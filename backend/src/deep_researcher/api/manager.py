"""管理进程内的研究执行与有界排队。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Literal

from filelock import Timeout as FileLockTimeout

from deep_researcher.api.errors import ApiError
from deep_researcher.events import EventStore
from deep_researcher.config import Configuration, get_config
from deep_researcher.graph.graph_researcher import (
    DEFAULT_GRAPH_DIR,
    GraphDeepResearcher,
    RunSnapshot,
)
from deep_researcher.graph.graph_repository import RunInfo
from deep_researcher.graph.graph_state import (
    encode_state,
    to_output,
)
from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
)
from deep_researcher.models import SummaryStateOutput


logger = logging.getLogger(__name__)

Execution = Literal["idle", "queued", "executing"]


class ResearchManager:
    def __init__(
        self,
        config: Configuration | None = None,
        *,
        data_dir: Path = DEFAULT_GRAPH_DIR,
        max_inflight: int = 4,
        researcher_factory: Callable[
            [], GraphDeepResearcher
        ] | None = None,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight 必须为正数")

        self._config = (
            config if config is not None else get_config()
        )
        self._data_dir = data_dir
        self.events = EventStore(data_dir.resolve() / "events.sqlite3")
        self._factory = researcher_factory
        self._capacity = max_inflight

        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="research",
        )

        self._lock = Lock()
        self._jobs: dict[str, Future[None]] = {}
        self._closed = False

    def _researcher(self) -> GraphDeepResearcher:
        """每次操作创建独立实例。"""
        if self._factory is not None:
            return self._factory()

        return GraphDeepResearcher(
            config=self._config,
            data_dir=self._data_dir,
        )

    def _check_capacity(self) -> None:
        """调用此方法时，必须已经持有管理器锁。"""
        if self._closed:
            raise ApiError(
                503,
                "service_closing",
                "服务正在关闭",
            )

        active = sum(
            not future.done()
            for future in self._jobs.values()
        )

        if active >= self._capacity:
            raise ApiError(
                503,
                "queue_full",
                "研究队列已满，请稍后提交",
            )

    def start(
        self,
        topic: str,
        max_tasks: int | None,
    ) -> str:
        """创建研究并提交后台执行。"""
        with self._lock:
            self._check_capacity()

            run_id = self._researcher().create_run(
                topic,
                max_tasks,
            )
            future = self._submit_locked(run_id, False)

        # Future 已完成时，注册回调可能立即调用回调。
        # 所以必须离开上面的锁之后再注册。
        future.add_done_callback(
            partial(self._on_done, run_id)
        )

        logger.info(
            "研究已受理 · %s",
            topic,
            extra={
                "event": "research_accepted",
                "run_id": run_id,
            },
        )

        return run_id

    def resume(
        self,
        run_id: str,
        *,
        recover_interrupted: bool = False,
    ) -> None:
        """恢复已存在的研究。"""
        with self._lock:
            current = self._jobs.get(run_id)

            if current is not None and not current.done():
                raise ApiError(
                    409,
                    "already_submitted",
                    "研究已排队或正在执行",
                    run_id=run_id,
                )

            self._check_capacity()
            snapshot = self.get_snapshot(run_id)

            if snapshot.report_ready:
                raise ApiError(
                    409,
                    "already_completed",
                    "研究已经完成",
                    run_id=run_id,
                )

            if (
                snapshot.info.status == "running"
                and not recover_interrupted
            ):
                raise ApiError(
                    409,
                    "recovery_confirmation_required",
                    "上次执行未正常结束；确认旧进程已停止后再恢复",
                    run_id=run_id,
                )

            future = self._submit_locked(
                run_id,
                recover_interrupted,
            )

        future.add_done_callback(
            partial(self._on_done, run_id)
        )

        logger.info(
            "恢复请求已受理",
            extra={
                "event": "resume_accepted",
                "run_id": run_id,
            },
        )
    def delete(self, run_id: str) -> None:
          """拒绝删除本进程正在排队或执行的研究。"""
          with (
              self._lock,
              log_context(
                  run_id=run_id,
                  stage="删除研究",
              ),
          ):
              if self._closed:
                  raise ApiError(
                      503,
                      "service_closing",
                      "服务正在关闭",
                      run_id=run_id,
                  )

              current = self._jobs.get(run_id)

              if current is not None and not current.done():
                  logger.warning(
                      "暂不能删除 · 研究已排队或正在执行"
                  )

                  raise ApiError(
                      409,
                      "research_busy",
                      "研究已排队或正在执行，请结束后再删除",
                      run_id=run_id,
                  )

              try:
                  self._researcher().delete_run(run_id)

              except FileLockTimeout as exc:
                  logger.warning(
                      "暂不能删除 · 研究被其他执行者占用"
                  )

                  raise ApiError(
                      409,
                      "research_busy",
                      "研究正在其他进程中执行，请结束后再删除",
                      run_id=run_id,
                  ) from exc

              except Exception as exc:
                  error_id = log_exception_once(
                      logger,
                      "研究删除未完成",
                      error=exc,
                  )

                  raise ApiError(
                      500,
                      "delete_incomplete",
                      "删除未完成，可以按原研究编号重试",
                      run_id=run_id,
                      error_id=error_id,
                  ) from exc

              self._jobs.pop(run_id, None)

    def _submit_locked(
        self,
        run_id: str,
        recover_interrupted: bool,
    ) -> Future[None]:
        """提交后台工作；调用方已经持有管理器锁。"""
        try:
            future = self._executor.submit(
                self._execute,
                run_id,
                recover_interrupted,
            )
        except Exception as exc:
            with log_context(
                run_id=run_id,
                stage="任务提交",
            ):
                error_id = log_exception_once(
                    logger,
                    "提交后台任务失败",
                    error=exc,
                )

            self._save_failure(
                run_id,
                f"任务提交失败 · error_id={error_id}",
            )

            raise ApiError(
                503,
                "submit_failed",
                "提交失败，研究编号已保留，可以稍后恢复",
                run_id=run_id,
                error_id=error_id,
            ) from exc

        self._jobs[run_id] = future
        return future

    def _execute(
        self,
        run_id: str,
        recover_interrupted: bool,
    ) -> None:
        """在线程中执行；处理异常并补齐失败状态。"""
        with log_context(
            run_id=run_id,
            stage="后台执行",
        ):
            try:
                self._researcher().resume(
                    run_id,
                    recover_interrupted=recover_interrupted,
                )
            except FileLockTimeout:
                # 其他执行者持有锁，不能覆盖它的状态。
                logger.warning(
                    "研究已被其他执行者占用"
                )
            except Exception as exc:
                error_id = log_exception_once(
                    logger,
                    "后台研究失败",
                    error=exc,
                )

                self._save_failure(
                    run_id,
                    f"后台执行失败 · error_id={error_id}",
                )

    def _save_failure(
        self,
        run_id: str,
        message: str,
    ) -> None:
        """尽力保存失败记录；保存失败时记录独立异常。"""
        try:
            self._researcher().mark_failed_if_idle(
                run_id,
                message,
            )
        except Exception as exc:
            with log_context(
                run_id=run_id,
                stage="保存失败状态",
            ):
                log_exception_once(
                    logger,
                    "失败状态写入失败，请检查存储",
                    error=exc,
                )

    def _on_done(
        self,
        run_id: str,
        future: Future[None],
    ) -> None:
        """执行结束或取消后清理内存记录。"""
        if future.cancelled():
            self._save_failure(
                run_id,
                "服务关闭，排队任务未执行，请重新恢复",
            )

        with self._lock:
            # 防止旧任务的回调删除同编号的新执行。
            if self._jobs.get(run_id) is future:
                self._jobs.pop(run_id)

    def execution(self, run_id: str) -> Execution:
        """查询本进程是否正在处理该研究。"""
        with self._lock:
            future = self._jobs.get(run_id)

            if future is None or future.done():
                return "idle"

            return (
                "executing"
                if future.running()
                else "queued"
            )

    def get_snapshot(self, run_id: str) -> RunSnapshot:
          researcher = self._researcher()

          try:
              return researcher.read_run(run_id)

          except (KeyError, FileNotFoundError) as exc:
              raise ApiError(
                  404,
                  "run_not_found",
                  "研究不存在",
                  run_id=run_id,
              ) from exc

    def list_runs(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[RunInfo], bool]:
        return self._researcher().list_runs(
            limit=limit,
            offset=offset,
        )

    def get_result(self, run_id: str) -> SummaryStateOutput:
        snapshot = self.get_snapshot(run_id)

        if not snapshot.report_ready or snapshot.state is None:
            raise ApiError(
                409,
                "result_not_ready",
                "研究报告尚未就绪",
                run_id=run_id,
            )

        return to_output(
            encode_state(snapshot.state)
        )

    def close(self) -> None:
        """停止接收，取消排队工作，并等待正在执行的研究结束。"""
        with self._lock:
            self._closed = True

        # 不能持有管理器锁等待关闭：
        # 任务完成回调也需要取得这把锁。
        self._executor.shutdown(
            wait=True,
            cancel_futures=True,
        )