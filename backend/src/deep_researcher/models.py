"""研究状态，以及服务返回的临时结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# Literal 表示类型检查时允许的字符串集合。
# 它本身不会在运行时自动校验赋值。
TaskStatus = Literal[
    "pending",
    "running",
    "completed",
    "skipped",
]
@dataclass(kw_only=True)
class TodoItem:
    """一个研究任务，以及已经提交的任务结果。"""

    id: int
    title: str
    intent: str
    query: str
    status: TaskStatus = "pending"
    summary: str | None = None
    # 本任务允许使用的证据。
    evidence_ids: list[str] = field(default_factory=list)
    # 最终摘要实际使用的证据。
    citation_ids: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    note_id: str | None = None
    note_path: str | None = None

#证据引用类
@dataclass(kw_only=True, frozen=True)
class Source:
    """证据所属的来源页面。"""

    source_id: str
    url: str
    title: str


@dataclass(kw_only=True, frozen=True)
class Evidence:
    """实际提供给模型的材料片段。"""

    evidence_id: str
    source_id: str
    task_id: int
    text: str
    retrieved_at: str
    query: str
    backend: str

    kind: Literal["search_snippet", "page_excerpt"]

    truncated: bool = False

@dataclass(kw_only=True)
class SummaryState:
    """研究过程中需要持久化的业务数据。"""

    run_id: str = ""
    research_topic: str = ""

    todo_items: list[TodoItem] = field(default_factory=list)

    sources: dict[str, Source] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)

    structured_report: str | None = None
    report_citation_ids: list[str] = field(default_factory=list)

    report_note_id: str | None = None
    report_note_path: str | None = None

    def get_task(self, task_id: int) -> TodoItem:
        """根据任务编号取得任务；不存在时抛出异常。"""
        for task in self.todo_items:
            if task.id == task_id:
                return task

        raise KeyError(f"任务不存在：{task_id}")

@dataclass(kw_only=True)
class SummaryStateOutput:
    """研究入口返回给调用者的结果。"""

    run_id: str = ""
    report_markdown: str | None = None

    todo_items: list[TodoItem] = field(default_factory=list)
    sources: dict[str, Source] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)

    report_citation_ids: list[str] = field(default_factory=list)


@dataclass(kw_only=True, frozen=True)
class CitedText:
    """摘要或报告服务返回的正文及引用。"""

    text: str

    # tuple[str, ...] 表示任意长度的字符串元组。
    citation_ids: tuple[str, ...] = ()


@dataclass(kw_only=True)
class EvidenceBatch:
    """一次检索整理后的证据目录和本次选中的证据。"""

    sources: dict[str, Source]
    evidence: dict[str, Evidence]

    evidence_ids: list[str]
    notices: list[str] = field(default_factory=list)




