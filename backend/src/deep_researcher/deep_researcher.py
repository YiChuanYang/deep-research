"""协调规划、搜索、总结和报告保存的串行研究流程."""

from __future__ import annotations

import logging
from copy import deepcopy
from time import perf_counter

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration, get_config
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import log_context, log_operation
from deep_researcher.models import SummaryState, SummaryStateOutput, TodoItem
from deep_researcher.prompts import (
    reporter_system_prompt,
    summarizer_system_prompt,
    todo_planner_system_prompt,
)
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.planner import PlanningService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.search import SearchService
from deep_researcher.services.summarizer import SummarizationService

logger = logging.getLogger(__name__)
task_logger = logging.getLogger(f"{__name__}.task")


class DeepResearcher:
    """共享模型和笔记服务，按任务顺序执行一轮研究."""

    def __init__(self, config: Configuration | None = None) -> None:
        """组装服务，各服务日志继承运行时的研究和任务上下文."""
        self._llm = MyLLM()
        self._config = config or get_config()
        self._state: SummaryState | None = None
        self._note = NoteService(self._config)
        self._search = SearchService(self._config)
        self._planner_agent = ToolAwareAgent(
            name="Agent_for_planner",
            llm=self._llm,
            sys_prompt=todo_planner_system_prompt,
            config=self._config,
        )
        self._planner = PlanningService(self._planner_agent)
        self._summarizer = SummarizationService(
            summarizer_factory=self._create_summarizer_agent,
            config=self._config,
            note_service=self._note,
        )
        self._reporter = ReportingService(self._create_reporter_agent)
        logger.debug(
            "初始化完成 · provider=%s model=%s",
            self._llm.llm_provider,
            self._llm.model,
        )

    @log_operation("研究流程", new_run=True)
    def run(self, topic: str, max_tasks: int | None = None) -> SummaryStateOutput:
        """执行研究，保留最近一次运行的内存状态."""
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("研究主题必须是非空字符串")
        if max_tasks is not None and (type(max_tasks) is not int or max_tasks <= 0):
            raise ValueError("max_tasks 必须是正整数或 None")

        started_at = perf_counter()
        state = SummaryState(research_topic=topic.strip())
        self._state = state
        logger.info(
            "开始研究 · %s · 任务上限=%s",
            state.research_topic,
            max_tasks if max_tasks is not None else "全部",
            extra={"event": "research_started"},
        )
        self._planner_agent.clear_history()
        try:
            self._planner.plan_todo_list(state)
        finally:
            self._planner_agent.clear_history()

        tasks = state.todo_items if max_tasks is None else state.todo_items[:max_tasks]
        if len(tasks) != len(state.todo_items):
            logger.info(
                "本轮执行 %d/%d 个任务，其余任务保留待办",
                len(tasks),
                len(state.todo_items),
            )
        for index, task in enumerate(tasks, start=1):
            with log_context(task_id=task.id, stage="任务执行"):
                task_logger.info(
                    "开始 · %s · 进度=%d/%d",
                    task.title,
                    index,
                    len(tasks),
                    extra={"event": "task_started"},
                )
                self._execute_task(state, task.id)

        completed_count = sum(
            task.status == "completed" and bool(task.summary)
            for task in state.todo_items
        )
        if completed_count == 0:
            raise RuntimeError("本轮没有获得有效任务摘要，无法生成研究报告")

        report = self._reporter.generate_report(state)
        report = self._append_scope_notice(state, report)
        state.structured_report = report
        state.running_summary = report
        self._note.save_report_note(state)
        elapsed = perf_counter() - started_at
        logger.info(
            "研究完成 · 任务=%d/%d · 报告=%d 字 · 总耗时=%.2fs",
            completed_count,
            len(state.todo_items),
            len(report),
            elapsed,
            extra={
                "event": "research_completed",
                "duration_s": round(elapsed, 3),
                "report_note_id": state.report_note_id,
            },
        )
        return SummaryStateOutput(
            running_summary=report,
            report_markdown=report,
            todo_items=deepcopy(state.todo_items),
        )

    @log_operation("任务执行", task_parameter="task_id")
    def _execute_task(self, state: SummaryState, task_id: int) -> None:
        """记录任务结果，单个任务失败后继续执行后续任务."""
        task = state.get_task(task_id)
        task.status = "in_progress"
        started_at = perf_counter()
        try:
            self._search.search_task(state, task_id)
            context = state.search_contexts.get(task_id, "")
            if not context.strip():
                task.status = "skipped"
                task.notices.append("搜索未返回可用素材，本轮跳过总结")
                task_logger.warning(
                    "已跳过 · 搜索素材为空",
                    extra={"event": "task_skipped"},
                )
                return
            summary = self._summarizer.summarize_task(state, task_id)
            normalized = summary.strip().strip("「」“”\"'。.!！")
            if normalized == "暂无可用信息":
                task.status = "skipped"
                task.notices.append("总结未获得有效研究信息，未纳入最终报告")
                task_logger.warning(
                    "已跳过 · 未获得有效研究信息",
                    extra={"event": "task_skipped"},
                )
                return
            task_logger.debug(
                "任务完成 · 耗时=%.2fs",
                perf_counter() - started_at,
                extra={"event": "task_completed"},
            )
        except Exception as exc:
            task.status = "failed"
            task.notices.append(f"失败：{type(exc).__name__}")
            # 服务边界已记录根因和堆栈，此处只说明调度决定。
            task_logger.warning(
                "任务失败 · 继续其他任务",
                extra={"event": "task_failed"},
            )

    @staticmethod
    def _append_scope_notice(state: SummaryState, report: str) -> str:
        """为部分完成的研究添加范围说明，并随报告一起保存."""
        unfinished: list[TodoItem] = [
            task for task in state.todo_items if task.status != "completed"
        ]
        if not unfinished:
            return report
        status_labels: dict[str, str] = {
            "failed": "执行失败",
            "skipped": "未获得有效研究资料",
            "pending": "本轮未执行",
            "in_progress": "尚未完成",
        }
        lines = [
            "## 研究范围说明",
            "",
            "本报告仅基于已完成任务的摘要生成，"
            "以下任务未纳入结论，相关问题仍需补充研究：",
            "",
        ]
        for task in unfinished:
            status = status_labels.get(task.status, task.status)
            lines.append(f"- 任务 {task.id}「{task.title}」：{status}。")
        return f"{report.rstrip()}\n\n" + "\n".join(lines)

    def _create_summarizer_agent(self) -> SimpleAgent:
        """创建独立的任务总结对话."""
        return SimpleAgent(
            name="Agent_for_summarizer",
            llm=self._llm,
            sys_prompt=summarizer_system_prompt,
            config=self._config,
            enable_tool_call=False,
        )

    def _create_reporter_agent(self) -> SimpleAgent:
        """创建报告生成对话."""
        return SimpleAgent(
            name="Agent_for_reporter",
            llm=self._llm,
            sys_prompt=reporter_system_prompt,
            config=self._config,
            enable_tool_call=False,
        )
