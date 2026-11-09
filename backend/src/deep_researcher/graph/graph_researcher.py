"""保留 run/resume 接口，执行进度交给 LangGraph。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from langgraph.checkpoint.sqlite import SqliteSaver

from deep_researcher.config import Configuration, get_config
from deep_researcher.graph.graph_nodes import (
    ResearchNodes,
    build_graph,
)
from deep_researcher.graph.graph_repository import GraphRunRepository
from deep_researcher.graph.graph_state import (
    ResearchGraphState,
    encode_state,
    to_output,
)
from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
)
from deep_researcher.models import (
    SummaryState,
    SummaryStateOutput,
)


logger = logging.getLogger(__name__)

DEFAULT_GRAPH_DIR = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "langgraph"
)




class GraphDeepResearcher:
    """本地串行研究入口；同一实例按顺序调用。"""

    def __init__(
        self,
        config: Configuration | None = None,
        *,
        data_dir: Path = DEFAULT_GRAPH_DIR,
        nodes_factory: Callable[
            [Configuration, str], ResearchNodes
        ] = ResearchNodes,
    ) -> None:
        self._base_config = (
            config
            if config is not None
            else get_config()
        )

        self._directory = data_dir.resolve()
        self._directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._nodes_factory = nodes_factory
        self._last_run_id: str | None = None

    @property
    def last_run_id(self) -> str | None:
        return self._last_run_id

    def run(
        self,
        topic: str,
        max_tasks: int | None = None,
    ) -> SummaryStateOutput:
        '''run构造run_id并在数据库中构造条目'''
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError(
                "研究主题必须为非空字符串"
            )

        if max_tasks is not None and (
            type(max_tasks) is not int
            or max_tasks <= 0
        ):
            raise ValueError(
                "max_tasks 必须是正整数或 None"
            )

        run_id = uuid4().hex
        self._last_run_id = run_id

        # closing 在离开 with 时调用 close()，异常时也会调用。
        with closing(
            GraphRunRepository(
                self._directory / "runs.sqlite3"
            )
        ) as repository:
            repository.create(
                run_id,
                topic.strip(),
                self._base_config.snapshot(),
                max_tasks,
            )

        return self._execute(
            run_id,
            recover_interrupted=False,
        )

    def resume(
        self,
        run_id: str,
        *,
        recover_interrupted: bool = False,
    ) -> SummaryStateOutput:
        if re.fullmatch(
            r"[0-9a-f]{32}",
            run_id,
        ) is None:
            raise ValueError(
                "请使用完整的 32 位研究编号"
            )

        self._last_run_id = run_id

        return self._execute(
            run_id,
            recover_interrupted=recover_interrupted,
        )

    def _execute(
        self,
        run_id: str,
        *,
        recover_interrupted: bool,
    ) -> SummaryStateOutput:
        metadata_path = (
            self._directory / "runs.sqlite3"
        )
        checkpoint_path = (
            self._directory / "checkpoints.sqlite3"
        )

        if not metadata_path.is_file():
            raise FileNotFoundError(
                "运行元数据库不存在"
            )

        execution_id = uuid4().hex
        lock_path = self._directory / f"{run_id}.lock"

        # timeout=0：同一研究正被执行时，立即失败。
        # recover_interrupted=True 也不能越过这个锁。
        #
        # 一个 with 可以管理多个上下文管理器。
        # 退出时按进入顺序的逆序释放。
        with (
            FileLock(str(lock_path), timeout=0),
            log_context(
                run_id=run_id,
                execution_id=execution_id,
                task_id=None,
                stage="图调度",
                research_stage=None,
                checkpoint=None,
                agent=None,
            ),
            closing(
                GraphRunRepository(metadata_path)
            ) as repository,
        ):
            info = repository.load(run_id)

            if (
                info.status == "completed"
                and not checkpoint_path.is_file()
            ):
                raise RuntimeError(
                    "元数据已完成，但图检查点文件缺失"
                )

            config = self._base_config.for_resume(
                info.config
            )

            nodes = self._nodes_factory(
                config,
                execution_id,
            )

            try:
                # from_conn_string 是上下文管理器。
                # 必须让连接覆盖整个图执行过程。
                with SqliteSaver.from_conn_string(
                    str(checkpoint_path)
                ) as checkpointer:
                    graph = build_graph(
                        nodes,
                        checkpointer,
                    )

                    run_config: dict[str, Any] = {
                        "configurable": {
                            "thread_id": run_id,
                        },
                        # 这是图执行步数限制，不是业务任务数。
                        "recursion_limit": 1000,
                    }

                    snapshot = graph.get_state(
                        run_config
                    )

                    exists = (
                        checkpointer.get_tuple(
                            run_config
                        )
                        is not None
                    )

                    values = snapshot.values

                    if (
                        values
                        and values.get("version") != 1
                    ):
                        raise ValueError(
                            "图状态版本不兼容"
                        )

                    if (
                        values
                        and values.get(
                            "payload", {}
                        ).get("run_id") != run_id
                    ):
                        raise ValueError(
                            "图状态与研究编号不一致"
                        )

                    # 图已经完成，但元数据更新前进程退出：
                    # 只补齐元数据，不重新生成报告。
                    if (
                        values
                        and values.get("finished")
                        and not snapshot.next
                    ):
                        result = to_output(
                            values["payload"]
                        )

                        repository.update(
                            run_id,
                            "completed",
                        )

                        logger.info(
                            "返回已有报告",
                            extra={
                                "event": "research_reused",
                            },
                        )

                        return result

                    if info.status == "completed":
                        raise RuntimeError(
                            "元数据与图检查点不一致"
                        )

                    if (
                        info.status == "running"
                        and not recover_interrupted
                    ):
                        raise RuntimeError(
                            "上次运行未正常结束，"
                            "确认旧进程停止后使用 "
                            "recover_interrupted=True"
                        )

                    repository.update(
                        run_id,
                        "running",
                    )

                    initial: ResearchGraphState | None = None

                    if not exists:
                        initial = {
                            "version": 1,
                            "payload": encode_state(
                                SummaryState(
                                    run_id=run_id,
                                    research_topic=info.topic,
                                )
                            ),
                            "max_tasks": info.max_tasks,
                            "active_task_id": None,
                            "finished": False,
                        }

                    logger.info(
                        "执行研究图 · 恢复=%s 待执行=%s",
                        exists,
                        snapshot.next,
                        extra={
                            "event": "graph_execution_started",
                        },
                    )

                    try:
                        # 已有检查点时传 None：
                        # 继续原执行，不重新输入主题。
                        graph.invoke(
                            initial,
                            run_config,
                            durability="sync",
                        )

                        final = graph.get_state(
                            run_config
                        )

                        if (
                            final.next
                            or not final.values.get(
                                "finished"
                            )
                        ):
                            raise RuntimeError(
                                "研究图尚未完成"
                            )

                        result = to_output(
                            final.values["payload"]
                        )

                        repository.update(
                            run_id,
                            "completed",
                        )

                        logger.info(
                            "研究完成",
                            extra={
                                "event": "research_completed",
                            },
                        )

                        return result

                    except (
                        Exception,
                        KeyboardInterrupt,
                    ) as exc:
                        if isinstance(exc, Exception):
                            error_id = log_exception_once(
                                logger,
                                "研究图执行失败",
                                error=exc,
                            )
                        else:
                            error_id = None
                            logger.warning(
                                "用户中断 · "
                                "保留已提交的图检查点"
                            )

                        try:
                            repository.update(
                                run_id,
                                "failed",
                                f"{type(exc).__name__}: {exc}",
                            )
                        except Exception as save_error:
                            log_exception_once(
                                logger,
                                "运行元数据更新失败",
                                error=save_error,
                                extra={
                                    "related_error_id": error_id,
                                },
                            )
                        logger.warning(
                            "研究执行中止 · run_id=%s",
                            run_id,
                            extra={
                                "event": "research_stopped",
                                "run_id": run_id,
                                "execution_id": execution_id,
                                "error_id": error_id,
                            },
                        )

                        raise

            finally:
                nodes.close()


    