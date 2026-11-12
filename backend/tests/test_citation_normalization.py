"""检查引用规范化的边界、审计记录和摘要/报告接入。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any, cast

import pytest
from openai.types.chat import ChatCompletion

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import JsonFormatter
from deep_researcher.models import Evidence, Source, SummaryState, TodoItem
from deep_researcher.services.evidence_service import CitationError, EvidenceService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.summarizer import SummarizationService
from evals.checks import check_text


@pytest.fixture
def state() -> SummaryState:
    value = SummaryState(run_id="a" * 32, research_topic="引用验证")
    value.todo_items = [TodoItem(
        id=1, title="任务", intent="核对引用", query="test", status="completed",
        summary="依据 [E0001][E0002]", evidence_ids=["E0001", "E0002"],
        citation_ids=["E0001", "E0002"],
    )]
    value.sources["S0001"] = Source(
        source_id="S0001", url="https://example.com", title="Source",
    )
    for number in range(1, 4):
        key = f"E{number:04d}"
        value.evidence[key] = Evidence(
            evidence_id=key, source_id="S0001", task_id=1, text=f"事实 {number}",
            retrieved_at="2026-11-12T00:00:00Z", query="test", backend="test",
            kind="search_snippet",
        )
    return value


@pytest.mark.parametrize(("raw", "expected", "ids"), [
    ("事实 [E0001, E0002]", "事实 [E0001][E0002]", ("E0001", "E0002")),
    ("事实 [E0001，E0002]", "事实 [E0001][E0002]", ("E0001", "E0002")),
    ("事实 [E0002,\tE0001]", "事实 [E0002][E0001]", ("E0002", "E0001")),
    ("事实 [E0001,E0001]", "事实 [E0001][E0001]", ("E0001",)),
    ("事实 [E0001][E0002]", "事实 [E0001][E0002]", ("E0001", "E0002")),
])
def test_prepare_is_idempotent_and_does_not_mutate_state(
    state: SummaryState, raw: str, expected: str, ids: tuple[str, ...],
) -> None:
    before = asdict(state)
    service = EvidenceService()
    result = service.prepare_text(state, raw, iter(["E0001", "E0002"]))
    assert result.text == expected
    assert result.citation_ids == ids
    assert service.prepare_text(state, result.text, ids) == result
    assert asdict(state) == before


@pytest.mark.parametrize("raw", [
    "事实 [E0001,E9999]", "事实 [E0001,E0003]", "事实 [E0001-E0002]",
    "事实 [E0001;E0002]", "事实 [E0001,0002]", "事实 [E1,E2]",
    "没有引用", "", "事实 [E0001,E0002] https://example.com",
])
def test_unsupported_output_is_rejected(state: SummaryState, raw: str) -> None:
    before = asdict(state)
    with pytest.raises(CitationError):
        EvidenceService().prepare_text(state, raw, ["E0001", "E0002"])
    assert asdict(state) == before


def test_missing_source_still_fails(state: SummaryState) -> None:
    state.sources.clear()
    with pytest.raises(CitationError, match="来源不存在"):
        EvidenceService().prepare_text(state, "事实 [E0001,E0002]", ["E0001", "E0002"])


def test_audit_preserves_original_offsets_and_only_logs_actual_changes(
    state: SummaryState, caplog: pytest.LogCaptureFixture,
) -> None:
    raw = "甲 [E0001, E0002]。乙 [E0002，E0001]。"
    service = EvidenceService()
    with caplog.at_level(logging.INFO):
        result = service.prepare_text(state, raw, ["E0001", "E0002"], document="report")
        service.prepare_text(state, result.text, ["E0001", "E0002"])
    records = [record for record in caplog.records if getattr(record, "event", None) == "citations_normalized"]
    assert len(records) == 1
    audit = json.loads(JsonFormatter().format(records[0]))
    assert audit["original_text"] == raw
    assert audit["normalized_text"] == result.text
    assert audit["repair_count"] == 2
    for change in audit["citation_changes"]:
        assert raw[change["start"]:change["end"]] == change["before"]


def test_rejected_candidate_is_not_logged_as_success(
    state: SummaryState, caplog: pytest.LogCaptureFixture,
) -> None:
    raw = "事实 [E0001,E9999]"
    with caplog.at_level(logging.INFO), pytest.raises(CitationError):
        EvidenceService().prepare_text(state, raw, ["E0001", "E0002"])
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "citations_normalized" not in events
    rejected = next(record for record in caplog.records if getattr(record, "event", None) == "citation_normalization_rejected")
    assert json.loads(JsonFormatter().format(rejected))["original_text"] == raw


def test_existing_validation_and_evaluation_remain_strict(state: SummaryState) -> None:
    raw = "事实 [E0001,E0002]"
    with pytest.raises(CitationError):
        EvidenceService().validate(state, raw, ["E0001", "E0002"])
    assessment = check_text(state, raw, ["E0001", "E0002"], ["E0001", "E0002"])
    assert assessment["status"] == "fail"


class ScriptedLLM:
    """模拟模型响应，验证真实 Agent 和服务之间的数据传递。"""

    llm_provider = "test"

    def __init__(self, text: str) -> None:
        self.config = Configuration()
        self.text = text

    def invoke(self, **kwargs: Any) -> ChatCompletion:
        return ChatCompletion(
            id="test", created=0, object="chat.completion", model="test",
            choices=[{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": self.text},
            }],
        )

    def stream_text(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        # 故意把合写引用拆到多个分片，验证规范化在拼接完成之后执行。
        yield self.text[:10]
        yield self.text[10:]


def make_agent(text: str) -> SimpleAgent:
    return SimpleAgent("test", cast(MyLLM, ScriptedLLM(text)), config=Configuration())


@pytest.mark.parametrize("text", ["事实 [E0001, E0002]", "暂无可用信息"])
def test_summary_returns_prepared_text(state: SummaryState, text: str) -> None:
    before = asdict(state)
    agent = make_agent(text)
    result = SummarizationService(lambda: agent, Configuration()).summarize_task(state, 1)
    expected = "事实 [E0001][E0002]" if text.startswith("事实") else text
    assert result.text == expected
    assert result.citation_ids == (("E0001", "E0002") if text.startswith("事实") else ())
    assert asdict(state) == before
    assert agent.get_hsitory() == []


@pytest.mark.parametrize("streaming", [False, True])
def test_report_normalizes_complete_body_and_preserves_raw_draft(
    state: SummaryState, streaming: bool,
) -> None:
    raw = "事实 [E0001, E0002]"
    before = asdict(state)
    parts: list[str] = []
    agent = make_agent(raw)
    report = ReportingService(lambda: agent).generate_report(
        state, on_text=parts.append if streaming else None,
    )
    assert report.text.startswith("事实 [E0001][E0002]\n\n## 引用证据")
    assert report.citation_ids == ("E0001", "E0002")
    assert "https://example.com" in report.text
    assert "".join(parts) == (raw if streaming else "")
    assert asdict(state) == before
    assert agent.get_hsitory() == []
