"""生成任务摘要，并在成功提交笔记后更新任务状态."""

from __future__ import annotations

import logging
from time import perf_counter
from collections.abc import Callable
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.models import SummaryState
from deep_researcher.prompts import summarizer_running_prompt
from deep_researcher.services.note_service import NoteService
from deep_researcher.utils import strip_thinking_tokens

from deep_researcher.logging_config import log_operation

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
        logger.debug(
            "✅ 总结服务已初始化：enable_notes=%s",
            config.enable_notes,
        )

    @log_operation("任务总结", task_parameter="task_id", state_parameter="state", checkpoint="summarize")
    def summarize_task(
        self,
        state: SummaryState,
        task_id: int,
        *,
        persist_note: bool = True,
    ) -> str:
        """生成任务摘要，并根据调用方要求决定是否保存笔记。"""
        started_at = perf_counter()
        task = state.get_task(task_id)

        # 与你当前 TodoItem.status 的定义保持一致。
        task.status = "running"

        agent: SimpleAgent | None = None
        logger.info("正在生成摘要", extra={"event": "summary_started", "persist_note": persist_note})

        try:
            evidence_service = EvidenceService()

            context = evidence_service.context(
                state,
                task.evidence_ids,
            )

            if not context or not context.strip():
                raise ValueError(
                    f"任务 {task_id} 没有搜索上下文"
                )

            previous_content = self._notes.load_task_note(
                state,
                task_id,
            )

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
                summary = strip_thinking_tokens(
                    summary
                ).strip()

            if not summary:
                raise ValueError("总结结果为空")

            # 无论是否立即保存笔记，都先把正文放入共享 state。
            normalized = summary.strip().strip(
                "「」“”\"'。.!！"
            )

            if normalized == "暂无可用信息":
                summary = "暂无可用信息"
                citation_ids: list[str] = []
            else:
                citation_ids = evidence_service.validate(
                    state,
                    summary,
                    task.evidence_ids,
                )

            # 验证通过后再提交正文及引用。
            old_summary = task.summary
            old_citation_ids = task.citation_ids

            task.summary = summary
            task.citation_ids = citation_ids

            if persist_note:
                if citation_ids:
                    try:
                        self._notes.save_task_note(
                            state,
                            task_id,
                        )
                    except Exception:
                        task.summary = old_summary
                        task.citation_ids = old_citation_ids
                        raise

                task.status = (
                    "completed"
                    if citation_ids
                    else "skipped"
                )

            # persist_note=False 时：
            # 正文已更新，但任务仍为 running。
            # 由工作流保存检查点、保存笔记，再标记 completed。

            logger.info(
                "摘要已生成 · %d 字 · 耗时=%.2fs",
                len(summary),
                perf_counter() - started_at,
                extra={
                    "event": "summary_completed",
                    "summary_chars": len(summary),
                    "citation_ids": task.citation_ids,
                    "note_id": task.note_id,
                    "persist_note": persist_note,
                    "duration_s": round(
                        perf_counter() - started_at,
                        3,
                    ),
                },
            )

            return summary

        except Exception:
            task.status = "failed"
            # 装饰器负责记录异常，避免重复打印堆栈。
            raise

        finally:
            if agent is not None:
                agent.clear_history()

                logger.debug(
                    "总结 Agent 历史已清理 · task_id=%d",
                    task_id,
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
                "历史笔记不是新证据，只能引用本轮允许的编号；"
"历史结论必须重新由本轮材料支持。"
            )
        return prompt
