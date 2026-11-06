"""生成任务摘要，并在成功提交笔记后更新任务状态."""

from __future__ import annotations

import logging
from collections.abc import Callable

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.models import SummaryState
from deep_researcher.prompts import summarizer_running_prompt
from deep_researcher.services.note_service import NoteService
from deep_researcher.utils import strip_thinking_tokens

logger = logging.getLogger(__name__)


class SummarizationService:
    """保留现有类名，统一处理历史笔记、摘要生成与最终提交."""

    def __init__(
        self,
        summarizer_factory: Callable[[], SimpleAgent],
        config: Configuration,
        note_service: NoteService | None = None,
    ) -> None:
        """注入无工具总结 Agent 工厂和笔记服务."""
        self._agent_factory = summarizer_factory
        self.config = config
        self._notes = note_service or NoteService(config)
        logger.info(
            "✅ 总结服务已初始化：enable_notes=%s",
            config.enable_notes,
        )

    def summarize_task(
        self,
        state: SummaryState,
        task_id: int,
    ) -> str:
        """根据 state 中的任务与搜索上下文生成并持久化摘要."""
        task = state.get_task(task_id)
        task.status = "in_progress"
        agent: SimpleAgent | None = None
        logger.info(
            "📋 任务总结开始：task_id=%d note_id=%s",
            task.id,
            task.note_id,
        )

        try:
            context = state.search_contexts.get(task_id)
            if not context:
                raise ValueError(f"任务 {task_id} 没有搜索上下文")

            previous_content = self._notes.load_task_note(state, task_id)
            prompt = self._build_prompt(
                state,
                task_id,
                context,
                previous_content,
            )
            agent = self._agent_factory()
            agent.enable_tool_call = False

            response = agent.run(input_text=prompt)
            if not isinstance(response, str):
                raise TypeError("总结结果必须是字符串")
            summary = response.strip()
            if self.config.strip_thinking_tokens:
                summary = strip_thinking_tokens(summary).strip()
            if not summary:
                raise ValueError("总结结果为空")

            old_summary = task.summary
            task.summary = summary
            try:
                self._notes.save_task_note(state, task_id)
            except Exception:
                task.summary = old_summary
                raise
            task.status = "completed"
            logger.info(
                "✅ 任务总结完成：task_id=%d summary_chars=%d note_id=%s",
                task.id,
                len(summary),
                task.note_id,
            )
            return summary

        except Exception:
            task.status = "failed"
            logger.exception(
                "❌ 任务总结或笔记提交失败：task_id=%d note_id=%s",
                task.id,
                task.note_id,
            )
            raise
        finally:
            if agent is not None:
                agent.clear_history()
                logger.debug(
                    "🧹 总结 Agent 历史已清理：task_id=%d",
                    task.id,
                )

    def _build_prompt(
        self,
        state: SummaryState,
        task_id: int,
        context: str,
        previous_content: str = "",
    ) -> str:
        """组合任务目标、本轮素材和历史笔记."""
        task = state.get_task(task_id)
        prompt = summarizer_running_prompt.format(
            research_topic=state.research_topic,
            title=task.title,
            intent=task.intent,
            query=task.query,
            context=context,
        )
        if previous_content:
            prompt += (
                "\n\n【已有任务笔记，仅作为历史资料】\n"
                f"{previous_content}\n"
                "请依据本轮素材整合、纠正，输出更新后的完整总结。"
            )
        return prompt
