"""Web RAG 的离线回归：正文尾部召回、降级、快照、恢复与本地补充。"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest
from openai.types.chat import ChatCompletion

from deep_researcher.api.manager import ResearchManager
from deep_researcher.config import Configuration
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import ResearchGraphState, decode_state
from deep_researcher.graph.report_cycle import update
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.rag.documents import chunk_page, clean_markdown, public_url
from deep_researcher.rag.pipeline import WebRagPipeline
from deep_researcher.rag.retrieval import EmbeddingClient, HybridRetriever, RankedChunk
from deep_researcher.rag.store import PageSnapshot, TextChunk, WebRagStore, digest
from deep_researcher.report_iteration import ReportDraft, ReportIteration, ReportReview, ReviewOutcome
from deep_researcher.services.report_reflection import ReportReflectionService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.review_protocol import requirement_map, resolve_requirements
from tests.test_report_iteration import TextAgent
from tests.test_streaming import client_for

RUN = "a" * 32
FACT = "事务性发件箱 transactional outbox 将发送意图和业务更新放在同一事务中，重试发送仍需接收方幂等。"
BODY = "# Guide\n\n" + "\n\n".join(f"安装说明第 {i} 项：准备运行环境与示例文件。" for i in range(100)) + "\n\n## Atomic delivery\n\n" + FACT


def config(tmp_path: Path, **options: Any) -> Configuration:
    return Configuration.model_validate({
        "web_rag_workspace": str(tmp_path / "rag"), "notes_workspace": str(tmp_path / "notes"),
        "enable_embeddings": False, "enable_rerank": False, "enable_notes": False,
        "enable_query_rewrite": False, "web_rag_top_k": 3, **options,
    })


def state() -> SummaryState:
    return SummaryState(run_id=RUN, research_topic="设计事务性发件箱",
                        todo_items=[TodoItem(id=1, title="outbox", intent="发送意图与业务更新如何保持原子性", query="outbox 发送意图")])


class Search:
    backend = "tavily"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"results": [{"url": "https://example.com/guide", "title": "Database guide",
                             "content": FACT}], "backend": "tavily", "notices": []}


def forbidden() -> MyLLM:
    raise AssertionError("本测试不允许发出模型请求")


class FakeEmbedding(EmbeddingClient):
    signature = "offline-semantic-test"

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def embed(self, texts: list[str], timeout: float) -> list[list[float]]:
        self.calls["embedded_texts"] += len(texts)
        return [[1.0, .1] if "通知" in t or "outbox" in t else [.1, 1.] for t in texts]

    def close(self) -> None:
        pass


class JsonModel:
    def __init__(self, values: list[Any]) -> None:
        self.values, self.requests = values, []
        self.closed = False

    def invoke(self, **kwargs: Any) -> ChatCompletion:
        self.requests.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return ChatCompletion.model_validate({"id": "test", "created": 0, "model": "offline",
            "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}}]})

    def close(self) -> None:
        self.closed = True


def pipeline(tmp_path: Path, **options: Any) -> tuple[WebRagPipeline, Search, list[list[str]]]:
    search = Search()
    reads: list[list[str]] = []

    def extract(urls: list[str]) -> dict[str, Any]:
        reads.append(urls)
        return {"results": [{"url": urls[0], "raw_content": BODY}], "failed_results": []}

    service = WebRagPipeline(config(tmp_path, **options), RUN, forbidden,
                             search_tool=cast(Any, search), extract=extract)
    return service, search, reads


def test_tail_evidence_is_retrieved_with_exact_snapshot_offsets(tmp_path: Path) -> None:
    service, search, reads = pipeline(tmp_path)
    s = state()
    before = asdict(s)
    service.discover(s, 1)
    service.read(1)
    service.index(1)
    batch = service.retrieve(s, 1)
    assert asdict(s) == before
    assert any("发送意图" in e.text and e.start > 2000 for e in batch.evidence.values())
    for e in batch.evidence.values():
        p = service.store.describe(e.snapshot_id)
        assert e.text == p.text[e.start:e.end]
        assert e.content_hash == digest(p.text)
    # 重启服务对象后阶段产物仍然可复用。
    restarted = WebRagPipeline(service.config, RUN, forbidden, search_tool=cast(Any, search),
                               extract=lambda _: pytest.fail("不得重复读取"))
    restarted.discover(s, 1)
    restarted.read(1)
    restarted.index(1)
    assert restarted.retrieve(s, 1).evidence_ids == batch.evidence_ids
    assert search.calls == 1 and len(reads) == 1


def test_second_task_reuses_full_page_and_preserves_old_evidence(tmp_path: Path) -> None:
    service, search, reads = pipeline(tmp_path)
    s = state()
    service.discover(s, 1); service.read(1); service.index(1)
    batch = service.retrieve(s, 1)
    s.sources, s.evidence = batch.sources, batch.evidence
    s.todo_items[0].evidence_ids = batch.evidence_ids
    previous = set(s.evidence)
    s.todo_items.append(TodoItem(id=2, title="通知", intent="通知重试", query="outbox"))
    service.discover(s, 2)
    result = service.read(2)
    service.index(2)
    second = service.retrieve(s, 2)
    assert result["cached_pages"] == 1 and len(reads) == 1
    assert previous.issubset(second.evidence)
    assert all(second.evidence[k].task_id == 2 for k in second.evidence_ids)


def test_extract_failure_preserves_snippet_and_exposes_limitation(tmp_path: Path) -> None:
    service, _, _ = pipeline(tmp_path)
    service.extract = lambda _: {"results": [], "failed_results": [{"url": "https://example.com/guide", "error": "timeout"}]}
    s = state()
    service.discover(s, 1)
    read = service.read(1)
    service.index(1)
    batch = service.retrieve(s, 1)
    assert read["snippet_pages"] == 1 and read["full_pages"] == 0
    assert batch.notices and batch.evidence_ids
    assert all(e.kind == "search_snippet" for e in batch.evidence.values())


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/", "https://u:p@example.com", "javascript:alert(1)"])
def test_nonpublic_source_rejected(url: str) -> None:
    assert public_url(url) is None


def test_chunks_preserve_text_and_heading_without_mutating_snapshot() -> None:
    text = clean_markdown("<script>malicious()</script>\n# A\n\n" + FACT * 12)
    page = PageSnapshot(snapshot_id="p", url="https://example.com", title="test", text=text,
                        retrieved_at="2026-01-01T00:00:00+00:00", kind="page_excerpt", content_hash=digest(text))
    chunks = chunk_page(page, 200, 30)
    assert len(chunks) > 1 and all(c.text == text[c.start:c.end] for c in chunks)
    assert all(c.section == "A" for c in chunks)
    assert "script" not in text


def test_semantic_retrieval_and_embedding_cache(tmp_path: Path) -> None:
    cfg = config(tmp_path, enable_embeddings=True, embedding_model="test-semantic", embedding_dimensions=2)
    store = WebRagStore(cfg.web_rag_workspace, RUN)
    calls: Counter[str] = Counter()
    retriever = HybridRetriever(cfg, store, forbidden, lambda: FakeEmbedding(calls))
    page = PageSnapshot(snapshot_id="p", url="https://example.com", title="Guide", text="", retrieved_at="", kind="page_excerpt", content_hash="")
    chunks = [TextChunk(chunk_id="a", snapshot_id="p", text="outbox durable dispatch", section="", start=0, end=23),
              TextChunk(chunk_id="b", snapshot_id="p", text="cache expiry", section="", start=30, end=42)]
    assert retriever.index(chunks)[0] == 2
    assert retriever.index(chunks)[0] == 2
    assert calls["embedded_texts"] == 2
    chosen, audit, notices = retriever.retrieve("通知", chunks, {"p": page}, question="如何通知")
    assert chosen[0].chunk.chunk_id == "a" and not notices
    assert audit[0]["cosine"] is not None and audit[0]["bm25"] == 0


@pytest.mark.parametrize("options", [
    {"web_rag_chunk_chars": 200, "web_rag_overlap_chars": 200},
    {"web_rag_candidates": 4, "web_rag_top_k": 5},
    {"embedding_dimensions": 256},
])
def test_inconsistent_web_rag_budgets_fail_at_config_load(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        config(tmp_path, **options)


def test_invalid_rerank_falls_back_without_dropping_candidates(tmp_path: Path) -> None:
    service, _, _ = pipeline(tmp_path, enable_rerank=True)
    invalid = {"scores": [{"id": "invented", "score": 3, "reason": "x"}]}
    model = JsonModel([invalid, invalid])
    service.retriever.llm_factory = lambda: cast(MyLLM, model)
    s = state(); service.discover(s, 1); service.read(1); service.index(1)
    batch = service.retrieve(s, 1)
    assert batch.evidence_ids and any("重排" in n for n in batch.notices)


def test_rerank_repairs_incomplete_candidate_list_once(tmp_path: Path) -> None:
    cfg = config(tmp_path, enable_rerank=True)
    store = WebRagStore(cfg.web_rag_workspace, RUN)
    page = PageSnapshot(snapshot_id="p", url="https://example.com", title="Guide", text="",
                        retrieved_at="", kind="page_excerpt", content_hash="")
    chunks = [
        TextChunk(chunk_id="a", snapshot_id="p", text="cache latency", section="", start=0, end=13),
        TextChunk(chunk_id="b", snapshot_id="p", text="cache freshness", section="", start=20, end=36),
    ]
    model = JsonModel([
        {"scores": [{"id": "C0", "score": 3, "reason": "direct"}]},
        {"scores": [
            {"id": "C0", "score": 3, "reason": "direct"},
            {"id": "C1", "score": 2, "reason": "partial"},
        ]},
    ])
    ranked = [RankedChunk(chunk=chunk) for chunk in chunks]

    HybridRetriever(cfg, store, lambda: cast(MyLLM, model))._rerank(
        "cache latency", ranked, {"p": page},
    )

    assert len(model.requests) == 2
    assert [item.rerank for item in ranked] == [3, 2]
    assert not model.closed


def test_requirement_ids_map_to_original_text_and_reject_unknown() -> None:
    req = requirement_map("解释事务。分析重试副作用。")
    data = resolve_requirements({"research_gaps": [{"requirement_id": "R02", "question": "重试"}]}, req)
    assert data["research_gaps"][0]["topic_quote"] == "分析重试副作用。"
    with pytest.raises(ValueError):
        resolve_requirements({"research_gaps": [{"requirement_id": ["bad"]}]}, req)


def graph_workflow(tmp_path: Path, *, fail_summary: bool = False, supplement: bool = False) -> tuple[GraphDeepResearcher, Counter[str]]:
    calls: Counter[str] = Counter()
    cfg = config(tmp_path, enable_report_reflection=supplement)
    search = Search()
    failed = False

    class Nodes(ResearchNodes):
        def plan(self, state: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            calls["plan"] += 1
            state.todo_items = [TodoItem(id=1, title="outbox", intent="atomicity", query="outbox")]
            return update(ReportIteration(initial_task_ids=[1])) if supplement else {}

        def rag(self, run_id: str) -> WebRagPipeline:
            if run_id not in self._rag:
                def extract(urls: list[str]) -> dict[str, Any]:
                    calls["read"] += 1
                    return {"results": [{"url": urls[0], "raw_content": BODY}]}
                p = WebRagPipeline(cfg, run_id, forbidden, search_tool=cast(Any, search), extract=extract)
                p.local_sufficient = lambda *args: True
                self._rag[run_id] = p
            return self._rag[run_id]

        def summarize(self, state: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            nonlocal failed
            if fail_summary and not failed:
                failed = True
                raise RuntimeError("summary unavailable")
            task = state.get_task(self.task_id(graph_state))
            task.summary = "发送意图与业务更新同一事务 [" + task.evidence_ids[0] + "]"
            task.citation_ids = task.evidence_ids[:1]
            return {}

        def report(self, state: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            calls["report"] += 1
            self.reporter_agent = lambda: cast(Any, TextAgent("# 报告\n\n" + "\n\n".join(t.summary for t in state.todo_items if t.summary)))
            return super().report(state, graph_state)

        def report_reflection_service(self) -> Any:
            class Review:
                def review(self, *args: Any, **kwargs: Any) -> ReviewOutcome:
                    calls["review"] += 1
                    items = [] if calls["review"] > 1 else [{"kind": "missing_evidence", "reason": "补充通知机制",
                        "question": "outbox 通知如何持久化？", "query": "outbox 通知"}]
                    return ReviewOutcome(review=ReportReview.model_validate({"issues": items}))
            return Review()

    return GraphDeepResearcher(cfg, data_dir=tmp_path / "graph", nodes_factory=Nodes), calls


def test_graph_recovery_reuses_discovery_pages_and_index(tmp_path: Path) -> None:
    researcher, calls = graph_workflow(tmp_path, fail_summary=True)
    with pytest.raises(RuntimeError, match="summary unavailable"):
        researcher.run("发件箱研究", max_tasks=1)
    run_id = researcher.last_run_id
    snapshot = researcher.read_run(run_id)
    assert snapshot.next_nodes == ("parallel_summarize",)
    assert snapshot.state.todo_items[0].retrieval["stage"] == "retrieved"
    result = researcher.resume(run_id)
    assert result.report_markdown and calls["plan"] == 1 and calls["read"] == 1


def test_supplement_retrieves_existing_corpus_before_web_search(tmp_path: Path) -> None:
    researcher, calls = graph_workflow(tmp_path, supplement=True)
    result = researcher.run("发件箱研究", max_tasks=1)
    snapshot = researcher.read_run(result.run_id)
    assert len(result.todo_items) == 2 and calls["read"] == 1
    assert result.todo_items[1].retrieval["local_sufficient"] is True
    assert snapshot.report_iteration.status == "accepted"


def test_api_returns_snapshot_and_delete_cleans_research_corpus(tmp_path: Path) -> None:
    researcher, _ = graph_workflow(tmp_path)
    result = researcher.run("发件箱研究")
    manager = ResearchManager(researcher_factory=lambda: researcher)
    try:
        with client_for(manager) as client:
            data = client.get(f"/api/research/{result.run_id}/result").json()
            citation = data["citations"][0]
            response = client.get(f"/api/research/{result.run_id}/evidence/{citation['evidence_id']}/snapshot")
            page = response.json()
            assert response.status_code == 200
            assert page["text"][citation["start"]:citation["end"]] == citation["text"]
            assert client.get(f"/api/research/{result.run_id}/retrieval/1").status_code == 200
            assert client.delete(f"/api/research/{result.run_id}").status_code == 204
            assert not (tmp_path / "rag" / result.run_id).exists()
    finally:
        manager.close()


def test_report_can_use_selected_evidence_not_used_by_summary(tmp_path: Path) -> None:
    service, _, _ = pipeline(tmp_path)
    s = state(); service.discover(s, 1); service.read(1); service.index(1)
    batch = service.retrieve(s, 1)
    s.sources, s.evidence = batch.sources, batch.evidence
    task = s.todo_items[0]
    task.status, task.evidence_ids, task.retrieval = "completed", batch.evidence_ids, {"stage": "retrieved"}
    task.citation_ids, task.summary = batch.evidence_ids[:1], "摘要 [" + batch.evidence_ids[0] + "]"
    text = "# Report\n\n" + "".join(f"[{k}]" for k in batch.evidence_ids)
    result = ReportingService(lambda: cast(Any, TextAgent(text))).generate_report(s)
    assert set(result.citation_ids) == set(batch.evidence_ids)


def test_old_snapshot_preserves_old_workflow(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    old = {"llm_provider": cfg.llm_provider, "llm_base_url": cfg.llm_base_url}
    restored = cfg.for_resume(old)
    assert not restored.enable_web_rag and restored.review_output_mode == "tool"
    assert not restored.enable_parallel_tasks
    assert "embedding_api_key" not in cfg.snapshot()
