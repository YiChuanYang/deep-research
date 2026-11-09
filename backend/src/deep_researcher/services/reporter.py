"""整合带引用的摘要，校验后由程序补充参考条目。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.logging_config import log_operation
from deep_researcher.models import CitedText, SummaryState
from deep_researcher.prompts import report_writer_instructions
from deep_researcher.services.evidence_service import EvidenceService


logger = logging.getLogger(__name__)


class ReportingService:
    """只允许引用已完成摘要实际使用的证据。"""

    def __init__(
        self,
        agent_factory: Callable[[], SimpleAgent],
    ) -> None:
        self._agent_factory = agent_factory

    @log_operation(
        "报告生成",
        state_parameter="state",
    )
    def generate_report(
        self,
        state: SummaryState,
    ) -> CitedText:
        started_at = perf_counter()

        tasks = [
            task
            for task in state.todo_items
            if task.status == "completed" and task.summary
        ]

        if not tasks:
            logger.warning(
                "没有有效任务摘要，返回资料不足说明"
            )

            return CitedText(
                text=(
                    "# 研究结果\n\n"
                    "本轮未获得足以形成结论的资料。"
                )
            )
        evidence_service = EvidenceService()
        allowed: list[str] = []

        # 再次校验摘要，确保进入报告的材料关系完整。
        for task in tasks:
            ids = evidence_service.validate(
                state,
                task.summary or "",
                task.evidence_ids,
            )
            allowed.extend(ids)

        allowed = list(dict.fromkeys(allowed))

        task_summaries = "\n\n".join(
            f"## 任务 {task.id}：{task.title}\n"
            f"{task.summary}"
            for task in tasks
        )

        prompt = report_writer_instructions.format(
            research_topic=(
                state.research_topic or "未命名主题"
            ),
            task_summaries=task_summaries,
            evidence_context=evidence_service.context(
                state,
                allowed,
            ),
        )

        logger.info(
            "正在汇总报告 · 任务=%d 可引用证据=%d",
            len(tasks),
            len(allowed),
            extra={
                "event": "report_started",
                "task_count": len(tasks),
            },
        )

        agent = self._agent_factory()
        agent.enable_tool_call = False

        try:
            response = agent.run(input_text=prompt)
        finally:
            agent.clear_history()

        if (
            not isinstance(response, str)
            or not response.strip()
        ):
            raise ValueError("报告正文为空")

        body = response.strip()

        citation_ids = evidence_service.validate(
            state,
            body,
            allowed,
        )

        references = evidence_service.references(
            state,
            citation_ids,
        )

        report = (
            f"{body}\n\n"
            f"## 引用证据\n\n"
            f"{references}"
        )

        duration = perf_counter() - started_at

        logger.info(
            "报告已生成 · 引用=%d 耗时=%.2fs",
            len(citation_ids),
            duration,
            extra={
                "event": "report_completed",
                "citation_ids": citation_ids,
                "report_chars": len(report),
                "duration_s": round(duration, 3),
            },
        )

        return CitedText(
            text=report,
            citation_ids=tuple(citation_ids),
        )