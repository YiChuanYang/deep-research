"""根据证据生成摘要，返回正文与引用。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.logging_config import log_operation
from deep_researcher.models import CitedText, SummaryState
from deep_researcher.prompts import summarizer_running_prompt
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.utils import strip_thinking_tokens


logger = logging.getLogger(__name__)


class SummarizationService:
    """只生成并校验摘要，不负责保存和推进任务。"""

    def __init__(
        self,
        summarizer_factory: Callable[[], SimpleAgent],
        config: Configuration,
    ) -> None:
        self._agent_factory = summarizer_factory
        self.config = config

    @log_operation(
        "任务总结",
        task_parameter="task_id",
        state_parameter="state",
        checkpoint="summarize",
    )
    def summarize_task(
        self,
        state: SummaryState,
        task_id: int,
    ) -> CitedText:
        started_at = perf_counter()
        task = state.get_task(task_id)
        evidence_service = EvidenceService()

        context = evidence_service.context(
            state,
            task.evidence_ids,
        )

        if not context.strip():
            raise ValueError(
                f"任务 {task_id} 没有可用证据"
            )

        prompt = summarizer_running_prompt.format(
            research_topic=state.research_topic,
            title=task.title,
            intent=task.intent,
            query=task.query,
            context=context,
        )

        logger.info(
            "正在生成摘要 · 任务=%s · 证据=%s",
            task.title,
            "、".join(task.evidence_ids) or "无",
            extra={
                "event": "summary_started",
                "task_title": task.title,
                "evidence_ids": task.evidence_ids,
            },
        )

        agent = self._agent_factory()
        agent.enable_tool_call = False

        try:
            response = agent.run(input_text=prompt)
        finally:
            agent.clear_history()

        if not isinstance(response, str):
            raise TypeError("总结结果必须是字符串")

        summary = response.strip()

        if self.config.strip_thinking_tokens:
            summary = strip_thinking_tokens(summary).strip()

        if not summary:
            raise ValueError("总结结果为空")

        # 固定的资料不足响应仍然允许没有引用。
        empty_marker = summary.strip("「」“”\"'。.!！")

        if empty_marker == "暂无可用信息":
            result = CitedText(text="暂无可用信息")
        else:
            # 返回值同时携带规范后的正文和合法编号；由图节点统一写入状态。
            result = evidence_service.prepare_text(
                state,
                summary,
                task.evidence_ids,
                document="summary",
            )

        logger.info(
            "摘要已生成 · %d 字 · 引用=%s · 耗时=%.2fs",
            len(result.text),
            "、".join(result.citation_ids) or "无",
            perf_counter() - started_at,
            extra={
                "event": "summary_completed",
                "citation_ids": result.citation_ids,
                "summary_chars": len(result.text),
            },
        )

        return result
