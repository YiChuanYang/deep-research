"""组织研究笔记正文，并回填笔记关联。"""

from __future__ import annotations

import logging
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.config import Configuration
from deep_researcher.logging_config import log_operation
from deep_researcher.models import SummaryState
from deep_researcher.tools.note_tool import Note, NoteTool

logger = logging.getLogger(__name__)


class NoteService:
    """保持三个业务入口，统一使用 read/save。"""

    def __init__(
        self,
        config: Configuration,
        note_tool: NoteTool | None = None,
    ) -> None:
        self._tool: NoteTool | None = None

        if config.enable_notes:
            self._tool = (
                note_tool
                if note_tool is not None
                else NoteTool(config)
            )

    @log_operation(
        "读取任务笔记",
        task_parameter="task_id",
        state_parameter="state",
    )
    def load_task_note(
        self,
        state: SummaryState,
        task_id: int,
    ) -> str:
        if self._tool is None:
            logger.debug("笔记功能已关闭 · 跳过读取", extra={"event": "note_read_disabled"})
            return ""

        task = state.get_task(task_id)

        note = self._tool.read(
            run_id=state.run_id,
            task_id=task_id,
        )

        if note is None:
            if task.note_id:
                logger.warning(
                    "关联笔记缺失 · 将在保存时重建",
                    extra={"event": "note_link_missing", "note_id": task.note_id},
                )

            return ""


        logger.debug(
            "已有笔记已读取 · %d 字",
            len(note.content),
            extra={"event": "task_note_loaded", "note_id": note.note_id, "content_chars": len(note.content)},
        )

        return note.content

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
    ) -> Note|None:
        if self._tool is None:
            logger.debug("笔记功能已关闭 · 跳过保存", extra={"event": "note_save_disabled"})
            return

        task = state.get_task(task_id)

        if not task.summary or not task.summary.strip():
            raise ValueError(
                f"任务 {task_id} 没有有效摘要"
            )
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
    ) -> Note|None:
        if self._tool is None:
            logger.debug("笔记功能已关闭 · 跳过报告保存", extra={"event": "note_save_disabled"})
            return None

        if (
            not state.structured_report
            or not state.structured_report.strip()
        ):
            raise ValueError(
                "报告正文为空，无法保存笔记"
            )

        saved = self._tool.save(
            run_id=state.run_id,
            title=(
                f"研究报告："
                f"{state.research_topic or '未命名主题'}"
            ),
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
