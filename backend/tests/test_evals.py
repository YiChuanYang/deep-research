"""评测口径及真实图导出的离线回归测试，不请求外部服务。"""

from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletionChunk

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import log_context
from deep_researcher.models import Evidence, Source, SummaryState, TodoItem
from deep_researcher.services.evidence_service import EvidenceService
from evals.artifacts import make_review_template, read_json, write_json
from evals.checks import NO_INFORMATION, inspect_state, score_review
from evals.compare import compare_suites
from evals.dataset import ResearchDataset, load_dataset, select_cases
from evals.run_suite import run_suite
from evals.summarize import aggregate, macro_stats, stats, summarize_suite
from evals.telemetry import collect_telemetry

BODY = "Fact A [E0001]\nFact B [E0001]\nFact C has no citation."


def seed(state: SummaryState) -> None:
    state.todo_items = [TodoItem(
        id=1, title="Task", intent="Test", query="Test", status="completed",
        summary="Fact A [E0001]", evidence_ids=["E0001"], citation_ids=["E0001"],
    )]
    state.sources = {"S0001": Source(source_id="S0001", url="https://example.invalid", title="Source")}
    state.evidence = {"E0001": Evidence(
        evidence_id="E0001", source_id="S0001", task_id=1, text="Fact A; part of fact B",
        retrieved_at="2026-11-12T00:00:00+00:00", query="Test", backend="offline", kind="search_snippet",
    )}


def state_and_review() -> tuple[SummaryState, dict[str, Any]]:
    state = SummaryState(run_id="a" * 32, research_topic="Alpha")
    seed(state)
    state.report_citation_ids = ["E0001"]
    state.structured_report = BODY + "\n\n## 引用证据\n\n" + EvidenceService().references(state, ["E0001"])
    review = make_review_template(state, inspect_state(state))
    finish_review(review)
    return state, review


def finish_review(review: dict[str, Any], scores: list[int] | None = None) -> None:
    review.update(
        status="done", reviewer="offline-test", evidence_sufficiency="limited",
        limitations_score=1, limitations_reason="Test reason",
        claim_inventory_complete=True,
        claims=[
            {"text": "Fact A [E0001]", "citation_ids": ["E0001"], "verdict": "supported", "reason": "Direct support"},
            {"text": "Fact B [E0001]", "citation_ids": ["E0001"], "verdict": "partial", "reason": "Only part is supported"},
            {"text": "Fact C has no citation.", "citation_ids": [], "verdict": "unsupported", "reason": "No evidence"},
        ],
    )
    if not review["key_points"][0]["point"]:
        review["key_points"] = [{"point": point, "score": None, "reason": ""} for point in ["A", "B", "C"]]
    values = scores if scores is not None else [2, 1, 0]
    for point, value in zip(review["key_points"], values, strict=True):
        point.update(score=value, reason="Test reason")


@pytest.fixture(autouse=True)
def close_test_loggers():
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in [
        "deep_researcher", "httpx", "httpcore", "openai", "urllib3", "ddgs",
    ]}
    yield
    for handler in root.handlers[:]:
        if handler not in old_handlers and getattr(handler, "_deep_research_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(old_level)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def dataset_path(tmp_path: Path) -> Path:
    path = tmp_path / "questions.json"
    write_json(path, {
        "schema_version": 1, "dataset_version": "1.0.0",
        "cases": [
            {"case_id": f"DR-00{number}", "split": split, "category": "overview",
             "topic": topic, "key_points": ["A", "B", "C"]}
            for number, split, topic in [(1, "dev", "Alpha"), (2, "dev", "Beta"), (3, "holdout", "Hidden")]
        ],
    })
    return path


def offline_factory(*, config: Configuration, data_dir: Path) -> GraphDeepResearcher:
    class Nodes(ResearchNodes):
        def plan(self, state, graph_state):
            seed(state)
            return {}

        def reporter_agent(self):
            chunks = [
                ChatCompletionChunk(
                    id="test", object="chat.completion.chunk", created=0, model="offline",
                    choices=[{"index": 0, "delta": {"content": BODY}, "finish_reason": None}],
                ),
                ChatCompletionChunk(
                    id="test", object="chat.completion.chunk", created=0, model="offline",
                    choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                ),
            ]
            # 真正的 Agent / LLM 日志 / Reporter / SQLite，只有网络响应是脚本。
            llm = object.__new__(MyLLM)
            llm.config, llm.model, llm.llm_provider = config, "offline", "offline"
            llm.client = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: (chunk for chunk in chunks))),
                close=lambda: None,
            )
            return SimpleAgent("offline", llm, config=config)
    return GraphDeepResearcher(config, data_dir=data_dir, nodes_factory=Nodes)


