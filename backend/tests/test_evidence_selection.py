"""固定输入验证筛选、片段溯源、正文降级和真实图/SQLite 集成。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration, SearchAPI
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import ResearchGraphState
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.selection_audit import SelectionAuditStore
from deep_researcher.services.evidence_selector import EvidenceSelector, _spans
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.services.search import SearchService
from deep_researcher.tools.search_tool import SearchTool
from evals.artifacts import read_json, write_json
from evals.run_suite import run_suite


class SelectorAgent:
    """只根据输入返回编号，记录实际传入的全部候选。"""

    def __init__(self, mode: str = "keep") -> None:
        self.mode = mode
        self.inputs: list[dict[str, Any]] = []
        self.clears = 0

    def run(self, input_text: str, **kwargs: Any) -> str:
        payload = json.loads(input_text)
        self.inputs.append(payload)
        if self.mode == "error":
            raise RuntimeError("offline model failure")
        if self.mode == "bad_json":
            return "```json {} ```"
        rows = [{"id": row["id"], "decision": "keep", "reason": "与任务直接相关"} for row in payload["candidates"]]
        if self.mode == "unknown":
            rows[0]["id"] = "C9999"
        if self.mode == "duplicate" and len(rows) > 1:
            rows[1]["id"] = rows[0]["id"]
        if self.mode == "drop" or (self.mode == "drop_fragments" and payload["phase"] == "fragments"):
            for row in rows:
                row["decision"] = "drop"
        return json.dumps({"decisions": rows}, ensure_ascii=False)

    def clear_history(self) -> None:
        self.clears += 1


class FakeSearch:
    backend = "duckduckgo"

    def __init__(self, results: list[dict[str, Any]], pages: dict[str, Any] | Exception | None = None) -> None:
        self.results = results
        self.pages = pages if pages is not None else {"results": []}
        self.searches = 0
        self.extract_calls: list[list[str]] = []

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        self.searches += 1
        return {"results": self.results, "notices": ["原有搜索提示"]}

    def extract_pages(self, urls: list[str]) -> dict[str, Any]:
        self.extract_calls.append(urls)
        if isinstance(self.pages, Exception):
            raise self.pages
        return self.pages


def make_state() -> SummaryState:
    return SummaryState(run_id="a" * 32, research_topic="高等代数 多项式 整除 因式分解", todo_items=[
        TodoItem(id=1, title="多项式课程", intent="课程知识范围", query="高等代数 多项式", search_query="课程大纲 多项式 整除 因式分解"),
    ])


def config(**kwargs: Any) -> Configuration:
    return Configuration(search_api=SearchAPI.DUCKDUCKGO, **kwargs)


def search_service(cfg: Configuration, tool: FakeSearch, agent: SelectorAgent) -> SearchService:
    return SearchService(cfg, search_tool=cast(SearchTool, tool), selector_agent_factory=lambda: cast(SimpleAgent, agent))


def test_middle_document_excerpt_is_selected_and_provenance_is_exact() -> None:
    state = make_state()
    before = asdict(state)
    document = ("页面其他介绍。" * 400) + "\n\n# 多项式\n多项式整除、因式分解与重根。限定：仅讨论数域上的多项式。\n\n" + ("结束说明。" * 30)
    tool = FakeSearch([{"url": "https://example.com/syllabus", "title": "课程大纲", "content": "大纲目录"}], {
        "results": [{"url": "https://example.com/syllabus", "raw_content": document}],
    })
    agent = SelectorAgent()
    batch = search_service(config(evidence_max_chars=400), tool, agent).search_task(state, 1)
    assert asdict(state) == before
    assert tool.extract_calls == [["https://example.com/syllabus"]]
    assert len(agent.inputs) == agent.clears == 2
    assert any("因式分解与重根" in item.text for item in batch.evidence.values())
    assert all(item.kind == "page_excerpt" for item in batch.evidence.values())
    audit = batch.selection_audit
    candidates = {item["candidate_id"]: item for item in audit["candidates"]}
    for chosen in audit["selected"]:
        candidate = candidates[chosen["candidate_id"]]
        saved = audit["documents"][candidate["page_id"]]
        assert saved["text"][candidate["start"]:candidate["end"]] == batch.evidence[chosen["evidence_id"]].text
        assert hashlib.sha256(saved["text"].encode()).hexdigest() == candidate["document_hash"]
        assert len(candidate["text"]) <= 400
    assert "原有搜索提示" in batch.notices
    assert all(item.query == state.get_task(1).effective_query for item in batch.evidence.values())


@pytest.mark.parametrize("mode", ["drop", "drop_fragments"])
def test_explicit_drop_all_does_not_fallback(mode: str) -> None:
    tool = FakeSearch([{"url": "https://example.com", "content": "无关内容"}])
    batch = search_service(config(), tool, SelectorAgent(mode)).search_task(make_state(), 1)
    assert batch.evidence_ids == []
    assert not any("降级" in message for message in batch.notices)
    if mode == "drop":
        assert tool.extract_calls == []


@pytest.mark.parametrize("mode", ["error", "bad_json", "unknown", "duplicate"])
def test_model_errors_preserve_bounded_rule_fallback(mode: str) -> None:
    tool = FakeSearch([
        {"url": "https://a.example.com", "content": "多项式整除"},
        {"url": "https://b.example.com", "content": "因式分解的条件"},
    ])
    agent = SelectorAgent(mode)
    batch = search_service(config(enable_page_extraction=False), tool, agent).search_task(make_state(), 1)
    assert batch.evidence_ids
    assert agent.clears == 2
    assert all(item.kind == "search_snippet" for item in batch.evidence.values())
    assert any("相关性未确认" in message for message in batch.notices)
    assert tool.extract_calls == []


@pytest.mark.parametrize("fail", [True, False])
def test_partial_and_total_extraction_failure_keep_snippet(fail: bool) -> None:
    rows = [{"url": "https://a.example.com", "content": "搜索摘要甲"},
            {"url": "https://b.example.com", "content": "搜索摘要乙"}]
    extracted = RuntimeError("timeout") if fail else {"results": [
        {"url": "https://a.example.com", "raw_content": "取得的正文甲"},
        {"url": "https://unrequested.example.com", "raw_content": "不应登记"},
    ], "failed_results": [{"url": "https://b.example.com"}]}
    batch = search_service(config(), FakeSearch(rows, extracted), SelectorAgent()).search_task(make_state(), 1)
    assert len(batch.evidence_ids) == 2
    assert all(source.url != "https://unrequested.example.com" for source in batch.sources.values())
    assert sum(item.kind == "page_excerpt" for item in batch.evidence.values()) == (0 if fail else 1)
    assert any("未取得正文" in notice for notice in batch.notices)


def test_existing_body_skips_fetch_and_keeps_historical_evidence() -> None:
    state = make_state()
    tool = FakeSearch([{"url": "https://example.com", "raw_content": "多项式整除"}])
    service = search_service(config(), tool, SelectorAgent())
    first = service.search_task(state, 1)
    state.sources, state.evidence = first.sources, first.evidence
    state.todo_items.append(TodoItem(id=2, title="第二任务", intent="区别", query="多项式"))
    second = service.search_task(state, 2)
    assert len(second.sources) == 1
    assert len(second.evidence) == 2
    assert first.evidence_ids[0] in second.evidence
    assert second.evidence_ids != first.evidence_ids
    assert tool.extract_calls == []


def test_rules_remove_duplicates_and_budgets_preserve_spans() -> None:
    text = "\n\n".join(f"段落{i} 多项式内容与条件。" * 40 for i in range(25))
    rows = [{"url": "http://[invalid", "content": "x"}, {"url": "https://example.com#one", "raw_content": text},
            {"url": "https://example.com#two", "raw_content": text}]
    cfg = config(evidence_max_chars=350, selection_input_chars=4000, selection_max_fragments=2, selection_per_source=1)
    batch = search_service(cfg, FakeSearch(rows), SelectorAgent()).search_task(make_state(), 1)
    assert len(batch.evidence_ids) == 1
    assert any(row["reason"] == "重复页面已合并" for row in batch.selection_audit["removed"])
    assert all(step.get("input_chars", 0) <= 4000 for step in batch.selection_audit["selection_steps"])
    assert batch.selection_audit["preselection_omitted_ids"]
    for start, end in _spans(text, 350):
        assert 0 <= start < end <= len(text)
        assert end - start <= 350


def test_disabled_selection_and_legacy_config_keep_original_behavior() -> None:
    state = make_state()
    tool = FakeSearch([{"url": "https://example.com", "content": "原始摘要" * 100}])
    agent = SelectorAgent()
    cfg = config(enable_evidence_selection=False, evidence_max_chars=30)
    batch = search_service(cfg, tool, agent).search_task(state, 1)
    assert len(batch.evidence[batch.evidence_ids[0]].text) == 30
    assert batch.selection_audit == {}
    assert agent.inputs == [] and tool.extract_calls == []
    old = cfg.snapshot()
    old.pop("enable_evidence_selection")
    old.pop("enable_page_extraction")
    restored = config().for_resume(old)
    assert not restored.enable_evidence_selection and not restored.enable_page_extraction


def test_preselected_text_not_retrimmed_and_oversize_is_rejected() -> None:
    state = make_state()
    row = {"url": "https://example.com", "content": " 条件与事实。 ", "kind": "page_excerpt", "truncated": True}
    batch = EvidenceService().collect(state, 1, [row], backend="test", max_chars=100, prefer_full_page=False, preselected=True)
    item = batch.evidence[batch.evidence_ids[0]]
    assert item.text == row["content"] and item.kind == "page_excerpt" and item.truncated
    with pytest.raises(ValueError, match="长度预算"):
        EvidenceService().collect(state, 1, [row], backend="test", max_chars=2, prefer_full_page=False, preselected=True)


class TextAgent:
    def run(self, **kwargs: Any) -> str:
        return "多项式内容 [E0001]"

    def stream(self, **kwargs: Any) -> Iterator[str]:
        yield "报告 [E0001]"

    def clear_history(self) -> None:
        pass


def test_graph_audit_export_resume_and_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = FakeSearch([{"url": "https://example.com", "raw_content": "多项式整除与因式分解"}])
    selector = SelectorAgent()
    fail_summary = [True]

    class Nodes(ResearchNodes):
        def plan(self, state: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            state.todo_items = make_state().todo_items
            return {}

        def evidence_selector_agent(self) -> SimpleAgent:
            return cast(SimpleAgent, selector)

        def summary_agent(self) -> SimpleAgent:
            if fail_summary[0]:
                raise RuntimeError("temporary summary failure")
            return cast(SimpleAgent, TextAgent())

        def reporter_agent(self) -> SimpleAgent:
            return cast(SimpleAgent, TextAgent())

    monkeypatch.setattr("deep_researcher.graph.graph_nodes.SearchService", lambda cfg, **kw: SearchService(cfg, search_tool=cast(SearchTool, tool), **kw))
    cfg = config(enable_query_rewrite=False, enable_notes=False, notes_workspace=str(tmp_path / "notes"))
    researcher = GraphDeepResearcher(cfg, data_dir=tmp_path / "data", nodes_factory=Nodes)
    with pytest.raises(RuntimeError, match="summary failure"):
        researcher.run("多项式课程")
    run_id = researcher.last_run_id
    assert run_id is not None
    saved = researcher.read_run(run_id)
    assert saved.next_nodes == ("summarize",)
    store = SelectionAuditStore(tmp_path / "data/selection_audits.sqlite3")
    audits = store.read_run(run_id)
    assert len(audits) == 1
    assert audits[0]["audit"]["selected"][0]["evidence_id"] == "E0001"
    assert "selection_audit" not in asdict(saved.state)
    fail_summary[0] = False
    researcher.resume(run_id)
    assert tool.searches == 1 and len(selector.inputs) == 2
    researcher.delete_run(run_id)
    assert store.read_run(run_id) == []

    dataset = tmp_path / "dataset.json"
    write_json(dataset, {"schema_version": 1, "dataset_version": "1.0.0", "cases": [{
        "case_id": "DR-001", "split": "dev", "category": "overview", "topic": "多项式课程",
        "key_points": ["整除", "分解", "条件"],
    }]})
    def factory(**kwargs: Any) -> GraphDeepResearcher:
        return GraphDeepResearcher(**kwargs, nodes_factory=Nodes)
    runner_cfg = cfg.model_copy(update={"llm_model_id": "fake", "llm_base_url": "https://example.invalid/v1", "llm_api_key": "fake"})
    # require_llm 需要 SecretStr；model_validate 执行类型转换。
    runner_cfg = Configuration.model_validate(runner_cfg.model_dump())
    batch = run_suite(variant="offline-selection", dataset_path=dataset, config=runner_cfg,
                      output_dir=tmp_path / "evals", researcher_factory=factory, mode="offline_test")
    exports = list(batch.glob("attempts/*/*/selection_audits.json"))
    assert len(exports) == 1
    assert read_json(exports[0])[0]["audit"]["documents"]


def test_extract_tool_forwards_bounded_urls_without_real_network() -> None:
    calls: list[dict[str, Any]] = []
    class Client:
        def extract(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"results": [], "failed_results": []}
    tool = object.__new__(SearchTool)
    tool._tavily_client = cast(Any, Client())
    tool.timeout = 12
    tool.extract_pages(["https://example.com"])
    assert calls == [{"urls": ["https://example.com"], "format": "markdown", "extract_depth": "basic", "timeout": 12}]
    tool._tavily_client = None
    assert tool.extract_pages(["https://example.com"])["failed_results"]
