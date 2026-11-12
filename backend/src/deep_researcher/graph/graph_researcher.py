"""保留 run/resume 接口，执行进度交给 LangGraph。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4
from dataclasses import dataclass
from filelock import FileLock, Timeout as FileLockTimeout
from deep_researcher.tools.note_tool import NoteTool
from langgraph.checkpoint.sqlite import SqliteSaver

from deep_researcher.config import Configuration, get_config
from deep_researcher.events import EventStore
from deep_researcher.graph.graph_nodes import (
    ResearchNodes,
    build_graph,
)
from deep_researcher.graph.graph_repository import GraphRunRepository,RunInfo
from deep_researcher.graph.graph_state import (
    ResearchGraphState,
    encode_state,
    decode_state,
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

@dataclass(frozen=True)
class RunSnapshot:
    """一次查询取得的运行记录和图检查点视图。"""

    info: RunInfo
    state: SummaryState | None
    next_nodes: tuple[str, ...]
    active_task_id: int | None
    graph_finished: bool

    @property
    def report_ready(self) -> bool:
        """判断是否已经有可交付的最终报告。"""
        return (
            self.graph_finished
            and not self.next_nodes
            and self.state is not None
            and bool(
                (self.state.structured_report or "").strip()
            )
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

        self._events = EventStore(self._directory / "events.sqlite3")
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
        run_id = self.create_run(topic,max_tasks)

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
        '''内部包含新建和续跑的逻辑：
        如果checkpointer不存在 则构建；存在则读取续跑;
        两者共同的基础都是再数据库中初始化了条目'''
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
                        "执行研究图 · 主题=%s · 恢复=%s · 待执行=%s",
                        info.topic,
                        exists,
                        snapshot.next,
                        extra={
                            "event": "graph_execution_started",
                            "research_topic": info.topic,
                            "max_tasks": info.max_tasks,
                            "resuming": exists,
                            "next_nodes": snapshot.next,
                        },
                    )

                    # 事件展示故障不破坏研究结果；禁用本次事件写入并只记一次日志。
                    # 前端仍可通过状态查询取得最终结果。
                    events_available = True
                    try:
                        self._events.initialize()
                    except Exception as event_error:
                        events_available = False
                        logger.warning("事件存储不可用 · 继续研究 · %s", type(event_error).__name__)

                    def emit(event_type: str, data: dict[str, Any]) -> None:
                        nonlocal events_available
                        if not events_available:
                            return
                        try:
                            self._events.append(
                                run_id=run_id,
                                execution_id=execution_id,
                                event_type=event_type,
                                data=data,
                            )
                        except Exception as event_error:
                            events_available = False
                            logger.warning(
                                "事件写入中断 · 已降级为进度查询 · %s",
                                type(event_error).__name__,
                            )

                    try:
                        emit("research.started", {"resuming": exists})
                        # stream 返回迭代器；消费时执行图并接收节点发出的字典。
                        # 恢复时仍传 None，检查点、锁和异常处理保持原有职责。
                        for event in graph.stream(
                            initial,
                            run_config,
                            stream_mode="custom",
                            version="v1",
                            durability="sync",
                        ):
                            emit(event["type"], event["data"])

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

                        emit("research.completed", {})

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
                        emit("research.failed", {"error_id": error_id})
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

    def delete_run(self, run_id: str) -> None:
          """删除研究及关联数据；不存在时视为已经删除。"""
          if re.fullmatch(
              r"[0-9a-f]{32}",
              run_id,
          ) is None:
              raise ValueError(
                  "请使用完整的 32 位研究编号"
              )

          metadata_path = (
              self._directory / "runs.sqlite3"
          )

          if not metadata_path.is_file():
              return

          with (
              FileLock(
                  str(self._directory / f"{run_id}.lock"),
                  timeout=0,
              ),
              log_context(
                  run_id=run_id,
                  stage="删除研究",
              ),
              closing(
                  GraphRunRepository(metadata_path)
              ) as repository,
          ):
              try:
                  info = repository.load(
                      run_id,
                      include_deleted=True,
                  )
              except KeyError:
                  # 已经删除，再次请求仍然成功。
                  return

              saved_workspace = info.config.get(
                  "notes_workspace"
              )

              if (
                  not isinstance(saved_workspace, str)
                  or not saved_workspace.strip()
              ):
                  raise ValueError(
                      "历史配置缺少笔记目录，请先补齐记录"
                  )

              workspace = Path(saved_workspace)

              if not workspace.is_absolute():
                  raise ValueError(
                      "历史笔记目录不是绝对路径，请先迁移记录"
                  )

              logger.info(
                  "开始删除研究 · 主题=%s",
                  info.topic,
                  extra={
                      "event": "research_delete_started",
                  },
              )

              # 此后普通查询和恢复都无法取得这条记录。
              repository.mark_deleted(run_id)

              checkpoint_path = (
                  self._directory / "checkpoints.sqlite3"
              )

              if checkpoint_path.is_file():
                  with SqliteSaver.from_conn_string(
                      str(checkpoint_path)
                  ) as checkpointer:
                      checkpointer.delete_thread(run_id)

              removed_files = NoteTool.delete_run_notes(
                  workspace,
                  run_id,
              )

              self._events.delete_run(run_id)

              # 最后删除元数据；失败重试还需要其中的配置。
              repository.purge_deleted(run_id)

              logger.info(
                  "研究已删除 · 笔记文件=%d",
                  removed_files,
                  extra={
                      "event": "research_deleted",
                      "removed_files": removed_files,
                  },
              )


    #面向fastapi的方法
    def create_run(
            self,
            topic:str,
            max_tasks:int|None=None,
    )->str:
        """面向fastapi,创建研究，返回run_id,但是不执行后续流程"""
        if not isinstance(topic,str) or not topic.strip():
            raise ValueError("topic必须是字符串且非空值")

        if max_tasks is not None and (type(max_tasks)!=int or max_tasks<=0):
            raise ValueError("max_tasksz必须是非负整数")

        run_id = uuid4().hex
        with closing(
            GraphRunRepository(self._directory/"runs.sqlite3")
        ) as repository:
            repository.create(
                run_id,topic,self._base_config.snapshot(),max_tasks
            )

        self._last_run_id = run_id
        return run_id
    
    def get_run_info(self, run_id: str) -> RunInfo:
        """读取运行元数据，不执行研究图。"""
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise ValueError("请使用完整的 32 位研究编号")

        metadata_path = self._directory / "runs.sqlite3"
        if not metadata_path.is_file():
            raise FileNotFoundError("运行元数据库不存在")

        with closing(GraphRunRepository(metadata_path)) as repository:
            return repository.load(run_id)

    def list_runs(self,limit:int,offset:int=0):
         if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")

         path = self._directory / "runs.sqlite3"

         if not path.is_file():
            return [], False
         with closing(
             GraphRunRepository(path)
         ) as repository:
             rows = repository.list_runs(limit+1,offset)

         return rows[:limit],len(rows)>limit

    def read_run(self, run_id: str) -> RunSnapshot:
        """读取检查点，不执行节点，也不校验原模型连接配置。"""
        info = self.get_run_info(run_id)
        path = self._directory / "checkpoints.sqlite3"

        if not path.is_file():
            if info.status == "completed":
                raise RuntimeError(
                    "已完成研究的检查点文件缺失"
                )

            return RunSnapshot(
                info=info,
                state=None,
                next_nodes=(),
                active_task_id=None,
                graph_finished=False,
            )
        nodes = self._nodes_factory(
            self._base_config,
            uuid4().hex)
        try:
            with SqliteSaver.from_conn_string(
                str(path)
            ) as checkpointer:
                graph = build_graph(nodes,checkpointer)
                
                snapshot = graph.get_state(
                    {
                    
                        "configurable":{
                            "thread_id":run_id,
                        },
                        
                    }
                )
        finally :
            nodes.close()

        values = snapshot.values or {}
        payload = values.get("payload")

        state: SummaryState | None = None

        if payload is not None:
            if values.get("version") != 1:
                raise RuntimeError("图状态版本不兼容")

            if not isinstance(payload, dict):
                raise RuntimeError("图业务状态格式错误")

            state = decode_state(payload)

            if state.run_id != run_id:
                raise RuntimeError(
                    "图状态与研究编号不一致"
                )

        result = RunSnapshot(
            info=info,
            state=state,
            next_nodes=tuple(snapshot.next),
            active_task_id=values.get("active_task_id"),
            graph_finished=values.get("finished") is True,
        )

        if info.status == "completed" and not result.report_ready:
            raise RuntimeError(
                "已完成运行与图检查点不一致"
            )

        return result
        
        



    def get_result(self, run_id: str) -> SummaryStateOutput:
        """读取可交付的最终结果。"""
        snapshot = self.read_run(run_id)

        if not snapshot.report_ready or snapshot.state is None:
            raise RuntimeError("研究结果尚未就绪")

        return to_output(
            encode_state(snapshot.state)
        )
    def mark_failed_if_idle(
        self,
        run_id: str,
        message: str,
    ) -> None:
        """补记入队或图初始化失败，不覆盖其他执行者占用的研究。"""
        lock_path = self._directory / f"{run_id}.lock"

        try:
            with FileLock(str(lock_path), timeout=0):
                with closing(
                    GraphRunRepository(
                        self._directory / "runs.sqlite3"
                    )
                ) as repository:
                    info = repository.load(run_id)

                    if info.status in {"pending", "running"}:
                        repository.update(
                            run_id,
                            "failed",
                            message,
                        )
        except FileLockTimeout:
            return