"""复用已保存网页做局部性能验证；默认不联网，--rerank 才调用现有模型。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from uuid import uuid4

import numpy as np
from rank_bm25 import BM25Okapi

from deep_researcher.config import Configuration
from deep_researcher.graph.graph_state import decode_state
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import log_context
from deep_researcher.models import SummaryState
from deep_researcher.rag.embeddings import EmbeddingClient, local_embedding
from deep_researcher.rag.retrieval import HybridRetriever, RankedChunk, tokenize
from deep_researcher.rag.store import PageSnapshot, TextChunk, WebRagStore
from evals.telemetry import collect_telemetry


@dataclass
class Fixture:
    state: SummaryState
    pages: dict[str, PageSnapshot]
    chunks: list[TextChunk]
    ranking: list[dict[str, Any]]
    task_id: int


def load_fixture(batch: Path, case_id: str) -> Fixture:
    raw = json.loads((batch / "attempts" / case_id / "trial-001" / "snapshot.json").read_text(encoding="utf-8"))
    state = decode_state(raw["state"])
    path = batch / "web_rag" / state.run_id / "corpus.sqlite3"
    # 原始评测资料只读，基准测试的新缓存另建目录。
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        pages = [PageSnapshot(**json.loads(row[0])) for row in db.execute("SELECT data FROM pages ORDER BY rowid")]
        chunks = [TextChunk(**json.loads(row[0])) for row in db.execute("SELECT data FROM chunks ORDER BY rowid")]
        records = {key: json.loads(data) for key, data in db.execute("SELECT key, data FROM records")}
    task = next(t for t in state.todo_items if records.get(f"task:{t.id}:retrieve:web", {}).get("audit"))
    return Fixture(state, {p.snapshot_id: p for p in pages}, chunks,
                   records[f"task:{task.id}:retrieve:web"]["audit"], task.id)


def measure(action: Callable[[], Any], repeats: int) -> tuple[Any, list[float]]:
    durations: list[float] = []
    result: Any = None
    for _ in range(repeats):
        started = perf_counter()
        result = action()
        durations.append(perf_counter() - started)
    return result, durations


def forbidden_model() -> MyLLM:
    raise AssertionError("本地基准不允许模型调用")


def local_benchmark(fixture: Fixture, args: argparse.Namespace, destination: Path) -> dict[str, Any]:
    cfg = Configuration(enable_rerank=False, embedding_cache_dir=str(args.model_cache))
    size = min(args.chunks, len(fixture.chunks))
    picked = [fixture.chunks[i * (len(fixture.chunks) - 1) // max(1, size - 1)] for i in range(size)]
    texts = [c.section[:60] + "\n" + c.text for c in picked]
    results: list[dict[str, Any]] = []
    reference: np.ndarray | None = None
    for device in args.devices:
        for batch_size in args.batch_sizes:
            for threads in args.threads:
                current = cfg.model_copy(update={"embedding_device": device,
                                                 "embedding_batch_size": batch_size,
                                                 "embedding_threads": threads})
                init_at = perf_counter()
                client = EmbeddingClient(current)
                init_s = perf_counter() - init_at
                try:
                    client.embed(texts[:2], 120)  # 预热单列，不计入稳定计算耗时。
                    vectors, durations = measure(lambda encoder=client: encoder.embed(texts, 120), args.repeats)
                    matrix = np.asarray(vectors, dtype=float)
                    if reference is None:
                        reference = matrix
                    similarity = np.sum(reference * matrix, axis=1) / (
                        np.linalg.norm(reference, axis=1) * np.linalg.norm(matrix, axis=1)
                    )
                    record = {"requested_device": device, "actual_device": client.device,
                              "providers": list(client.providers), "batch_size": batch_size,
                              "threads": threads, "texts": size, "init_s": init_s,
                              "duration_s": durations, "median_s": median(durations),
                              "min_cosine_vs_first": float(similarity.min())}
                    results.append(record)
                    print(json.dumps(record, ensure_ascii=False), flush=True)
                finally:
                    client.close()
                del client
            # 测试多套线程配置时及时释放旧会话，避免把多个模型同时留在显存。
            local_embedding.cache_clear()

    store = WebRagStore(destination / "corpus", "b" * 32)
    retriever = HybridRetriever(cfg.model_copy(update={"enable_embeddings": False}), store, forbidden_model)
    chunks, pages = fixture.chunks, fixture.pages
    query = fixture.state.get_task(fixture.task_id).effective_query
    retriever._lexical_index(chunks, pages)

    def rebuild() -> Any:
        tokens = [list(tokenize(pages[c.snapshot_id].title + " " + c.section + " " + c.text)) or ["_"] for c in chunks]
        return BM25Okapi(tokens).get_scores(tokenize(query))

    expected, cold = measure(rebuild, args.repeats)
    actual, warm = measure(lambda: retriever._lexical_index(chunks, pages)[0].get_scores(tokenize(query)), args.repeats)
    np.testing.assert_allclose(actual, expected)
    return {"embedding": results, "bm25": {"chunks": len(chunks), "rebuild_s": cold,
            "cached_s": warm, "scores_equal": True, "note": "两组分词均已预热；比较是否重复构建BM25。"}}


def rerank_benchmark(fixture: Fixture, args: argparse.Namespace, destination: Path) -> list[dict[str, Any]]:
    cfg = Configuration.from_env()
    by_id = {c.chunk_id: c for c in fixture.chunks}
    task = fixture.state.get_task(fixture.task_id)
    question = f"研究主题：{fixture.state.research_topic}\n当前任务：{task.title}\n目标：{task.intent}\n查询：{task.effective_query}"
    model = MyLLM(cfg)
    results: list[dict[str, Any]] = []
    try:
        for trial in range(args.repeats):
            # 交替执行顺序，减少某一种输出格式总是先运行的影响。
            for reasons in ([True, False] if trial % 2 == 0 else [False, True]):
                run_id = uuid4().hex
                current = cfg.model_copy(update={"rerank_include_reason": reasons})
                retriever = HybridRetriever(current, WebRagStore(destination / "rerank", run_id), lambda: model)
                candidates = [RankedChunk(chunk=by_id[row["chunk_id"]]) for row in fixture.ranking]
                started = perf_counter()
                error: str | None = None
                with log_context(run_id=run_id), collect_telemetry(run_id) as tracker:
                    try:
                        retriever._rerank(question, candidates, fixture.pages)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                record = {"trial": trial + 1, "include_reason": reasons, "candidate_count": len(candidates),
                          "duration_s": perf_counter() - started, "error": error,
                          "scores": {r.chunk.chunk_id: r.rerank for r in candidates},
                          "reasons": {r.chunk.chunk_id: r.reason for r in candidates},
                          "telemetry": tracker.snapshot()}
                results.append(record)
                print(json.dumps({k: v for k, v in record.items() if k not in {"scores", "reasons", "telemetry"}}, ensure_ascii=False), flush=True)
    finally:
        model.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument("--case-id", default="DR-002")
    parser.add_argument("--devices", nargs="+", choices=["cpu", "cuda", "auto"], default=["cpu", "cuda"])
    parser.add_argument("--batch-sizes", nargs="+", type=int, default=[16])
    parser.add_argument("--threads", nargs="+", type=int, default=[2])
    parser.add_argument("--chunks", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model-cache", type=Path, default=Path("data/models"))
    parser.add_argument("--rerank", action="store_true", help="仅对比重排输出格式，实际调用当前配置模型")
    parser.add_argument("--output-dir", type=Path, default=Path("evals/runs"))
    args = parser.parse_args()
    if args.repeats < 1 or args.chunks < 1 or any(n < 1 for n in [*args.batch_sizes, *args.threads]):
        parser.error("重复次数、文本数、批大小和线程数必须为正数")
    fixture = load_fixture(args.batch_dir, args.case_id)
    destination = args.output_dir / ("rag-speed-" + uuid4().hex[:12])
    destination.mkdir(parents=True, exist_ok=False)
    result = {"batch": str(args.batch_dir.resolve()), "case_id": args.case_id,
              "kind": "rerank" if args.rerank else "local", "repeats": args.repeats}
    result["results"] = (rerank_benchmark(fixture, args, destination) if args.rerank
                         else local_benchmark(fixture, args, destination))
    target = destination / "benchmark.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"结果：{target}")


if __name__ == "__main__":
    main()
