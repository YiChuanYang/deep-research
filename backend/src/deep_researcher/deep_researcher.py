"""DeepResearcher:串行研究流程，支持通过 SQLite 检查点恢复研究流程。"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration, get_config
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
    log_operation,
)
from deep_researcher.models import (
    ResearchRecord,
    SummaryState,
    SummaryStateOutput,
)
from deep_researcher.prompts import (
    reporter_system_prompt,
    summarizer_system_prompt,
    todo_planner_system_prompt,
)
from deep_researcher.repository import (
    ResearchRepository,
    RunOwnerShipError,
)
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.planner import PlanningService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.search import SearchService
from deep_researcher.services.summarizer import SummarizationService

logger = logging.getLogger(__name__)
task_logger = logging.getLogger(f"{__name__}.task")




class DeepResearcher:
    """"串行执行；异常时保存检查点，由 resume 显式继续。"""
    def __init__(
        self,
        config: Configuration | None = None,
        *,
        repository: ResearchRepository | None = None,
    ) -> None:
        self._base_config = config or get_config()
        self._config = self._base_config
        self._owns_repository = repository is None
        self._repository = (
            repository
            if repository is not None
            else ResearchRepository()
        )

        self._state: SummaryState | None = None
        self._llm=MyLLM()

    @property
    def last_run_id(self) -> str | None:
        """即使运行失败，也能获取用于恢复的编号。"""
        return (
            self._state.run_id
            if self._state is not None
            else None
        )

    def run(
        self,
        topic: str,
        max_tasks: int | None = None,
    ) -> SummaryStateOutput:
        """创建新研究；同主题再次 run 也会创建新编号。"""
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("研究主题必须是非空字符串")

        if max_tasks is not None and (
            type(max_tasks) is not int or max_tasks <= 0
        ):
            raise ValueError(
                "max_tasks 必须是正整数或 None"
            )

        #创建新研究，初始化state
        state = SummaryState(
            run_id=uuid4().hex,
            research_topic=topic.strip(),
        )
        self._state = state
        config = self._base_config.model_dump(
            mode="json",
            include={
                "enable_notes",
                "notes_workspace",
                "search_api",
                "fetch_full_page",
                "strip_thinking_tokens",
            },
        )

        # 恢复时即使工作目录改变，也使用原来的笔记目录。
        config["notes_workspace"] = str(
            Path(self._base_config.notes_workspace)
            .expanduser()
            .resolve()
        )

        #构造record
        record = ResearchRecord(
            state=state,
            config=config,
            max_tasks=max_tasks
        )
        with log_context(
            run_id=state.run_id,
            execution_id=uuid4().hex,
            task_id=None,
            stage="新建研究",
            research_stage=None,
            checkpoint=None,
        ):
            try:
                self._repository.create(record)
            except Exception as exc:
                log_exception_once(
                    logger, "研究记录创建失败", error=exc,
                    extra={"event": "research_create_failed", "research_stage": record.stage},
                )
                raise
            logger.info(
                "开始研究 · %s",
                state.research_topic,
                extra={"event": "research_started", "research_stage": "plan", "max_tasks": max_tasks},
            )

            return self._drive(state.run_id)


    def resume(
        self,
        run_id: str,
        *,
        recover_interrupted: bool = False,
    ) -> SummaryStateOutput:
        """普通失败直接恢复；强制接管要求旧进程已停止。"""
        with log_context(
            run_id=run_id,
            execution_id=uuid4().hex,
            task_id=None,
            stage="恢复研究",
            research_stage=None,
            checkpoint=None,
        ):
            logger.info(
                "准备恢复研究",
                extra={
                    "event": "research_resume_requested",
                    "recover_interrupted": recover_interrupted,
                },
            )

            return self._drive(
                run_id,
                recover_interrupted=recover_interrupted,
            )


    @log_operation("研究调度")
    def _drive(
        self,
        run_id: str,
        *,
        recover_interrupted: bool = False,
    ) -> SummaryStateOutput:
        #首先取得执行权
        record = self._repository.claim(run_id=run_id,recover_interrupted=recover_interrupted)
        state = record.state
        self._state = state
        started_at = perf_counter()
        if record.status == "completed":
            logger.info(
                "研究已完成 · 返回已有报告",
                extra={"event": "research_reused", "research_stage": "done"},
            )
            return self._to_output(state)

        try:
            self._config = Configuration.model_validate({
                **self._base_config.model_dump(),
                **record.config,
            })

            record.last_error = None

            logger.debug(
                "进入调度 · stage=%s",
                record.stage,
                extra={"event": "execution_started", "research_stage": record.stage},
            )
            if record.stage == "plan":
                with log_context(research_stage="plan"):
                    self._plan(state)
                record.stage = 'tasks'
                self._repository.update(record)

            if record.stage == "tasks":
                notes = NoteService(self._config)

                tasks = (
                    state.todo_items
                    if record.max_tasks is None
                    else state.todo_items[:record.max_tasks]
                )

                for position, task in enumerate(tasks, start=1):
                    if task.checkpoint == "done":
                        logger.debug(
                            "复用任务结果 · task_id=%d",
                            task.id,
                            extra={
                                "event": "task_reused",
                                "task_id": task.id,
                                "research_stage": "tasks",
                                "checkpoint": task.checkpoint,
                                "task_status": task.status,
                            },
                        )
                        continue

                    with log_context(
                        research_stage="tasks",
                        task_position=position,
                        task_count=len(tasks),
                    ):
                        self._execute_task(record, task.id, notes)

                if not any(
                    task.status == "completed"
                    and task.summary
                    for task in tasks
                ):
                    raise RuntimeError(
                        "没有有效任务摘要，无法生成报告"
                    )

                record.stage = "report"
                self._repository.update(record)

            if record.stage == "report":
                reporter = ReportingService(
                    self._create_reporter_agent
                )

                with log_context(research_stage="report", checkpoint=None):
                    report = reporter.generate_report(state)
                report = self._append_scope_notice(
                    state,
                    report,
                )

                state.structured_report = report
                state.running_summary = report

                record.stage = "save_report"

                # 先把报告正文保存到 SQLite。
                self._repository.update(record)

            if record.stage == "save_report":
                with log_context(research_stage="save_report", checkpoint=None):
                    NoteService(self._config).save_report_note(state)

                record.stage = "done"

            # 单独处理 done，兼容：
            # 文件保存成功，但最后一次数据库提交失败。
            if record.stage == "done":
                self._repository.update(
                    record,
                    status="completed",
                )

            duration = perf_counter() - started_at
            completed = sum(task.status == "completed" for task in state.todo_items)
            skipped = sum(task.status == "skipped" for task in state.todo_items)
            logger.info(
                "研究完成 · 完成=%d 跳过=%d 未执行=%d · 本次耗时=%.2fs",
                completed, skipped, len(state.todo_items) - completed - skipped, duration,
                extra={
                    "event": "research_completed",
                    "research_stage": "done",
                    "status": record.status,
                    "completed_tasks": completed,
                    "skipped_tasks": skipped,
                    "task_count": len(state.todo_items),
                    "duration_s": round(duration, 3),
                },
            )

            return self._to_output(state)

        except RunOwnerShipError as exc:
            # 执行权已失效，不能覆盖其他执行者的状态。
            log_exception_once(
                logger, "研究停止 · 执行权已失效", error=exc,
                extra={"event": "research_ownership_lost", "research_stage": record.stage},
            )
            raise

        except Exception as exc:
            error_id = log_exception_once(
                logger, "研究执行失败", error=exc,
                extra={"event": "operation_failed", "research_stage": record.stage},
            )
            record.last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            failure_saved = False
            try:
                self._repository.update(
                    record,
                    status="failed",
                )
                failure_saved = True
            except Exception as save_error:
                log_exception_once(
                    logger,
                    "故障状态保存失败 · "
                    "保留数据库上次成功的检查点",
                    error=save_error,
                    extra={
                        "event": "failure_checkpoint_failed",
                        "research_stage": record.stage,
                        "related_error_id": error_id,
                    },
                )

            logger.warning(
                "研究已中止 · stage=%s · run_id=%s",
                record.stage, state.run_id,
                extra={
                    "event": "research_failed",
                    "research_stage": record.stage,
                    "error_id": error_id,
                    "failure_saved": failure_saved,
                    "duration_s": round(perf_counter() - started_at, 3),
                },
            )
            raise
                




    def _plan(self, state: SummaryState) -> None:
        """为本轮规划创建独立的 Agent 对话。"""
        agent = ToolAwareAgent(
            name="Agent_for_planner",
            llm=self._llm,
            sys_prompt=todo_planner_system_prompt,
            config=self._config,
        )

        try:
            PlanningService(agent).plan_todo_list(state)
        finally:
            agent.clear_history()


    @log_operation(
        "执行任务",
        task_parameter="task_id",
    )
    def _execute_task(
        self,
        record: ResearchRecord,
        task_id: int,
        notes: NoteService,
    ) -> None:
        """从任务检查点继续搜索、总结或笔记保存。"""
        state = record.state
        task = state.get_task(task_id)
        started_at = perf_counter()

        task.status = "running"
        task.last_error = None
        with log_context(checkpoint=task.checkpoint):
            self._repository.update(record)

        task_logger.info(
            "开始 · %s · checkpoint=%s",
            task.title,
            task.checkpoint,
            extra={"event": "task_started", "checkpoint": task.checkpoint},
        )

        try:
            if task.checkpoint == "search":
                SearchService(
                    self._config
                ).search_task(state, task_id)

                if not state.search_contexts.get(
                    task_id, ""
                ).strip():
                    task.status = "skipped"
                    task.checkpoint = "done"
                    task.notices.append(
                        "搜索没有可用素材"
                    )

                    with log_context(checkpoint=task.checkpoint):
                        self._repository.update(record)
                    task_logger.warning(
                        "任务跳过 · 搜索没有可用素材",
                        extra={"event": "task_skipped", "checkpoint": "done", "reason": "empty_search"},
                    )
                    return

                task.checkpoint = "summarize"
                with log_context(checkpoint=task.checkpoint):
                    self._repository.update(record)

            if task.checkpoint == "summarize":
                summarizer = SummarizationService(
                    self._create_summarizer_agent,
                    config=self._config,
                    note_service=notes,
                )

                summary = summarizer.summarize_task(
                    state,
                    task_id,
                    persist_note=False,
                )

                normalized = summary.strip().strip(
                    "「」“”\"'。.!！"
                )

                if normalized == "暂无可用信息":
                    task.status = "skipped"
                    task.checkpoint = "done"
                    task.notices.append(
                        "无有效研究信息，未纳入报告"
                    )

                    with log_context(checkpoint=task.checkpoint):
                        self._repository.update(record)
                    task_logger.warning(
                        "任务跳过 · 无有效研究信息",
                        extra={"event": "task_skipped", "checkpoint": "done", "reason": "empty_summary"},
                    )
                    return

                task.checkpoint = "save_note"

                # 先保存摘要正文，再尝试保存笔记文件。
                with log_context(checkpoint=task.checkpoint):
                    self._repository.update(record)

            if task.checkpoint == "save_note":
                notes.save_task_note(state, task_id)

                task.status = "completed"
                task.checkpoint = "done"
                with log_context(checkpoint=task.checkpoint):
                    self._repository.update(record)

                task_logger.info(
                    "任务完成 · 耗时=%.2fs",
                    perf_counter() - started_at,
                    extra={
                        "event": "task_completed",
                        "checkpoint": "done",
                        "task_status": task.status,
                        "duration_s": round(perf_counter() - started_at, 3),
                    },
                )

        except Exception as exc:
            if task.checkpoint != "done":
                task.status = "failed"
                task.last_error = (
                    f"{type(exc).__name__}: {exc}"
                )

            # 由 _drive 统一保存失败状态。
            error_id = log_exception_once(
                task_logger, "任务执行失败", error=exc,
                extra={"event": "operation_failed", "checkpoint": task.checkpoint},
            )
            task_logger.warning(
                "任务中止 · 恢复位置=%s", task.checkpoint,
                extra={
                    "event": "task_failed",
                    "checkpoint": task.checkpoint,
                    "error_id": error_id,
                    "task_status": task.status,
                },
            )
            raise

    @staticmethod
    def _append_scope_notice(
        state: SummaryState,
        report: str,
    ) -> str:
        """标明本轮没有纳入报告的任务。"""
        unfinished = [
            task
            for task in state.todo_items
            if task.status != "completed"
        ]

        if not unfinished:
            return report

        labels = {
            "pending": "本轮未执行",
            "running": "尚未完成",
            "failed": "执行失败",
            "skipped": "未获得有效研究资料",
        }

        lines = [
            "## 研究范围说明",
            "",
            "以下任务未纳入报告结论，"
            "相关问题仍需补充研究：",
            "",
        ]

        lines.extend(
            f"- 任务 {task.id}「{task.title}」："
            f"{labels.get(task.status, task.status)}。"
            for task in unfinished
        )

        return (
            f"{report.rstrip()}\n\n"
            + "\n".join(lines)
        )

    def _create_summarizer_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_summarizer",
            llm=self._llm,
            sys_prompt=summarizer_system_prompt,
            config=self._config,
            enable_tool_call=False,
        )

    def _create_reporter_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_reporter",
            llm=self._llm,
            sys_prompt=reporter_system_prompt,
            config=self._config,
            enable_tool_call=False,
        )

    def close(self) -> None:
        """释放自有资源；外部注入的仓储由调用方关闭。"""
        try:
            if self._llm is not None:
                self._llm.client.close()
                self._llm = None
        finally:
            if self._owns_repository:
                self._repository.close()





        







    @staticmethod
    def _to_output(
        state: SummaryState,
    ) -> SummaryStateOutput:
        """返回任务副本，避免调用方修改内部状态。"""
        return SummaryStateOutput(
            run_id=state.run_id,
            running_summary=state.running_summary,
            report_markdown=state.structured_report,
            todo_items=deepcopy(state.todo_items),
        )
