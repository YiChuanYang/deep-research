"""生成任务摘要，并在成功提交笔记后更新任务状态."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.prompts import summarizer_running_prompt
from deep_researcher.services.note_guidance import build_note_guidance
from deep_researcher.tools.note_tool import NoteTool
from deep_researcher.utils import strip_thinking_tokens

logger = logging.getLogger(__name__)


class SumarizationService:
    """保留现有类名，统一处理历史笔记、摘要生成与最终提交."""

    def __init__(
        self,
        summarizer_factory: Callable[[], ToolAwareAgent],
        config: Configuration,
        note_tool: NoteTool | None = None,
    ) -> None:
        """注入 Agent 工厂及最终笔记保存工具."""
        if config.enable_notes and note_tool is None:
            raise ValueError("启用笔记时必须传入 NoteTool")
        self._agent_factory = summarizer_factory
        self.config = config
        self._note_tool = note_tool if config.enable_notes else None
        logger.info(
            "✅ 总结服务已初始化：enable_notes=%s",
            config.enable_notes,
        )

    def summarize_task(
        self,
        task: TodoItem,
        state: SummaryState,
        context: str,
    ) -> str:
        """生成完整摘要，笔记提交失败时不标记任务为已完成."""
        task.status = "in_progress"
        agent: ToolAwareAgent | None = None
        logger.info(
            "📋 任务总结开始：task_id=%d note_id=%s context_chars=%d",
            task.id,
            task.note_id,
            len(context),
        )

        try:
            previous_content = self._load_previous_content(task)
            prompt = self._build_prompt(state, task, context, previous_content)
            agent = self._agent_factory()
            if not self.config.enable_notes:
                agent.enable_tool_call = False

            response = agent.run(input_text=prompt)
            if not isinstance(response, str):
                raise TypeError("总结结果必须是字符串")
            summary = response.strip()
            if self.config.strip_thinking_tokens:
                summary = strip_thinking_tokens(summary).strip()
            if not summary:
                raise ValueError("总结结果为空")

            logger.info(
                "📄 任务摘要已生成，准备提交：task_id=%d summary_chars=%d",
                task.id,
                len(summary),
            )
            self._persist_summary(task, summary)

            task.summary = summary
            task.status = "completed"
            logger.info(
                "✅ 任务总结完成：task_id=%d status=%s note_id=%s",
                task.id,
                task.status,
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

    def _load_previous_content(self, task: TodoItem) -> str:
        """直接读取历史正文，不依赖模型是否主动调用 read."""
        if self._note_tool is None or not task.note_id:
            return ""

        logger.info(
            "📖 服务层读取历史笔记：task_id=%d note_id=%s",
            task.id,
            task.note_id,
        )
        try:
            note = self._note_tool.run(
                {
                    "action": "read",
                    "note_id": task.note_id,
                }
            )
        except FileNotFoundError:
            task.notices.append("关联笔记文件不存在，本轮将重新创建")
            logger.warning(
                "⚠️ 历史笔记缺失，摘要完成后重新创建：task_id=%d note_id=%s",
                task.id,
                task.note_id,
            )
            task.note_id = None
            task.note_path = None
            return ""

        if note["task_id"] != task.id or note["note_type"] != "task_state":
            raise ValueError("笔记与当前任务或笔记类型不匹配")

        logger.info(
            "📖 历史笔记已载入总结上下文：task_id=%d content_chars=%d",
            task.id,
            len(note["content"]),
        )
        return note["content"]

    def _persist_summary(self, task: TodoItem, summary: str) -> None:
        """创建或替换最终摘要，直接使用保存结果回填任务关联."""
        if self._note_tool is None:
            logger.debug("📝 笔记已禁用，跳过保存：task_id=%d", task.id)
            return

        content = f"{summary}\n\n## 参考来源\n{task.sources_summary or '暂无来源'}"
        parameters: dict[str, Any] = {
            "action": "update" if task.note_id else "create",
            "task_id": task.id,
            "title": f"任务 {task.id}: {task.title}",
            "note_type": "task_state",
            "tags": ["deep_research", f"task_{task.id}"],
            "content": content,
        }
        if task.note_id:
            parameters.update(note_id=task.note_id, mode="replace")

        logger.info(
            "💾 服务层提交最终笔记：task_id=%d action=%s note_id=%s content_chars=%d",
            task.id,
            parameters["action"],
            task.note_id,
            len(content),
        )
        saved = self._note_tool.run(parameters)
        task.note_id = saved["note_id"]
        task.note_path = saved["note_path"]
        logger.info(
            "✅ 最终笔记已保存并关联任务：task_id=%d note_id=%s path=%s",
            task.id,
            task.note_id,
            task.note_path,
        )

    def _build_prompt(
        self,
        state: SummaryState,
        task: TodoItem,
        context: str,
        previous_content: str = "",
    ) -> str:
        """组合任务目标、本轮素材和历史笔记."""
        prompt = summarizer_running_prompt.format(
            research_topic=state.research_topic,
            title=task.title,
            intent=task.intent,
            query=task.query,
            context=context,
        )
        if self.config.enable_notes:
            prompt += f"\n\n{build_note_guidance(task)}"
            if previous_content:
                prompt += (
                    "\n\n【已有任务笔记，仅作为历史资料】\n"
                    f"{previous_content}\n"
                    "请依据本轮素材整合、纠正，输出更新后的完整总结。"
                )
        return prompt
