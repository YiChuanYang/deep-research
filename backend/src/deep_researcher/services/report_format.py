"""正式报告的程序附录，生成器与评测共用。"""

from __future__ import annotations

import re

from deep_researcher.models import SummaryState


def report_paragraphs(text: str) -> dict[str, str]:
    """评审和修订共用段落编号，P 编号仅用于输入定位，不写进最终正文。"""
    return {
        f"P{index:04d}": block
        for index, block in enumerate(re.split(r"\n\s*\n", text.strip()), 1)
        if block.strip()
    }


def report_suffix(state: SummaryState) -> str:
    rows: list[str] = []
    labels = {"pending": "本轮未执行", "running": "尚未完成", "skipped": "无有效资料"}
    for task in state.todo_items:
        if task.status != "completed":
            rows.append(f"- 任务 {task.id}「{task.title}」：{labels[task.status]}。")
    result = "\n\n## 研究范围说明\n\n" + "\n".join(rows) if rows else ""
    if state.report_limitations:
        result += "\n\n## 报告复核说明\n\n" + "\n".join(
            "- " + " ".join(note.split()) for note in state.report_limitations
        )
    return result
