"""组织和持久化笔记；关联字段与任务状态由图提交。"""

from __future__ import annotations

import logging

from deep_researcher.config import Configuration
from deep_researcher.logging_config import log_operation
from deep_researcher.models import SummaryState
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.tools.note_tool import Note, NoteTool

logger = logging.getLogger(__name__)


class NoteService:
    """保存任务和报告笔记；不推进研究状态。"""

    def __init__(
        self,
        config: Configuration,
        note_tool: NoteTool | None = None,
    ) -> None:
        self._tool: NoteTool | None = None

        if config.enable_notes:
            self._tool = note_tool if note_tool is not None else NoteTool(config)

    @log_operation(
        "保存任务笔记",
        task_parameter="task_id",
        state_parameter="state",
        checkpoint="save_note",
    )
    def save_task_note(
        self,
        state: SummaryState,
        task_id: int,
    ) -> Note | None:
        if self._tool is None:
            logger.debug(
                "笔记功能已关闭 · 跳过保存", extra={"event": "note_save_disabled"}
            )
            return

        task = state.get_task(task_id)

        if not task.summary or not task.summary.strip():
            raise ValueError(f"任务 {task_id} 没有有效摘要")
        references = EvidenceService().references(
            state,
            task.citation_ids,
        )
        content = (
            f"# 任务 {task.id}: {task.title}\n\n"
            f"研究主题：{state.research_topic or ''}\n\n"
            f"任务目标：{task.intent}\n\n"
            f"规划查询：{task.query}\n\n"
            f"检索查询：{task.effective_query}\n\n"
            f"## 摘要\n{task.summary}\n\n"
            f"## 引用证据\n"
            f"{references}"
        )

        saved = self._tool.save(
            run_id=state.run_id,
            task_id=task_id,
            title=f"任务 {task.id}: {task.title}",
            content=content,
        )

        logger.info(
            "任务笔记已保存 · %d 字",
            len(saved.content),
            extra={
                "event": "task_note_saved",
                "note_id": saved.note_id,
                "note_path": saved.note_path,
                "content_chars": len(saved.content),
            },
        )
        return saved

    @log_operation("保存报告笔记", state_parameter="state")
    def save_report_note(
        self,
        state: SummaryState,
    ) -> Note | None:
        if self._tool is None:
            logger.debug(
                "笔记功能已关闭 · 跳过报告保存", extra={"event": "note_save_disabled"}
            )
            return None

        if not state.structured_report or not state.structured_report.strip():
            raise ValueError("报告正文为空，无法保存笔记")

        saved = self._tool.save(
            run_id=state.run_id,
            title=(f"研究报告：{state.research_topic or '未命名主题'}"),
            content=state.structured_report,
        )

        logger.info(
            "报告笔记已保存 · %d 字",
            len(saved.content),
            extra={
                "event": "report_note_saved",
                "note_id": saved.note_id,
                "note_path": saved.note_path,
                "content_chars": len(saved.content),
            },
        )
        return saved
