"""离线验证查询改写、真实检索输入、证据溯源和检查点恢复。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest
from openai.types.chat import ChatCompletion

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration, SearchAPI
from deep_researcher.events import EventStore
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import ResearchGraphState, decode_state, encode_state
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.prompts import query_rewriter_system_prompt
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.query_rewriter import QueryRewriteService
from deep_researcher.services.search import SearchService
from deep_researcher.services.summarizer import SummarizationService
from deep_researcher.tools.search_tool import SearchTool

ORIGINAL = "报告工具 引用流程设计"
REWRITTEN = "大模型研究报告 引用溯源 证据片段 数据关联"


class ScriptedLLM:
    llm_provider = "offline"

    def __init__(self, response: str | BaseException) -> None:
        self.config = Configuration()
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> ChatCompletion:
        self.requests.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return ChatCompletion(
            id="test", model="offline", created=0, object="chat.completion",
            choices=[{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": self.response,
            }}],
        )

    def stream_text(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        if isinstance(self.response, BaseException):
            raise self.response
        yield self.response


def agent_for(model: ScriptedLLM) -> SimpleAgent:
    return SimpleAgent(
        "offline-query", cast(MyLLM, model), config=model.config,
        sys_prompt=query_rewriter_system_prompt,
    )


@pytest.fixture
def state() -> SummaryState:
    return SummaryState(
        run_id="a" * 32,
        research_topic="为大模型联网研究工具设计可追溯引用，不讨论法律存证。",
        todo_items=[TodoItem(
            id=1, title="引用关联", intent="说明搜索片段如何关联摘要和报告", query=ORIGINAL,
        )],
    )


def test_rewrite_passes_context_in_one_call_without_state_mutation(
    state: SummaryState, caplog: pytest.LogCaptureFixture,
) -> None:
    llm = ScriptedLLM(REWRITTEN)
    agent = agent_for(llm)
    before = asdict(state)
    with caplog.at_level(logging.INFO):
        query = QueryRewriteService(lambda: agent, Configuration()).rewrite_task(state, 1)
    assert query == REWRITTEN
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request["tools"] == []
    assert request["max_tokens"] == 256
    payload = json.loads(request["messages"][-1]["content"])
    assert payload == {
        "research_topic": state.research_topic,
        "task_title": state.get_task(1).title,
        "task_intent": state.get_task(1).intent,
        "original_query": ORIGINAL,
    }
    assert asdict(state) == before
    assert agent.get_hsitory() == []
    record = next(record for record in caplog.records if getattr(record, "event", None) == "query_rewrite_completed")
    assert record.original_query == ORIGINAL
    assert record.search_query == REWRITTEN


@pytest.mark.parametrize("response", [
    "", "\n", "一条查询\n另一条查询", "x" * 201,
    '{"query":"RAG"}', "```RAG```", "<think>理由</think> RAG", "- RAG query",
    RuntimeError("model offline"),
])
def test_invalid_or_failed_rewrite_falls_back(
    state: SummaryState, response: str | BaseException, caplog: pytest.LogCaptureFixture,
) -> None:
    llm = ScriptedLLM(response)
    agent = agent_for(llm)
    with caplog.at_level(logging.WARNING):
        assert QueryRewriteService(lambda: agent, Configuration()).rewrite_task(state, 1) == ORIGINAL
    assert len(llm.requests) == 1
    assert agent.get_hsitory() == []
    assert state.get_task(1).search_query is None
    assert any(getattr(record, "event", None) == "query_rewrite_fallback" for record in caplog.records)


def test_disabled_and_saved_query_do_not_create_agent(state: SummaryState) -> None:
    def forbidden() -> SimpleAgent:
        raise AssertionError("不应创建模型")

    service = QueryRewriteService(forbidden, Configuration(enable_query_rewrite=False))
    assert service.rewrite_task(state, 1) == ORIGINAL
    state.get_task(1).search_query = REWRITTEN
    assert QueryRewriteService(forbidden, Configuration()).rewrite_task(state, 1) == REWRITTEN


def test_interrupt_is_not_converted_to_fallback(state: SummaryState) -> None:
    agent = agent_for(ScriptedLLM(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        QueryRewriteService(lambda: agent, Configuration()).rewrite_task(state, 1)
    assert agent.get_hsitory() == []


def test_config_snapshot_and_old_checkpoint_compatibility(state: SummaryState) -> None:
    enabled = Configuration()
    assert enabled.enable_query_rewrite
    snapshot = enabled.snapshot()
    assert snapshot["enable_query_rewrite"] is True
    assert Configuration(enable_query_rewrite=False).for_resume(snapshot).enable_query_rewrite
    snapshot.pop("enable_query_rewrite")
    assert not enabled.for_resume(snapshot).enable_query_rewrite
    assert not Configuration.from_env({"enable_query_rewrite": False}, env_file=None).enable_query_rewrite

    raw = encode_state(state)
    raw["todo_items"][0].pop("search_query")
    restored = decode_state(raw)
    assert restored.get_task(1).effective_query == ORIGINAL
    assert "effective_query" not in encode_state(restored)["todo_items"][0]
    raw["todo_items"][0]["search_query"] = ""
    with pytest.raises(ValueError, match="搜索查询"):
        decode_state(raw)


class RecordingSearch:
    backend = "duckduckgo"

    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("temporary search failure")
        return {"results": [{"url": "https://example.com", "title": "资料", "content": "可核查事实"}], "notices": []}


def test_search_summary_and_note_use_actual_query(state: SummaryState, tmp_path: Path) -> None:
    config = Configuration(search_api=SearchAPI.DUCKDUCKGO, notes_workspace=str(tmp_path))
    state.get_task(1).search_query = REWRITTEN
    tool = RecordingSearch()
    service = SearchService(config, search_tool=cast(SearchTool, tool))
    batch = service.search_task(state, 1)
    assert tool.calls == [{"input": REWRITTEN, "max_results": 5}]
    assert state.evidence == {}  # 服务不直接更新业务状态。
    state.sources, state.evidence = batch.sources, batch.evidence
    task = state.get_task(1)
    task.evidence_ids = batch.evidence_ids
    assert all(item.query == REWRITTEN for item in state.evidence.values())
    # 同查询、同材料复用编号；查询改变则保留新的取得路径。
    assert service.search_task(state, 1).evidence_ids == batch.evidence_ids
    task.search_query = "新的查询"
    second = service.search_task(state, 1)
    assert len(second.evidence) == 2
    assert batch.evidence_ids[0] not in second.evidence_ids
    task.search_query = REWRITTEN

    llm = ScriptedLLM("可核查事实 [E0001]")
    result = SummarizationService(lambda: agent_for(llm), config).summarize_task(state, 1)
    assert f"检索关键词：{REWRITTEN}" in llm.requests[0]["messages"][-1]["content"]
    task.summary, task.citation_ids = result.text, list(result.citation_ids)
    note = NoteService(config).save_task_note(state, 1)
    assert note is not None
    assert f"规划查询：{ORIGINAL}" in note.content
    assert f"检索查询：{REWRITTEN}" in note.content
    assert task.query == ORIGINAL


@pytest.mark.parametrize("response", [REWRITTEN, RuntimeError("model unavailable")])
def test_checkpoint_reuses_rewritten_or_fallback_query_after_search_failure(
    state: SummaryState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    response: str | BaseException,
) -> None:
    config = Configuration(search_api=SearchAPI.DUCKDUCKGO, enable_notes=False)
    tool = RecordingSearch(fail_first=True)
    query_models: list[ScriptedLLM] = []

    class OfflineNodes(ResearchNodes):
        def plan(self, value: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            value.todo_items = [TodoItem(
                id=1, title="引用关联", intent="解释证据关联", query=ORIGINAL,
            ), TodoItem(id=2, title="本轮未执行", intent="test", query="unused")]
            return {}

        def query_agent(self) -> SimpleAgent:
            llm = ScriptedLLM(response)
            query_models.append(llm)
            return agent_for(llm)

        def summary_agent(self) -> SimpleAgent:
            return agent_for(ScriptedLLM("可核查事实 [E0001]"))

        def reporter_agent(self) -> SimpleAgent:
            return agent_for(ScriptedLLM("研究结论 [E0001]"))

    # 运行真实搜索服务和证据整理，只替换外部搜索工具。
    monkeypatch.setattr(
        "deep_researcher.graph.graph_nodes.SearchService",
        lambda configuration: SearchService(configuration, search_tool=cast(SearchTool, tool)),
    )
    researcher = GraphDeepResearcher(config, data_dir=tmp_path, nodes_factory=OfflineNodes)
    with pytest.raises(RuntimeError, match="temporary search failure"):
        researcher.run(state.research_topic, max_tasks=1)
    run_id = researcher.last_run_id
    assert run_id is not None
    saved = researcher.read_run(run_id)
    assert saved.next_nodes == ("search",)
    assert saved.state is not None
    expected = REWRITTEN if isinstance(response, str) else ORIGINAL
    assert saved.state.get_task(1).search_query == expected
    assert saved.state.get_task(1).query == ORIGINAL
    assert saved.state.get_task(2).search_query is None
    assert len(query_models) == 1

    result = researcher.resume(run_id)
    assert researcher.read_run(run_id).report_ready
    assert len(query_models) == 1  # 搜索失败恢复不会重新请求改写模型。
    assert [call["input"] for call in tool.calls] == [expected, expected]
    assert result.evidence["E0001"].query == expected
    events = EventStore(tmp_path / "events.sqlite3").read_after(run_id=run_id, after_event_id=0)
    assert sum(event.type == "node.started" and event.data["node"] == "rewrite_query" for event in events) == 1
