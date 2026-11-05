import re
from pathlib import Path
from typing import Any

from deep_researcher.models import SummaryState


class ToolCallTracker:
    def __init__(self, state: SummaryState, notes_workspace: str) -> None:
        self._state = state
        self._workspace = Path(notes_workspace)

    def record(self, event: dict[str, Any]) -> None:
        if not event.get("success") or event.get("tool_name") != "note":
            return

        parameters = event.get("clean_parameters") or {}
        if not isinstance(parameters, dict):
            return

        action = parameters.get("action")
        note_id = parameters.get("note_id")

        if action == "create":
            result = str(event.get("result") or "")
            match = re.search(r"note_id=(note_[0-9a-f]{12})", result)
            if match:
                note_id = match.group(1)

        if not note_id:
            return

        task_id = parameters.get("task_id")
        if task_id is not None:
            try:
                task_id = int(task_id)
            except (TypeError, ValueError):
                task_id = None

        if task_id is not None:
            for task in self._state.todo_items:
                if task.id == task_id:
                    task.note_id = str(note_id)
                    task.note_path = str(self._workspace / f"{note_id}.json")
                    break

        tags = parameters.get("tags") or []
        if (
            parameters.get("note_type") == "conclusion"
            or "report" in tags
        ):
            self._state.report_note_id = str(note_id)
            self._state.report_note_path = str(
                self._workspace / f"{note_id}.json"
            )