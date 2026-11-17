"""真实 LangGraph/SQLite 的并发回归；外部请求使用受控延迟与固定证据。"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BoundedSemaphore, Lock
from time import sleep
from typing import Any, Iterator, cast
from types import SimpleNamespace

import pytest

from deep_researcher.config import Configuration
from deep_researcher.graph import report_cycle
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import ResearchGraphState, merge_task_results
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.llm.llm import MyLLM
from deep_researcher.rag.pipeline import WebRagPipeline
from deep_researcher.report_iteration import ReportIteration, ReportReview, ReviewOutcome
from evals.checks import inspect_state
from tests.test_report_iteration import TextAgent
from tests.test_streaming import client_for
from deep_researcher.api.manager import ResearchManager


class Activity:
    def __init__(self) -> None:
        self.lock = Lock()
        self.calls: Counter[str] = Counter()
        self.active: Counter[str] = Counter()
        self.peak: Counter[str] = Counter()
        self.page_sets: list[tuple[str, ...]] = []
        self.summary_evidence: list[dict[str, int]] = []
        self.failed = False
        self.barrier: Barrier | None = None

    @contextmanager
    def operation(self, phase: str, task: str) -> Iterator[None]:
        with self.lock:
            self.calls[f"{phase}:{task}"] += 1
            self.active[phase] += 1
            self.peak[phase] = max(self.peak[phase], self.active[phase])
        try:
            sleep(.04)
            yield
        finally:
            with self.lock:
                self.active[phase] -= 1


def workflow(tmp_path: Path, *, fail_summary: bool = False, supplement: bool = False,
             concurrency: int = 2, network: int = 2, parallel: bool = True,
             no_results: bool = False) -> tuple[GraphDeepResearcher, Activity]:
    activity = Activity()
    cfg = Configuration(enable_parallel_tasks=parallel, task_concurrency=concurrency,
                        network_concurrency=network, enable_embeddings=False, enable_rerank=False,
                        enable_query_rewrite=False, enable_notes=True, enable_report_reflection=supplement,
                        report_max_supplement_tasks=2, report_max_revisions=2,
                        notes_workspace=str(tmp_path / "notes"), web_rag_workspace=str(tmp_path / "rag"))

    class Search:
        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            query = parameters["input"]
            number = query.split()[-1]
            with activity.operation("search", number):
                if activity.barrier is not None and number in {"1", "2"}:
                    activity.barrier.wait(timeout=4)
                return {"backend": "tavily", "notices": [], "results": [] if no_results else [
                    {"url": f"https://example.com/{number}", "title": f"Atomic durability {number}",
                     "content": "Atomic durability requires a transaction and idempotence."},
                ]}

    def extract(urls: list[str]) -> dict[str, Any]:
        with activity.operation("read", urls[0].rsplit("/", 1)[-1]):
            return {"results": [{"url": url, "raw_content": "# Atomic durability\n\n"
                    "Atomic durability requires a transaction and idempotence. " + url} for url in urls]}

    class Pipeline(WebRagPipeline):
        def select_evidence(self, state: SummaryState, task_id: int, **kwargs: Any) -> dict[str, Any]:
            with activity.operation("retrieve", str(task_id)):
                if kwargs.get("page_ids") is not None:
                    activity.page_sets.append(tuple(kwargs["page_ids"]))
                return super().select_evidence(state, task_id, **kwargs)

        def local_sufficient(self, state: SummaryState, task_id: int, batch: Any) -> bool:
            return bool(batch.evidence_ids) and task_id == 4

    class Nodes(ResearchNodes):
        def plan(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
            activity.calls["plan"] += 1
            state.todo_items = [TodoItem(id=i, title=f"Durability {i}", intent="Explain atomic durability",
                                       query=f"atomic durability {i}") for i in range(1, 4)]
            return report_cycle.update(ReportIteration(initial_task_ids=report_cycle.selected_ids(state, values))) if supplement else {}

        def rag(self, run_id: str) -> WebRagPipeline:
            if run_id not in self._rag:
                self._rag[run_id] = Pipeline(self.config, run_id, self.llm,
                                             search_tool=cast(Any, Search()), extract=extract)
            return self._rag[run_id]

        def summarize(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
            task = state.get_task(self.task_id(values))
            with activity.operation("summary", str(task.id)):
                if fail_summary and task.id == 2 and not activity.failed:
                    sleep(.15)  # 让同一步的成功分支先完成并持久化。
                    activity.failed = True
                    raise RuntimeError("task 2 summary failed")
                if parallel:
                    activity.summary_evidence.append({key: item.task_id for key, item in state.evidence.items()})
                task.summary = f"Task {task.id} uses atomic transactions [{task.evidence_ids[0]}]"
                task.citation_ids = task.evidence_ids[:1]
            return {}

        def report(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
            activity.calls["report"] += 1
            self.reporter_agent = lambda: cast(Any, TextAgent(
                "# Report\n\n" + "\n\n".join(t.summary for t in state.todo_items if t.summary)))
            return super().report(state, values)

        def report_reflection_service(self) -> Any:
            class Review:
                def review(self, *args: Any, **kwargs: Any) -> ReviewOutcome:
                    activity.calls["review"] += 1
                    issues = [] if activity.calls["review"] > 1 else [
                        {"kind": "missing_evidence", "reason": f"Need specific failure {i}",
                         "question": f"How does atomic durability handle failure {i}?", "query": f"atomic durability {i}"}
                        for i in (4, 5)
                    ]
                    return ReviewOutcome(review=ReportReview.model_validate({"issues": issues}))
            return Review()

    return GraphDeepResearcher(cfg, data_dir=tmp_path / "graph", nodes_factory=Nodes), activity


def test_parallel_branches_overlap_and_merge_ids_deterministically(tmp_path: Path) -> None:
    researcher, activity = workflow(tmp_path)
    # 如果仍串行，第一个搜索会超时；这个屏障证明两个分支实际重叠执行。
    activity.barrier = Barrier(2)
    result = researcher.run("Explain atomic durability", max_tasks=3)
    snapshot = researcher.read_run(result.run_id)
    assert activity.peak["search"] == activity.peak["summary"] == activity.peak["retrieve"] == 2
    assert snapshot.report_ready and snapshot.batch_task_ids == []
    assert len(set(activity.page_sets)) == 1 and len(activity.page_sets[0]) == 3
    assert len(activity.summary_evidence) == 3
    assert all(x == activity.summary_evidence[0] for x in activity.summary_evidence)
    assert [e.task_id for e in result.evidence.values()] == sorted(e.task_id for e in result.evidence.values())
    for task in result.todo_items:
        assert task.status == "completed" and task.note_path and Path(task.note_path).exists()
        assert set(task.citation_ids).issubset(task.evidence_ids)
        assert all(result.evidence[e].task_id == task.id for e in task.evidence_ids)
    assert inspect_state(snapshot.state)["overall_check"]["status"] == "pass"


def test_failed_branch_resume_reuses_successful_siblings(tmp_path: Path) -> None:
    researcher, activity = workflow(tmp_path, fail_summary=True)
    with pytest.raises(RuntimeError, match="task 2"):
        researcher.run("Explain atomic durability", max_tasks=3)
    run_id = researcher.last_run_id
    before = researcher.read_run(run_id)
    assert "parallel_summarize" in before.next_nodes and before.batch_task_ids == [1, 2, 3]
    evidence_before = dict(before.state.evidence)
    successful_calls = activity.calls["summary:1"]
    result = researcher.resume(run_id)
    assert activity.calls["summary:1"] == successful_calls == 1
    assert activity.calls["summary:2"] == 2
    assert activity.calls["plan"] == 1
    assert all(activity.calls[f"search:{i}"] == 1 for i in range(1, 4))
    assert result.evidence == evidence_before
    assert researcher.read_run(run_id).report_ready


def test_supplement_batch_reuses_local_and_searches_only_missing(tmp_path: Path) -> None:
    researcher, activity = workflow(tmp_path, supplement=True)
    result = researcher.run("Explain atomic durability")
    snapshot = researcher.read_run(result.run_id)
    assert snapshot.report_iteration.status == "accepted"
    assert len(result.todo_items) == 5 and all(t.status == "completed" for t in result.todo_items)
    assert result.todo_items[3].retrieval["local_sufficient"] is True
    assert activity.calls["search:4"] == 0 and activity.calls["search:5"] == 1
    assert all(s.search_completed for s in snapshot.report_iteration.supplements)
    assert activity.calls["report"] == 2 and activity.calls["review"] == 2


@pytest.mark.parametrize("concurrency,network", [(1, 2), (3, 1)])
def test_limits_and_max_tasks_are_honored(tmp_path: Path, concurrency: int, network: int) -> None:
    researcher, activity = workflow(tmp_path, concurrency=concurrency, network=network)
    result = researcher.run("Atomic durability", max_tasks=2)
    assert activity.peak["search"] == 1
    assert activity.peak["summary"] <= concurrency
    assert result.todo_items[2].status == "pending" and activity.calls["search:3"] == 0


def test_empty_retrieval_skips_tasks_without_calling_summaries(tmp_path: Path) -> None:
    researcher, activity = workflow(tmp_path, no_results=True)
    result = researcher.run("Atomic durability")
    assert all(t.status == "skipped" for t in result.todo_items)
    assert not any(key.startswith("summary:") for key in activity.calls)
    assert "资料" in result.report_markdown and researcher.read_run(result.run_id).report_ready


def test_legacy_serial_run_keeps_original_nodes_on_resume(tmp_path: Path) -> None:
    researcher, activity = workflow(tmp_path, parallel=False, fail_summary=True)
    with pytest.raises(RuntimeError):
        researcher.run("Atomic durability", max_tasks=2)
    before = researcher.read_run(researcher.last_run_id)
    assert before.next_nodes == ("summarize",)
    assert before.info.config["enable_parallel_tasks"] is False
    researcher.resume(researcher.last_run_id)
    assert activity.peak["search"] == activity.peak["summary"] == 1


def test_reducer_replay_is_idempotent_and_conflict_is_rejected() -> None:
    first = {"b:rewrite:1": {"task": {"id": 1}}}
    assert merge_task_results(first, first) == first
    assert len(merge_task_results(first, {"b:rewrite:2": {"task": {"id": 2}}})) == 2
    with pytest.raises(ValueError, match="冲突"):
        merge_task_results(first, {"b:rewrite:1": {"task": {"id": 3}}})


def test_api_reports_batch_tasks_and_task_events(tmp_path: Path) -> None:
    researcher, _ = workflow(tmp_path, fail_summary=True)
    with pytest.raises(RuntimeError):
        researcher.run("Atomic durability", max_tasks=2)
    manager = ResearchManager(researcher_factory=lambda: researcher)
    try:
        with client_for(manager) as client:
            run_id = researcher.last_run_id
            response = client.get(f"/api/research/{run_id}")
            assert response.status_code == 200
            assert response.json()["batch_task_ids"] == [1, 2]
    finally:
        manager.close()


def test_model_request_gate_limits_calls_and_releases_on_failure() -> None:
    activity = Activity()
    llm = object.__new__(MyLLM)
    llm.config = Configuration()
    llm.model, llm.llm_provider = "fake", "fake"
    llm._request_gate = BoundedSemaphore(2)

    def create(**kwargs: Any) -> Any:
        number = kwargs["messages"][0]["content"]
        with activity.operation("model", number):
            if number == "fail":
                raise RuntimeError("provider failed")
            return SimpleNamespace(choices=[object()], usage=None)

    llm.client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))

    def invoke(number: str) -> bool:
        try:
            llm.invoke([{"role": "user", "content": number}])
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(invoke, ["1", "fail", "3", "4"]))
    assert outcomes == [True, False, True, True] and activity.peak["model"] == 2
    assert invoke("after-failure") is True


def test_selected_chunks_are_frozen_and_registering_does_not_rerank(tmp_path: Path) -> None:
    from tests.test_web_rag import pipeline, state

    service, _, _ = pipeline(tmp_path)
    current = state()
    service.discover(current, 1); service.read(1); service.index(1)
    pages = [p.snapshot_id for p in service.store.pages()]
    service.select_evidence(current, 1, page_ids=pages)
    # 选段阶段没有分配任何业务证据编号。
    assert not current.evidence and not current.sources
    with pytest.raises(ValueError, match="语料版本"):
        service.select_evidence(current, 1, page_ids=[])
    service.retriever.retrieve = lambda *args, **kwargs: pytest.fail("登记阶段不应再次调用重排")
    batch = service.register_evidence(current, 1)
    assert batch.evidence_ids


def test_parallel_config_is_frozen_and_old_runs_remain_serial() -> None:
    cfg = Configuration(enable_parallel_tasks=True, task_concurrency=3, model_concurrency=1)
    changed = Configuration(enable_parallel_tasks=False, task_concurrency=1, model_concurrency=2)
    restored = changed.for_resume(cfg.snapshot())
    assert restored.enable_parallel_tasks and restored.task_concurrency == 3 and restored.model_concurrency == 1
    legacy = cfg.snapshot()
    legacy.pop("enable_parallel_tasks")
    assert cfg.for_resume(legacy).enable_parallel_tasks is False
