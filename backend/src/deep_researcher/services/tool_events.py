"""将成功的笔记工具调用同步到研究任务或报告状态."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from deep_researcher.models import SummaryState

logger = logging.getLogger(__name__)


class ToolCallTracker:
    """只同步笔记关联，不将工具成功等同于任务总结完成."""

    def __init__(self, state: SummaryState, notes_workspace: str) -> None:
        """绑定当前研究状态及笔记工作目录."""
        self._state = state
        self._workspace = Path(notes_workspace).resolve()
        logger.info(
            "👂 笔记监听器已初始化：workspace=%s",
            self._workspace,
        )

    def record(self, event: dict[str, Any]) -> None:
        """从工具实际返回的结构化记录中获取任务编号和笔记编号."""
        tool_name = event.get("tool_name")
        call_id = event.get("tool_call_id")
        if tool_name != "note" or not event.get("success"):
            logger.debug(
                "🪝 跳过笔记关联：tool=%s call_id=%s success=%s",
                tool_name,
                call_id,
                event.get("success"),
            )
            return

        raw_result = event.get("result")
        data: Any = (
            json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        )
        if not isinstance(data, dict):
            raise ValueError("NoteTool 必须返回结构化笔记结果")

        note_id = data.get("note_id")
        if (
            not isinstance(note_id, str)
            or re.fullmatch(r"note_[0-9a-f]{12}", note_id) is None
        ):
            raise ValueError("NoteTool 返回的 note_id 无效")

        note_path = data.get("note_path")
        if not isinstance(note_path, str) or not note_path:
            raise ValueError("NoteTool 返回的 note_path 无效")

        if data.get("note_type") == "conclusion":
            self._state.report_note_id = note_id
            self._state.report_note_path = note_path
            logger.info(
                "🪝 报告笔记关联已同步：action=%s call_id=%s note_id=%s",
                data.get("action"),
                call_id,
                note_id,
            )
            return

        task_id = data.get("task_id")
        if type(task_id) is not int:
            logger.warning(
                "⚠️ 笔记缺少有效任务编号，未同步任务：call_id=%s note_id=%s",
                call_id,
                note_id,
            )
            return

        for task in self._state.todo_items:
            if task.id == task_id:
                task.note_id = note_id
                task.note_path = note_path
                logger.info(
                    "🪝 任务笔记关联已同步：action=%s call_id=%s task_id=%d note_id=%s",
                    data.get("action"),
                    call_id,
                    task_id,
                    note_id,
                )
                return

        logger.warning(
            "⚠️ 未找到笔记对应任务：call_id=%s task_id=%d note_id=%s",
            call_id,
            task_id,
            note_id,
        )
