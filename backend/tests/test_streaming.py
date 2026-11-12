"""流式链路的离线验证：真实 Agent/Reporter/图/SQLite/SSE，模型使用脚本分片。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletionChunk

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.api.errors import ApiError
from deep_researcher.api.manager import ResearchManager
from deep_researcher.api.routes import router
from deep_researcher.api.streaming import research_events
from deep_researcher.config import Configuration
from deep_researcher.events import EventStore
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import ResearchGraphState
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import Evidence, Source, SummaryState, TodoItem
from deep_researcher.services.evidence_service import CitationError
from deep_researcher.services.reporter import ReportingService


class ChunkStream:
    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[ChatCompletionChunk]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


def model(config: Configuration, body: str, reason: str | None = "stop") -> tuple[MyLLM, ChunkStream]:
    chunks = [
        ChatCompletionChunk(
            id="test", object="chat.completion.chunk", created=0, model="test",
            choices=[{"index": 0, "delta": {"content": part}, "finish_reason": None}],
        )
        for part in [body[:8], body[8:]]
    ]
    if reason is not None:
        chunks.append(ChatCompletionChunk(
            id="test", object="chat.completion.chunk", created=0, model="test",
            choices=[{"index": 0, "delta": {}, "finish_reason": reason}],
        ))
    stream = ChunkStream(chunks)
    llm = object.__new__(MyLLM)
    llm.config = config
    llm.model = "test"
    llm.llm_provider = "test"
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: stream)),
        close=lambda: None,
    )
    return llm, stream


def seed(state: SummaryState) -> None:
    state.todo_items = [TodoItem(
        id=1, title="Test task", intent="test", query="test", status="completed",
        summary="Supported fact [E0001]", evidence_ids=["E0001"], citation_ids=["E0001"],
    )]
    state.sources = {"S0001": Source(source_id="S0001", url="https://example.com", title="Source")}
    state.evidence = {"E0001": Evidence(
        evidence_id="E0001", source_id="S0001", task_id=1, text="Supported fact",
        retrieved_at="2026-11-11T00:00:00+00:00", query="test", backend="test", kind="search_snippet",
    )}


def workflow(tmp_path: Path, bodies: list[str]) -> tuple[GraphDeepResearcher, Configuration, list[str]]:
    config = Configuration(enable_notes=False, notes_workspace=str(tmp_path / "notes"))
    plan_calls: list[str] = []

    class OfflineNodes(ResearchNodes):
        def plan(self, state: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            plan_calls.append(state.run_id)
            seed(state)
            return {}

        def reporter_agent(self) -> SimpleAgent:
            llm, _ = model(self.config, bodies.pop(0))
            return SimpleAgent("offline", llm, config=self.config)

    return GraphDeepResearcher(config, data_dir=tmp_path, nodes_factory=OfflineNodes), config, plan_calls


def client_for(manager: ResearchManager) -> TestClient:
    app = FastAPI()
    app.state.research_manager = manager
    app.include_router(router, prefix="/api")

    @app.exception_handler(ApiError)
    async def error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code}})

    return TestClient(app)


def parse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for block in body.split("\n\n")
        if "event: research.event" in block
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


def test_graph_report_sse_and_delete(tmp_path: Path) -> None:
    researcher, config, calls = workflow(tmp_path, ["# Report\n\nSupported fact [E0001]"])
    output = researcher.run("Test")
    store = EventStore(tmp_path / "events.sqlite3")
    events = store.read_after(run_id=output.run_id, after_event_id=0)
    types = [event.type for event in events]
    assert types[0] == "research.started"
    assert types[-1] == "research.completed"
    assert types.index("report.delta") < types.index("report.validating") < types.index("report.generated")
    draft = "".join(event.data["text"] for event in events if event.type == "report.delta")
    assert draft == "# Report\n\nSupported fact [E0001]"
    assert output.report_markdown.startswith(draft)
    assert "https://example.com" in output.report_markdown
    assert researcher.read_run(output.run_id).report_ready
    assert len(calls) == 1

    manager = ResearchManager(config, data_dir=tmp_path)
    try:
        with client_for(manager) as client:
            url = f"/api/research/{output.run_id}/events"
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert len(parse_events(response.text)) == len(events)
            assert "event: research.sync" in response.text
            # Header 优先于 query；不会重发游标之前的内容。
            response = client.get(url + "?after=0", headers={"Last-Event-ID": str(events[2].event_id)})
            assert all(event["event_id"] > events[2].event_id for event in parse_events(response.text))
            assert client.get(url, headers={"Last-Event-ID": "invalid"}).status_code == 422
            assert client.get(url + "?after=-1").status_code == 422
            assert client.get("/api/research/" + "f" * 32 + "/events").status_code == 404
            assert client.delete(f"/api/research/{output.run_id}").status_code == 204
            assert store.read_after(run_id=output.run_id, after_event_id=0) == []
    finally:
        manager.close()


def test_failed_citation_does_not_commit_and_resume_replays_past_failure(tmp_path: Path) -> None:
    researcher, config, calls = workflow(tmp_path, ["Bad [E9999]", "Recovered [E0001]"])
    with pytest.raises(CitationError):
        researcher.run("Test")
    run_id = researcher.last_run_id
    assert run_id is not None
    snapshot = researcher.read_run(run_id)
    assert not snapshot.report_ready
    assert snapshot.state.structured_report is None
    assert snapshot.next_nodes == ("report",)

    result = researcher.resume(run_id)
    assert result.report_markdown.startswith("Recovered")
    assert len(calls) == 1  # 恢复没有重新规划。
    manager = ResearchManager(config, data_dir=tmp_path)
    try:
        with client_for(manager) as client:
            events = parse_events(client.get(f"/api/research/{run_id}/events").text)
        assert any(event["type"] == "research.failed" for event in events)
        assert events[-1]["type"] == "research.completed"
        starts = [event for event in events if event["type"] == "report.started"]
        assert len({event["execution_id"] for event in starts}) == 2
        assert len({event["data"]["generation_id"] for event in starts}) == 2
    finally:
        manager.close()


@pytest.mark.parametrize("reason", ["length", None, "content_filter"])
def test_abnormal_model_finish_rolls_back_agent_history(reason: str | None) -> None:
    config = Configuration()
    llm, chunks = model(config, "Incomplete body", reason)
    agent = SimpleAgent("test", llm, config=config)
    with pytest.raises(RuntimeError):
        list(agent.stream("prompt"))
    assert chunks.closed
    assert agent.get_hsitory() == []


def test_reporter_callback_failure_closes_model_stream() -> None:
    config = Configuration()
    llm, chunks = model(config, "Supported fact [E0001]")
    agent = SimpleAgent("test", llm, config=config)
    state = SummaryState(run_id="a" * 32)
    seed(state)

    def reject(text: str) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        ReportingService(lambda: agent).generate_report(state, on_text=reject)
    assert chunks.closed
    assert agent.get_hsitory() == []


def test_sse_replay_crosses_old_failure_and_multiple_pages(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    run_id = "b" * 32
    for index in range(205):
        store.append(
            run_id=run_id, execution_id="test",
            event_type="research.failed" if index == 1 else "report.delta",
            data={"text": str(index)},
        )
    manager = SimpleNamespace(
        events=store,
        get_snapshot=lambda _: SimpleNamespace(report_ready=True, info=SimpleNamespace(status="completed")),
        execution=lambda _: "idle",
    )

    class Connected:
        async def is_disconnected(self) -> bool:
            return False

    async def collect() -> str:
        return "".join([frame async for frame in research_events(Connected(), manager, run_id, 0)])

    events = parse_events(asyncio.run(collect()))
    assert len(events) == 205
    assert events[-1]["data"]["text"] == "204"


def test_sse_wait_is_cancellable(tmp_path: Path) -> None:
    manager = SimpleNamespace(
        events=EventStore(tmp_path / "missing.sqlite3"),
        get_snapshot=lambda _: SimpleNamespace(report_ready=False, info=SimpleNamespace(status="running")),
        execution=lambda _: "executing",
    )

    class Connected:
        async def is_disconnected(self) -> bool:
            return False

    async def cancel() -> None:
        stream = research_events(Connected(), manager, "a" * 32, 0)
        assert "retry:" in await anext(stream)
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await stream.aclose()

    asyncio.run(cancel())


def test_events_arrive_while_model_is_still_generating(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    release = Event()
    config = Configuration(enable_notes=False, notes_workspace=str(tmp_path / "notes"))

    class PausedStream(ChunkStream):
        def __iter__(self) -> Iterator[ChatCompletionChunk]:
            yield self.chunks[0]
            if not release.wait(5):
                raise RuntimeError("test stream was not released")
            yield from self.chunks[1:]

    class OfflineNodes(ResearchNodes):
        def plan(self, state: SummaryState, graph_state: ResearchGraphState) -> dict[str, Any]:
            seed(state)
            return {}

        def reporter_agent(self) -> SimpleAgent:
            llm, original = model(config, "Supported fact [E0001]")
            # 超过合并阈值的首段会立刻传出；后续分片等待测试主动放行。
            original.chunks[0].choices[0].delta.content = "Streaming " * 70
            stream = PausedStream(original.chunks)
            llm.client.chat.completions.create = lambda **kwargs: stream
            return SimpleAgent("test", llm, config=config)

    researcher = GraphDeepResearcher(config, data_dir=tmp_path, nodes_factory=OfflineNodes)
    run_id = researcher.create_run("Live test")
    store = EventStore(tmp_path / "events.sqlite3")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(researcher.resume, run_id)
        try:
            deadline = monotonic() + 4
            while monotonic() < deadline:
                events = store.read_after(run_id=run_id, after_event_id=0)
                if any(event.type == "report.delta" for event in events):
                    break
                sleep(0.02)
            else:
                pytest.fail("没有在模型结束前收到正文分片")
            assert not future.done()
            assert not researcher.read_run(run_id).report_ready
        finally:
            release.set()
        assert future.result(timeout=5).report_markdown
    assert researcher.read_run(run_id).report_ready


def test_event_storage_failure_keeps_final_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    researcher, _, _ = workflow(tmp_path, ["Supported [E0001]"])

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated event disk failure")

    monkeypatch.setattr(EventStore, "append", fail)
    result = researcher.run("Test")
    assert researcher.read_run(result.run_id).report_ready
    assert sum("事件写入中断" in record.message for record in caplog.records) == 1
