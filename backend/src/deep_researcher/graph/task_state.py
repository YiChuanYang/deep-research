"""任务状态的提交规则；服务产出结果，由图调用这里更新业务状态。

pending -> running -> completed / skipped。
研究执行失败保存在运行元数据中；未完成任务保留 running，供检查点恢复。
本模块不请求模型、不写文件、不创建另一份持久化状态。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from deep_researcher.models import CitedText, EvidenceBatch, SummaryState, TodoItem

if TYPE_CHECKING:
    from deep_researcher.tools.note_tool import Note

Phase = Literal["rewrite", "lookup", "collect", "retrieve", "summarize"]

# 分支只能提交当前阶段负责的字段，不能覆盖其他任务或整个 payload。
_PHASE_FIELDS: dict[Phase, tuple[str, ...]] = {
    "rewrite": ("search_query", "status", "notices"),
    "lookup": ("retrieval", "notices", "status"),
    "collect": ("retrieval", "notices"),
    "retrieve": (),  # 选段已写资料库，全局证据由 parallel_register 登记。
    "summarize": ("summary", "citation_ids", "status", "notices"),
}


def start_task(task: TodoItem) -> None:
    """开始或恢复尚未结束的任务。"""
    if task.status in {"completed", "skipped"}:
        raise ValueError(f"任务 {task.id} 已结束，不能重复开始")
    task.status = "running"


def skip_task(task: TodoItem, reason: str) -> None:
    """跳过任务，并保留具体原因。"""
    task.status = "skipped"
    if reason not in task.notices:
        task.notices.append(reason)


def accept_evidence(
    state: SummaryState,
    task_id: int,
    batch: EvidenceBatch,
    *,
    missing_notice: str,
    keep_notices: bool = True,
) -> None:
    """接收完整合并目录；拒绝把单任务增量误当作全局目录覆盖。"""
    task = state.get_task(task_id)
    if not state.sources.keys() <= batch.sources.keys():
        raise ValueError("证据批次丢失已有来源")
    if not state.evidence.keys() <= batch.evidence.keys():
        raise ValueError("证据批次丢失已有证据")
    for evidence_id in batch.evidence_ids:
        item = batch.evidence.get(evidence_id)
        if (
            item is None
            or item.task_id != task_id
            or item.source_id not in batch.sources
        ):
            raise ValueError("证据批次的任务或来源关联无效")
    # EvidenceBatch 的两个目录是已有内容与新增内容的合并结果。
    state.sources = dict(batch.sources)
    state.evidence = dict(batch.evidence)
    task.evidence_ids = list(batch.evidence_ids)
    prior = task.notices if keep_notices else []
    task.notices = list(dict.fromkeys([*prior, *batch.notices]))
    if not task.evidence_ids:
        skip_task(task, missing_notice)


def accept_summary(task: TodoItem, result: CitedText) -> None:
    """接收已校验的摘要；有摘要不代表笔记已经保存或任务已经完成。"""
    if not set(result.citation_ids).issubset(task.evidence_ids):
        raise ValueError("摘要引用超出当前任务证据范围")
    task.summary = result.text
    task.citation_ids = list(result.citation_ids)
    if not task.citation_ids:
        skip_task(task, "无有效研究信息，未纳入报告")


def complete_task(task: TodoItem, saved_note: Note | None) -> None:
    """在笔记保存成功后调用；关闭笔记时也可以提交任务完成。"""
    if not task.summary or not task.citation_ids:
        raise ValueError("没有有效摘要和引用，不能完成任务")
    task.note_id = saved_note.note_id if saved_note is not None else None
    task.note_path = saved_note.note_path if saved_note is not None else None
    task.status = "completed"


def merge_task_result(task: TodoItem, result: dict[str, Any], phase: Phase) -> None:
    """按阶段字段白名单接收工作分支；保持现有检查点的产物格式。"""
    completed = TodoItem(**result["task"])
    if completed.id != task.id:
        raise ValueError("分支返回了其他任务的结果")
    for field in _PHASE_FIELDS[phase]:
        setattr(task, field, deepcopy(getattr(completed, field)))