def run_offline(dataset_path: Path, tmp_path: Path, **kwargs) -> Path:
    config = Configuration(
        llm_provider="offline", llm_model_id="offline", llm_base_url="https://example.invalid/v1",
        llm_api_key="offline-test-secret", search_api="duckduckgo", enable_notes=False,
    )
    arguments = dict(
        variant="test", dataset_path=dataset_path, output_dir=tmp_path / "runs",
        config=config, researcher_factory=offline_factory, mode="offline_test",
    )
    arguments.update(kwargs)
    return run_suite(**arguments)


def test_dataset_validation_and_holdout(dataset_path: Path):
    dataset = load_dataset(dataset_path)
    assert len(select_cases(dataset)) == 2
    with pytest.raises(ValueError, match="split"):
        select_cases(dataset, case_ids=["DR-003"])
    with pytest.raises(ValueError, match="不存在"):
        select_cases(dataset, case_ids=["DR-999"])
    data = dataset.model_dump(mode="json")
    data["cases"].append(deepcopy(data["cases"][0]))
    with pytest.raises(ValueError, match="重复"):
        ResearchDataset.model_validate(data)
    data["cases"].pop()
    data["cases"][0]["key_points"] = ["A", "A", "B"]
    with pytest.raises(ValueError, match="重复"):
        ResearchDataset.model_validate(data)
    dataset_path.write_text(dataset.model_dump_json(indent=4), encoding="utf-8-sig")
    assert load_dataset(dataset_path).fingerprint() == dataset.fingerprint()


def test_score_arithmetic_and_zero_denominators():
    state, review = state_and_review()
    checks = inspect_state(state)
    score = score_review(review, state, checks, expected_points=["A", "B", "C"])
    assert checks["overall_check"]["status"] == "pass"
    assert score["coverage_ratio"] == 0.5
    assert score["citation_coverage"] == pytest.approx(2 / 3)
    assert score["citation_support"] == 0.5
    assert score["partial_claims"] == score["uncited_claims"] == 1
    review.update(claims=[], no_claims_reason="No factual claims")
    score = score_review(review, state, checks)
    assert score["citation_coverage"] is None and score["citation_support"] is None


@pytest.mark.parametrize("change", ["points", "hash", "run_id", "duplicate", "citation", "unsupported"])
def test_invalid_review_rejected(change: str):
    state, review = state_and_review()
    if change == "points":
        review["key_points"][0]["point"] = "Altered"
    elif change == "hash":
        review.update(status="pending", report_sha256="old")
    elif change == "run_id":
        review["run_id"] = "other"
    elif change == "duplicate":
        review["claims"].append(deepcopy(review["claims"][0]))
    elif change == "citation":
        review["claims"][0]["citation_ids"] = []
    else:
        review["claims"][-1]["verdict"] = "supported"
    with pytest.raises(ValueError):
        score_review(review, state, inspect_state(state), expected_points=["A", "B", "C"])


def test_automatic_check_unknown_boundary_and_no_information():
    state, _ = state_and_review()
    state.structured_report += "\nChanged footer"
    assert inspect_state(state)["report_check"]["status"] == "unverified"
    empty = SummaryState(run_id="b" * 32, structured_report=NO_INFORMATION)
    assert inspect_state(empty)["report_check"]["status"] == "not_applicable"
    state.todo_items[0].summary = "No citations"
    assert inspect_state(state)["overall_check"]["status"] == "fail"


