"""提速改动的行为回归：缓存失效、批量 SQL、重排校验和实际设备选择。"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from rank_bm25 import BM25Okapi

from deep_researcher.llm.llm import MyLLM
from deep_researcher.llm.structured import ModelOutputError
from deep_researcher.rag.embeddings import local_embedding
from deep_researcher.rag.retrieval import HybridRetriever, RankedChunk, tokenize
from deep_researcher.rag.store import PageSnapshot, TextChunk, WebRagStore
from deep_researcher.rag.timing import rag_timer
from deep_researcher.logging_config import log_context
from evals.telemetry import collect_telemetry
from tests.test_web_rag import FakeEmbedding, JsonModel, RUN, config, forbidden, pipeline, state


def corpus() -> tuple[list[TextChunk], dict[str, PageSnapshot]]:
    page = PageSnapshot(snapshot_id="p", url="https://example.com", title="Guide", text="",
                        retrieved_at="", kind="page_excerpt", content_hash="")
    chunks = [TextChunk(chunk_id="a", snapshot_id="p", text="outbox durable dispatch", section="", start=0, end=23),
              TextChunk(chunk_id="b", snapshot_id="p", text="cache expiry", section="", start=30, end=42)]
    return chunks, {"p": page}


def test_bm25_cache_rebuilds_on_corpus_order_content_and_title(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    retriever = HybridRetriever(cfg, WebRagStore(cfg.web_rag_workspace, RUN), forbidden)
    chunks, pages = corpus()
    first, _ = retriever._lexical_index(chunks, pages)
    assert retriever._lexical_index(chunks, pages)[0] is first
    variations = [
        (list(reversed(chunks)), pages),
        ([*chunks, replace(chunks[0], chunk_id="c", text="new outbox")], pages),
        ([replace(chunks[0], text="changed"), chunks[1]], pages),
        (chunks, {"p": replace(pages["p"], title="Changed")}),
    ]
    for items, sources in variations:
        current, tokens = retriever._lexical_index(items, sources)
        assert current is not first
        expected = BM25Okapi([list(tokenize(sources[c.snapshot_id].title + " " + c.section + " " + c.text)) or ["_"] for c in items])
        np.testing.assert_allclose(current.get_scores(tokenize("outbox")), expected.get_scores(tokenize("outbox")))
        assert len(tokens) == len(items)
        first = current


def test_query_cache_survives_restart_but_changes_with_query_and_model(tmp_path: Path) -> None:
    cfg = config(tmp_path, enable_embeddings=True, embedding_model="test", embedding_dimensions=2)
    store = WebRagStore(cfg.web_rag_workspace, RUN)
    calls: Counter[str] = Counter()
    factory = lambda: FakeEmbedding(calls)
    retriever = HybridRetriever(cfg, store, forbidden, factory)
    chunks, pages = corpus()
    # 重复文本只算一次，不同 chunk 仍有自己的证据关联。
    repeated = [*chunks, replace(chunks[0], chunk_id="duplicate")]
    assert retriever.index(repeated)[0] == 3
    assert calls["embedded_texts"] == 2
    first = retriever.retrieve("通知", chunks, pages, question="通知")
    assert calls["embedded_texts"] == 3
    restarted = HybridRetriever(cfg, store, forbidden, factory)
    assert restarted.retrieve("通知", chunks, pages, question="通知")[1] == first[1]
    assert calls["embedded_texts"] == 3
    restarted.retrieve("缓存", chunks, pages, question="缓存")
    assert calls["embedded_texts"] == 4

    def changed_model() -> FakeEmbedding:
        client = FakeEmbedding(calls)
        client.signature = "different-model"
        return client

    changed = HybridRetriever(cfg, store, forbidden, changed_model)
    changed.index(chunks)
    changed.retrieve("通知", chunks, pages, question="通知")
    assert calls["embedded_texts"] == 7


def test_batch_reads_preserve_order_and_do_not_decode_unselected_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WebRagStore(tmp_path, RUN)
    chunks, pages = corpus()
    store.save_page(pages["p"])
    store.save_chunks(chunks)
    values = {f"key-{i}": [float(i), 1.0] for i in range(905)}
    store.save_vectors(values)
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO pages VALUES ('bad', 'bad', 'not-json')")
        db.execute("INSERT INTO chunks VALUES ('bad', 'bad', 'not-json')")
    assert store.pages(["p", "p"]) == [pages["p"]]
    assert store.chunks(["p"]) == chunks
    assert store.pages([]) == store.chunks([]) == []
    statements: list[str] = []
    original = store._connect

    def connect() -> sqlite3.Connection:
        db = original()
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(store, "_connect", connect)
    assert store.vectors([*reversed(values), "missing", "key-1"]) == values
    assert sum(s.startswith("SELECT") for s in statements) == 3


def test_second_task_reuses_page_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deep_researcher.models import TodoItem
    from deep_researcher.rag import pipeline as module

    service, _, _ = pipeline(tmp_path)
    s = state()
    service.discover(s, 1); service.read(1); service.index(1)
    s.todo_items.append(TodoItem(id=2, title="outbox", intent="通知", query="outbox"))
    service.discover(s, 2); service.read(2)
    monkeypatch.setattr(module, "chunk_page", lambda *args: pytest.fail("不应重新分块"))
    service.index(2)
    assert service.retrieve(s, 2).evidence_ids


@pytest.mark.parametrize("reasons", [False, True])
def test_rerank_modes_keep_scores_and_require_all_ids(tmp_path: Path, reasons: bool) -> None:
    cfg = config(tmp_path, enable_rerank=True, rerank_include_reason=reasons)
    chunks, pages = corpus()
    model = JsonModel([{"scores": [{"id": f"C{i}", "score": score, **({"reason": "依据"} if reasons else {})}
                                  for i, score in enumerate([3, 1])]}])
    retriever = HybridRetriever(cfg, WebRagStore(tmp_path, RUN), lambda: cast(MyLLM, model))
    ranked = [RankedChunk(chunk=c) for c in chunks]
    retriever._rerank("outbox", ranked, pages)
    assert [c.rerank for c in ranked] == [3, 1]
    assert ranked[0].reason == ("依据" if reasons else "")
    assert len(model.requests) == 1 and not model.closed
    template = json.loads(model.requests[0]["messages"][1]["content"])["output_template"]
    assert ("reason" in template["scores"][0]) is reasons


@pytest.mark.parametrize("scores", [
    [{"id": "C0", "score": 3}],
    [{"id": "C0", "score": 3}, {"id": "C0", "score": 2}],
    [{"id": "C0", "score": True}, {"id": "C1", "score": 2}],
    [{"id": "C0", "score": 4}, {"id": "C1", "score": 2}],
    [{"id": "C0", "score": 3}, {"id": "wrong", "score": 2}],
])
def test_compact_rerank_still_rejects_invalid_scores(scores: list[dict[str, Any]]) -> None:
    with pytest.raises(ModelOutputError):
        HybridRetriever._parse_ranking(json.dumps({"scores": scores}), {"C0", "C1"})


def test_rerank_repairs_empty_model_response_without_unbound_variable(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    chunks, pages = corpus()
    model = JsonModel([ModelOutputError("模型没有choices"), {"scores": [
        {"id": "C0", "score": 3}, {"id": "C1", "score": 0},
    ]}])
    ranked = [RankedChunk(chunk=c) for c in chunks]
    HybridRetriever(cfg, WebRagStore(tmp_path, RUN), lambda: cast(MyLLM, model))._rerank("outbox", ranked, pages)
    assert [c.rerank for c in ranked] == [3, 0] and len(model.requests) == 2


def test_auto_checks_actual_session_and_explicit_cuda_does_not_fake_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[Any] = []

    def engine(**kwargs: Any) -> Any:
        requested.append(kwargs["providers"])
        return SimpleNamespace(model=SimpleNamespace(model=SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])))

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"], preload_dlls=lambda **kwargs: None,
    ))
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=engine))
    local_embedding.cache_clear()
    try:
        assert local_embedding("model", "cache", "auto", 2).device == "cpu"
        assert len(requested) == 2
        with pytest.raises(RuntimeError, match="实际回退"):
            local_embedding("model", "cache", "cuda", 2)
        assert local_embedding("model", "cache", "cpu", 2).providers == ("CPUExecutionProvider",)
    finally:
        local_embedding.cache_clear()


def test_rag_timings_include_failures_and_keep_runs_isolated() -> None:
    with collect_telemetry(RUN) as tracker:
        with log_context(run_id="other"):
            with rag_timer("query_embed", cache_hit=True):
                pass
        with log_context(run_id=RUN):
            with rag_timer("query_embed", cache_hit=True):
                pass
            with pytest.raises(ValueError):
                with rag_timer("rerank_repair"):
                    raise ValueError("invalid ranking")
            logging.getLogger("deep_researcher.rag.retrieval").info("indexed", extra={
                "event": "web_embedding_index_completed", "new_vectors": 2, "cached_vectors": 3,
            })
    values = tracker.snapshot()
    assert values["rag_step_calls"] == {"query_embed": 1, "rerank_repair": 1}
    assert values["rag_cache_hits"] == {"query_embed": 1}
    assert values["rag_new_vectors"] == 2 and values["rag_cached_vectors"] == 3
