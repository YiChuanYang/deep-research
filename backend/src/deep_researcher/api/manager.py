"""管理 API 提交的研究任务。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from deep_researcher.config import Configuration, get_config
from deep_researcher.graph.graph_researcher import (
    DEFAULT_GRAPH_DIR,
    GraphDeepResearcher,
)
from deep_researcher.graph.graph_repository import RunInfo
from deep_researcher.logging_config import log_exception_once
from deep_researcher.models import SummaryStateOutput


logger = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    """研究正执行、已完成，或不允许从当前状态恢复。"""


class ResearchManager:
    """把 HTTP 操作转交给图研究器，并在后台执行耗时任务。"""

    def __init__(
        self,
        config: Configuration | None = None,
        *,
        data_dir: Path = DEFAULT_GRAPH_DIR,
    ) -> None:
        self._config = config if config is not None else get_config()
        self._data_dir = data_dir

        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="research"
        )
        #线程锁，主要防御resume情况下的线程冲突（因为run_id是一致的
        self._lock = Lock()
        self._active_run_ids: set[str] = set()

    def _researcher(self) -> GraphDeepResearcher:
        """每次操作创建独立研究器实例。"""
        return GraphDeepResearcher(
            config=self._config,
            data_dir=self._data_dir,
        )

    def start(self,
              topic:str,
              max_tasks:int|None,)->str:
        researcher =self._researcher()
        run_id = researcher.create_run(topic,max_tasks)
        self._scheduler(run_id, recover_interrupted=False)
        return run_id

    def resume(
        self,
        run_id: str,
        *,
        recover_interrupted: bool = False,
    ) -> None:
        """校验运行状态，然后安排恢复执行。"""
        info = self._researcher().get_run_info(run_id)

        if info.status == "completed":
            raise RunConflictError("研究已经完成")

        if info.status == "running" and not recover_interrupted:
            raise RunConflictError(
                "研究仍标记为运行中；确认旧进程已停止后再恢复"
            )

        self._scheduler(
            run_id,
            recover_interrupted=recover_interrupted,
        )

    def get_run_info(self, run_id: str) -> RunInfo:
        """读取运行状态。"""
        return self._researcher().get_run_info(run_id)

    def get_result(self, run_id: str) -> SummaryStateOutput:
        """读取已完成研究的结果。"""
        return self._researcher().get_result(run_id)


    def _scheduler(
            self,
            run_id,
            *,
            recover_interrupted:bool
    )->None:
        with self._lock:
            if run_id in self._active_run_ids:
                raise RunConflictError("这项研究已经在当前进程中执行")
            self._active_run_ids.add(run_id)

        try:
            self._executor.submit(
                self._execute,run_id,recover_interrupted
            )

        except Exception:
            with self._lock:
                self._active_run_ids.discard(run_id)
            raise

    def _execute(self,run_id,recover_interrupted)->None:
        try:
            self._researcher().resume(run_id,recover_interrupted=recover_interrupted)
        except Exception as exc:
            log_exception_once(
                logger,
                "后台研究执行异常 · run_id=%s",
                run_id,
                error=exc,
            )
        finally:
            with self._lock:
                self._active_run_ids.discard(run_id)

    def close(self) -> None:
        """关闭后台执行器。"""
        self._executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