def test_telemetry_unknown_usage_and_run_isolation():
    logger = logging.getLogger("deep_researcher.test")
    with collect_telemetry("target") as tracker:
        with log_context(run_id="other"):
            logger.info("request", extra={"event": "model_request"})
        with log_context(run_id="target"):
            for event, tokens in [
                ("model_request", None), ("model_response", 12),
                ("model_stream_started", None), ("model_stream_completed", None),
                ("search_started", None), ("search_completed", None),
            ]:
                logger.info("event", extra={"event": event, "tokens": tokens, "ttft_s": 0.2 if "completed" in event else None})
    value = tracker.snapshot()
    assert value["model_requests"] == value["model_responses"] == 2
    assert value["reported_tokens"] == 12
    assert value["total_tokens"] is None and value["usage_complete"] is False
    assert value["search_requests"] == value["search_responses"] == 1
    assert value["report_ttft_s"] == 0.2 and value["cost"] is None


def test_real_graph_suite_and_fixed_review(dataset_path: Path, tmp_path: Path):
    batch = run_offline(dataset_path, tmp_path, repeats=2)
    summary = read_json(batch / "summary.json")
    assert summary["counts"]["scheduled"] == summary["counts"]["completed"] == 4
    assert summary["counts"]["pending"] == 4
    assert summary["quality"]["coverage_ratio"]["mean"] is None
    assert summary["automatic"]["overall"]["pass_rate"]["value"] == 1
    assert summary["efficiency"]["total_tokens"]["mean"] == 30
    assert summary["efficiency"]["model_requests"]["mean"] == 1
    assert len({row["run_id"] for row in summary["rows"]}) == 4
    manifest = read_json(batch / "manifest.json")
    assert "llm_api_key" not in manifest["config"]
    assert (batch / "source_snapshot" / "src" / "deep_researcher" / "config.py").exists()
    assert all(row["case_id"] != "DR-003" for row in summary["rows"])
    for row in summary["rows"][:2]:
        path = batch / row["path"] / "review.json"
        review = read_json(path)
        finish_review(review)
        write_json(path, review)
    summary = summarize_suite(batch)
    assert summary["counts"]["reviewed"] == summary["counts"]["pending"] == 2
    assert summary["quality"]["coverage_ratio"]["mean"] == 0.5
    path = batch / summary["rows"][0]["path"] / "review.json"
    review = read_json(path)
    review["key_points"][0]["point"] = "Changed criterion"
    write_json(path, review)
    assert summarize_suite(batch)["counts"]["invalid_review"] == 1
    # 篡改缓存检查不能影响重算；篡改实际供人工阅读的证据会被拦截。
    directory = batch / summary["rows"][1]["path"]
    write_json(directory / "checks.json", {"report_check": {"status": "fail"}})
    assert summarize_suite(batch)["automatic"]["report"]["pass_rate"]["value"] == 1
    write_json(directory / "evidence.json", {})
    assert summarize_suite(batch)["counts"]["invalid_artifact"] == 1


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_execution_failure_and_interrupt_preserve_schedule(dataset_path: Path, tmp_path: Path, failure):
    calls = []
    def factory(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise failure("offline simulated failure")
        return offline_factory(**kwargs)
    batch = run_offline(dataset_path, tmp_path, researcher_factory=factory)
    summary = read_json(batch / "summary.json")
    assert summary["counts"]["scheduled"] == 2
    if failure is KeyboardInterrupt:
        assert summary["counts"]["interrupted"] == summary["counts"]["not_started"] == 1
        assert summary["status"] == "interrupted"
    else:
        assert summary["counts"]["failed"] == summary["counts"]["completed"] == 1
        assert summary["rates"]["execution_completion"]["value"] == 0.5
    assert summary["rows"][0]["telemetry"] is None


def test_export_failure_is_not_execution_failure(dataset_path: Path, tmp_path: Path, monkeypatch):
    import evals.run_suite as runner
    def fail(*args):
        raise OSError("offline export failure")
    monkeypatch.setattr(runner, "export_attempt", fail)
    batch = run_offline(dataset_path, tmp_path, case_ids=["DR-001"])
    counts = read_json(batch / "summary.json")["counts"]
    assert counts["completed"] == counts["export_failed"] == 1
    assert counts["failed"] == counts["exported"] == 0


def test_macro_mean_does_not_overweight_repeated_question():
    result = macro_stats([
        {"case_id": "A", "value": 1}, {"case_id": "A", "value": 0},
        {"case_id": "B", "value": 1}, {"case_id": "B", "value": None},
    ], lambda row: row["value"])
    assert result["mean"] == 0.75
    assert result["n_attempts"] == 3 and result["n_cases"] == 2
    assert stats([1, 2, 3, 4])["p95"] == pytest.approx(3.85)
    assert aggregate([])["rates"]["execution_completion"]["value"] is None


def test_comparison_pairs_only_valid_same_scope_reviews(dataset_path: Path, tmp_path: Path, monkeypatch):
    baseline = run_offline(dataset_path, tmp_path, case_ids=["DR-001"], variant="baseline")
    candidate = run_offline(dataset_path, tmp_path, variant="candidate")
    for batch, scores in [(baseline, [2, 1, 0]), (candidate, [2, 2, 2])]:
        manifest = read_json(batch / "manifest.json")
        path = batch / manifest["attempts"][0]["path"] / "review.json"
        review = read_json(path)
        finish_review(review, scores)
        write_json(path, review)
    comparison = compare_suites(baseline, candidate)
    assert comparison["paired_attempts"] == 1
    assert comparison["only_candidate"] == [["DR-002", 1]]
    assert comparison["metrics"]["coverage_ratio"]["delta_mean"] == 0.5
    assert comparison["metrics"]["citation_support_complete"]["n_pairs"] == 1
    import evals.compare as comparison_cli
    monkeypatch.setattr(sys, "argv", [
        "compare", str(baseline), str(candidate), "--output-dir", str(tmp_path / "comparisons"),
    ])
    comparison_cli.main()
    assert len(list((tmp_path / "comparisons").glob("*/comparison.md"))) == 1
    from evals.review import summarize_batch
    summarize_batch(baseline)
    assert read_json(baseline / "summary.json")["counts"]["reviewed"] == 1
    review["claim_inventory_complete"] = False
    write_json(path, review)
    comparison = compare_suites(baseline, candidate)
    assert comparison["metrics"]["citation_support_complete"]["n_pairs"] == 0
    assert comparison["metrics"]["citation_support_sampled"]["n_pairs"] == 0
    data = read_json(candidate / "dataset.json")
    data["dataset_version"] = "2.0.0"
    write_json(candidate / "dataset.json", data)
    manifest = read_json(candidate / "manifest.json")
    manifest["dataset_sha256"] = load_dataset(candidate / "dataset.json").fingerprint()
    write_json(candidate / "manifest.json", manifest)
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare_suites(baseline, candidate)


def test_legacy_export_filters_deleted_runs_and_closes_backup(tmp_path: Path):
    from evals.review import _backup_sqlite, _read_completed_runs
    database = tmp_path / "runs.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE graph_runs (run_id TEXT, data TEXT, updated_at TEXT, status TEXT, deleted_at TEXT)")
        connection.executemany("INSERT INTO graph_runs VALUES (?, ?, ?, ?, ?)", [
            ("active", '{"topic": "Active"}', "2026-11-12", "completed", None),
            ("deleted", '{"topic": "Deleted"}', "2026-11-12", "completed", "2026-11-12"),
            ("failed", '{"topic": "Failed"}', "2026-11-12", "failed", None),
        ])
        connection.commit()
    assert [run.run_id for run in _read_completed_runs(tmp_path)] == ["active"]
    backup = tmp_path / "backup.sqlite3"
    _backup_sqlite(database, backup)
    # Windows 不允许重命名仍被 SQLite 打开的文件。
    moved = tmp_path / "moved.sqlite3"
    backup.replace(moved)
    with closing(sqlite3.connect(moved)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 3


def test_cli_dry_run_does_not_load_credentials(dataset_path: Path, tmp_path: Path, monkeypatch):
    import evals.run_suite as runner
    def reject(*args, **kwargs):
        raise AssertionError("dry-run must not load credentials")
    monkeypatch.setattr(Configuration, "from_env", reject)
    output = tmp_path / "should-not-exist"
    monkeypatch.setattr(sys, "argv", [
        "run_suite", "--variant", "preview", "--dataset", str(dataset_path),
        "--dry-run", "--output-dir", str(output),
    ])
    runner.main()
    assert not output.exists()
