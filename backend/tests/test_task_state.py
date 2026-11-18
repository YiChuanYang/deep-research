"""任务状态提交边界：目录不丢失、持久化失败不完成、分支不能越权覆盖。"""

from dataclasses import asdict

import pytest

from deep_researcher.config import Configuration
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_state import ResearchGraphState, encode_state
from deep_researcher.graph.task_state import (
    accept_evidence,
    accept_summary,
    complete_task,
    merge_task_result,
)
from deep_researcher.models import CitedText, EvidenceBatch, SummaryState, TodoItem
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.services.note_service import NoteService


def make_state() -> SummaryState:
    return SummaryState(
        run_id="a" * 32,
        research_topic="主题",
        todo_items=[
            TodoItem(id=i, title=f"任务 {i}", intent="目标", query=f"查询 {i}")
            for i in (1, 2)
        ],
    )


def collect(state: SummaryState, task_id: int) -> EvidenceBatch:
    return EvidenceService().collect(
        state,
        task_id,
        [{"url": f"https://example.com/{task_id}", "content": "已保存的材料"}],
        backend="offline",
        max_chars=100,
        prefer_full_page=False,
    )


def test_evidence_accumulates_across_tasks() -> None:
    state = make_state()
    first = collect(state, 1)
    accept_evidence(state, 1, first, missing_notice="无材料")
    second = collect(state, 2)
    accept_evidence(state, 2, second, missing_notice="无材料")

    assert len(state.sources) == len(state.evidence) == 2
    assert state.get_task(1).evidence_ids == first.evidence_ids
    assert state.get_task(2).evidence_ids == second.evidence_ids


@pytest.mark.parametrize("invalid_kind", ["lost_catalog", "wrong_task"])
def test_invalid_evidence_is_rejected_before_mutation(invalid_kind: str) -> None:
    state = make_state()
    first = collect(state, 1)
    accept_evidence(state, 1, first, missing_notice="无材料")
    before = asdict(state)
    invalid = (
        EvidenceBatch(sources={}, evidence={}, evidence_ids=[])
        if invalid_kind == "lost_catalog"
        else first
    )
    with pytest.raises(ValueError):
        accept_evidence(state, 2, invalid, missing_notice="无材料")
    assert asdict(state) == before


def test_summary_does_not_complete_task_and_rejects_unknown_citation() -> None:
    task = make_state().get_task(1)
    task.status = "running"
    task.evidence_ids = ["E0001"]
    before = asdict(task)
    with pytest.raises(ValueError):
        accept_summary(task, CitedText(text="错误引用", citation_ids=("E9999",)))
    assert asdict(task) == before

    accept_summary(task, CitedText(text="有效摘要 [E0001]", citation_ids=("E0001",)))
    assert task.status == "running"
    complete_task(task, None)  # 关闭笔记时，仍可完成有效任务。
    assert task.status == "completed"
    assert task.note_id is None


def test_note_failure_keeps_task_unfinished(monkeypatch: pytest.MonkeyPatch) -> None:
    state = make_state()
    task = state.get_task(1)
    task.status = "running"
    task.summary = "有效摘要 [E0001]"
    task.evidence_ids = task.citation_ids = ["E0001"]
    values: ResearchGraphState = {
        "version": 1,
        "payload": encode_state(state),
        "active_task_id": 1,
        "max_tasks": None,
        "finished": False,
    }

    def fail_save(self: NoteService, state: SummaryState, task_id: int) -> None:
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(NoteService, "save_task_note", fail_save)
    nodes = ResearchNodes(Configuration(enable_notes=False), "offline")
    with pytest.raises(OSError, match="模拟磁盘写入失败"):
        nodes.save_note(state, values)
    assert task.status == "running"
    assert task.note_id is None


def test_parallel_merge_only_accepts_current_phase_fields() -> None:
    task = make_state().get_task(1)
    task.evidence_ids = ["E0001"]
    result = {"task": {**asdict(task), "search_query": "实际查询", "title": "错误标题"}}
    result["task"]["evidence_ids"] = ["E9999"]
    result["task"]["notices"] = ["查询已改写"]

    merge_task_result(task, result, "rewrite")
    assert task.search_query == "实际查询"
    assert task.title == "任务 1"
    assert task.evidence_ids == ["E0001"]
    result["task"]["notices"].append("后续修改")
    assert task.notices == ["查询已改写"]

    result["task"]["id"] = 2
    before = asdict(task)
    with pytest.raises(ValueError):
        merge_task_result(task, result, "rewrite")
    assert asdict(task) == before
