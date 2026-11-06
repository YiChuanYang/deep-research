"""Persist task notes and the final research report."""

from __future__ import annotations

import logging

from deep_researcher.config import Configuration
from deep_researcher.models import SummaryState
from deep_researcher.tools.note_tool import NoteTool

logger = logging.getLogger(__name__)


class NoteService:
    """Own NoteTool operations and synchronize note metadata into state."""

    def __init__(
        self,
        config: Configuration,
        note_tool: NoteTool | None = None,
    ) -> None:
        self._tool = (
            note_tool or NoteTool(config=config)
            if config.enable_notes
            else None
        )

    def load_task_note(self, state: SummaryState, task_id: int) -> str:
        """Return existing task-note content, or an empty string for a new task."""
        task = state.get_task(task_id)
        if self._tool is None or not task.note_id:
            return ""

        try:
            note = self._tool.run({
                "action": "read",
                "note_id": task.note_id,
            })
        except FileNotFoundError:
            task.notices.append("任务笔记文件不存在，将在总结后重新创建")
            task.note_id = None
            task.note_path = None
            logger.warning(
                "⚠️ 任务笔记不存在，清除旧关联：task_id=%d",
                task_id,
            )
            return ""

        if note["task_id"] != task_id or note["note_type"] != "task_state":
            raise ValueError(f"任务 {task_id} 关联的笔记元数据不匹配")

        logger.info(
            "📖 已读取任务笔记：task_id=%d note_id=%s chars=%d",
            task_id,
            task.note_id,
            len(note["content"]),
        )
        return note["content"]

    def save_task_note(self, state: SummaryState, task_id: int) -> None:
        """Create or replace the complete note for a summarized task."""
        if self._tool is None:
            return

        task = state.get_task(task_id)
        if not task.summary:
            raise ValueError(f"任务 {task_id} 没有摘要，不能保存笔记")

        content = (
            f"# 任务 {task.id}: {task.title}\n\n"
            f"研究主题：{state.research_topic or ''}\n\n"
            f"任务目标：{task.intent}\n\n"
            f"检索查询：{task.query}\n\n"
            f"## 摘要\n{task.summary}\n\n"
            f"## 参考来源\n{task.sources_summary or '暂无来源'}"
        )
        parameters: dict[str, object] = {
            "action": "update" if task.note_id else "create",
            "task_id": task.id,
            "title": f"任务 {task.id}: {task.title}",
            "note_type": "task_state",
            "tags": ["deep_research", f"task_{task.id}"],
            "content": content,
        }
        if task.note_id:
            parameters["note_id"] = task.note_id
            parameters["mode"] = "replace"

        saved = self._tool.run(parameters)
        task.note_id = saved["note_id"]
        task.note_path = saved["note_path"]
        logger.info(
            "📝 任务笔记已保存：task_id=%d note_id=%s",
            task_id,
            task.note_id,
        )

    def save_report_note(self, state: SummaryState) -> None:
        """Create or replace a conclusion note for the final report."""
        if self._tool is None or not state.structured_report:
            return

        parameters: dict[str, object] = {
            "action": "update" if state.report_note_id else "create",
            "title": f"研究报告：{state.research_topic or '未命名主题'}",
            "note_type": "conclusion",
            "tags": ["deep_research", "report"],
            "content": state.structured_report,
        }
        if state.report_note_id:
            parameters["note_id"] = state.report_note_id
            parameters["mode"] = "replace"

        saved = self._tool.run(parameters)
        state.report_note_id = saved["note_id"]
        state.report_note_path = saved["note_path"]
        logger.info(
            f"📝 最终报告笔记已保存：note_id={state.report_note_id},path={state.report_note_path}")
