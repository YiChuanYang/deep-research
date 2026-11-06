"""验证笔记持久化、监听回填和任务摘要提交之间的协作."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessageFunctionToolCall

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration, SearchAPI
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.planner import PlanningService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.search import SearchService
from deep_researcher.services.summarizer import SummarizationService
from deep_researcher.services.tool_events import ToolCallTracker
from deep_researcher.tools.note_tool import NoteTool
from deep_researcher.tools.registry import ToolRegistry


class ScriptedLLM:
    """通过 SDK 响应驱动真实 Agent 工具循环，不调用外部接口."""

    llm_provider = "test"

    def __init__(
        self,
        responses: list[ChatCompletion | Exception | None],
    ) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> ChatCompletion | None:
        self.requests.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


def answer(
    text: str = "## 任务总结\n实际研究结论",
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "completion_answer",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": text,
                        "refusal": refusal,
                    },
                }
            ],
        }
    )


def tool_response(
    parameters: dict[str, Any],
    name: str = "note",
) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "completion_tool",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_001",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        parameters, ensure_ascii=False
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )


@pytest.fixture
def note_tool(tmp_path: Path) -> NoteTool:
    return NoteTool(config=Configuration(notes_workspace=str(tmp_path)))


@pytest.fixture
def task() -> TodoItem:
    return TodoItem(
        id=1,
        title="基准比较",
        intent="比较同一评测条件下的模型能力",
        query="model benchmark",
        sources_summary="* 来源 A：https://example.com/source",
    )


def make_service(
    note_tool: NoteTool,
    task: TodoItem,
    responses: list[ChatCompletion | Exception | None],
    *,
    enable_notes: bool = True,
) -> tuple[SummarizationService, SummaryState, ToolAwareAgent, ScriptedLLM]:
    config = Configuration(
        enable_notes=enable_notes,
        notes_workspace=str(note_tool._workspace),
    )
    state = SummaryState(
        research_topic="模型能力",
        todo_items=[task],
        search_contexts={task.id: "搜索素材"},
    )
    tracker = ToolCallTracker(state, config.notes_workspace)
    registry = ToolRegistry()
    if enable_notes:
        registry.register_tool(note_tool)
    llm = ScriptedLLM(responses)
    agent = ToolAwareAgent(
        name="test_summary",
        llm=cast(MyLLM, llm),
        config=config,
        tool_registry=registry,
        enable_tool_call=enable_notes,
        tool_call_listener=tracker.record,
    )

    def factory() -> ToolAwareAgent:
        return agent

    service = SummarizationService(
        factory,
        config,
        note_service=NoteService(
            config,
            note_tool if enable_notes else None,
        ),
    )
    return service, state, agent, llm


def test_note_create_read_append_and_replace(note_tool: NoteTool) -> None:
    created = note_tool.run(
        {
            "action": "create",
            "task_id": 1,
            "content": "第一轮证据",
        }
    )
    note_id = created["note_id"]
    assert Path(created["note_path"]).is_file()
    assert note_tool.run({"action": "read", "note_id": note_id})["task_id"] == 1

    appended = note_tool.run(
        {
            "action": "update",
            "note_id": note_id,
            "content": "新增发现",
        }
    )
    assert appended["content"] == "第一轮证据\n\n---\n新增发现"

    for _ in range(2):
        saved = note_tool.run(
            {
                "action": "update",
                "note_id": note_id,
                "mode": "replace",
                "content": "最新完整结论",
            }
        )
        assert saved["content"] == "最新完整结论"
    assert len(list(note_tool._workspace.glob("note_*.json"))) == 1


@pytest.mark.parametrize("content", [None, "", "   ", 123, []])
def test_create_rejects_invalid_content(
    note_tool: NoteTool,
    content: object,
) -> None:
    with pytest.raises(ValueError, match="content"):
        note_tool.run({"action": "create", "content": content})
    assert not list(note_tool._workspace.glob("note_*.json"))


@pytest.mark.parametrize("note_id", [None, "", "../other", "note_bad"])
def test_read_rejects_invalid_note_id(
    note_tool: NoteTool,
    note_id: object,
) -> None:
    with pytest.raises(ValueError, match="note_id"):
        note_tool.run({"action": "read", "note_id": note_id})


def test_update_cannot_reassign_task(note_tool: NoteTool) -> None:
    created = note_tool.run(
        {
            "action": "create",
            "task_id": 1,
            "content": "原有结论",
        }
    )
    with pytest.raises(ValueError, match="不属于"):
        note_tool.run(
            {
                "action": "update",
                "note_id": created["note_id"],
                "task_id": 2,
                "content": "错误任务的结论",
            }
        )
    assert (
        note_tool.run(
            {
                "action": "read",
                "note_id": created["note_id"],
            }
        )["content"]
        == "原有结论"
    )


def test_failed_atomic_replace_preserves_old_note(
    note_tool: NoteTool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = note_tool.run({"action": "create", "content": "原有结论"})
    old_bytes = Path(created["note_path"]).read_bytes()

    def fail_replace(self: Path, target: str | Path) -> Path:
        raise OSError("模拟磁盘替换失败")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="模拟磁盘替换失败"):
        note_tool.run(
            {
                "action": "update",
                "note_id": created["note_id"],
                "mode": "replace",
                "content": "新结论",
            }
        )
    assert Path(created["note_path"]).read_bytes() == old_bytes
    assert not list(note_tool._workspace.glob("*.tmp"))


def test_summary_creates_real_note_without_model_tool_call(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    service, state, agent, _ = make_service(note_tool, task, [answer()])
    summary = service.summarize_task(state, task.id)
    saved = note_tool.run({"action": "read", "note_id": task.note_id})

    assert not agent.enable_tool_call
    assert saved["content"].startswith(f"# 任务 {task.id}: {task.title}")
    assert f"## 摘要\n{summary}" in saved["content"]
    assert f"## 参考来源\n{task.sources_summary}" in saved["content"]
    assert task.summary == summary
    assert task.status == "completed"
    assert agent._history == []


def test_summary_agent_is_not_given_note_tool_schema(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    service, state, _, llm = make_service(note_tool, task, [answer()])
    service.summarize_task(state, task.id)

    assert llm.requests[0]["tools"] == []
    assert len(list(note_tool._workspace.glob("note_*.json"))) == 1
    saved = note_tool.run({"action": "read", "note_id": task.note_id})
    assert saved["content"].startswith(f"# 任务 {task.id}: {task.title}")
    assert "## 摘要\n## 任务总结" in saved["content"]


def test_existing_note_is_read_into_prompt_and_replaced(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    created = note_tool.run(
        {
            "action": "create",
            "task_id": task.id,
            "content": "上一轮研究证据",
        }
    )
    task.note_id = created["note_id"]
    service, state, _, llm = make_service(
        note_tool,
        task,
        [answer("整合后的结论"), answer("整合后的结论")],
    )
    state.search_contexts[task.id] = "本轮新素材"
    for _ in range(2):
        service.summarize_task(state, task.id)

    assert "上一轮研究证据" in llm.requests[0]["messages"][-1]["content"]
    assert "本轮新素材" in llm.requests[0]["messages"][-1]["content"]
    assert task.note_id == created["note_id"]
    saved = note_tool.run({"action": "read", "note_id": task.note_id})
    assert saved["content"].count("整合后的结论") == 1


def test_missing_note_is_recreated(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    task.note_id = "note_000000000000"
    service, state, _, _ = make_service(note_tool, task, [answer()])
    service.summarize_task(state, task.id)
    assert task.note_id != "note_000000000000"
    assert task.note_path is not None and Path(task.note_path).is_file()
    assert any("不存在" in notice for notice in task.notices)


def test_corrupt_note_is_not_silently_overwritten(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    task.note_id = "note_111111111111"
    path = note_tool._workspace / f"{task.note_id}.json"
    path.write_text("{broken", encoding="utf-8")
    service, state, _, llm = make_service(note_tool, task, [answer()])
    with pytest.raises(json.JSONDecodeError):
        service.summarize_task(state, task.id)
    assert path.read_text(encoding="utf-8") == "{broken"
    assert llm.requests == []
    assert task.status == "failed"


@pytest.mark.parametrize(
    "response",
    [
        None,
        RuntimeError("接口失败"),
        answer("", finish_reason="length"),
        answer("部分答复", finish_reason="length"),
        answer("", refusal="无法回答"),
        answer("<think>仅有思考</think>"),
    ],
)
def test_failed_generation_does_not_commit_note(
    note_tool: NoteTool,
    task: TodoItem,
    response: ChatCompletion | Exception | None,
) -> None:
    created = note_tool.run(
        {
            "action": "create",
            "task_id": task.id,
            "content": "上次有效摘要",
        }
    )
    task.note_id = created["note_id"]
    task.summary = "上次有效摘要"
    service, state, agent, _ = make_service(note_tool, task, [response])
    with pytest.raises((RuntimeError, ValueError)):
        service.summarize_task(state, task.id)
    assert task.status == "failed"
    assert task.summary == "上次有效摘要"
    assert (
        note_tool.run(
            {
                "action": "read",
                "note_id": task.note_id,
            }
        )["content"]
        == "上次有效摘要"
    )
    assert agent._history == []


def test_persistence_failure_does_not_mark_task_completed(
    note_tool: NoteTool,
    task: TodoItem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(data: dict[str, Any]) -> None:
        raise OSError("保存失败")

    service, state, _, _ = make_service(note_tool, task, [answer()])
    monkeypatch.setattr(note_tool, "_save", fail_write)
    with pytest.raises(OSError, match="保存失败"):
        service.summarize_task(state, task.id)
    assert task.status == "failed"
    assert task.summary is None
    assert task.note_id is None


def test_disabled_notes_skip_guidance_and_storage(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    task.note_id = "note_000000000000"
    service, state, _, llm = make_service(
        note_tool,
        task,
        [answer()],
        enable_notes=False,
    )
    service.summarize_task(state, task.id)
    assert "笔记协作" not in llm.requests[0]["messages"][-1]["content"]
    assert not list(note_tool._workspace.glob("note_*.json"))
    assert task.status == "completed"


def test_tracker_uses_read_result_metadata_and_filters_failed_events(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    state = SummaryState(todo_items=[task])
    tracker = ToolCallTracker(state, str(note_tool._workspace))
    created = note_tool.run(
        {
            "action": "create",
            "task_id": task.id,
            "content": "任务笔记",
        }
    )
    result = note_tool.run({"action": "read", "note_id": created["note_id"]})
    event: dict[str, Any] = {
        "tool_name": "note",
        "tool_call_id": "call_read",
        "success": False,
        "clean_parameters": {"action": "read", "note_id": created["note_id"]},
        "result": json.dumps(result),
    }
    tracker.record(event)
    assert task.note_id is None
    event["success"] = True
    tracker.record(event)
    assert task.note_id == created["note_id"]
    assert task.summary is None and task.status == "pending"

    report = note_tool.run(
        {
            "action": "create",
            "note_type": "conclusion",
            "content": "完整报告",
        }
    )
    tracker.record({**event, "result": json.dumps(report)})
    assert state.report_note_id == report["note_id"]
    assert task.note_id == created["note_id"]


def test_listener_failure_keeps_successful_tool_message(
    note_tool: NoteTool,
    task: TodoItem,
) -> None:
    _, _, agent, _ = make_service(note_tool, task, [])

    def broken_listener(event: dict[str, Any]) -> None:
        raise RuntimeError("监听失败")

    agent.tool_call_listener = broken_listener
    calls = (
        tool_response(
            {
                "action": "create",
                "task_id": task.id,
                "content": "实际正文",
            }
        )
        .choices[0]
        .message.tool_calls
    )
    assert calls
    call = cast(ChatCompletionMessageFunctionToolCall, calls[0])
    result = json.loads(agent._execute_tool_call(call).content or "{}")
    assert result["content"] == "实际正文"
    assert Path(result["note_path"]).is_file()


def test_planner_filters_unrelated_events_and_restores_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedLLM([])
    forwarded: list[dict[str, Any]] = []
    listener: Callable[[dict[str, Any]], None] = forwarded.append
    agent = ToolAwareAgent(
        name="planner",
        llm=cast(MyLLM, llm),
        tool_call_listener=listener,
    )
    service = PlanningService(agent)

    def fake_run(input_text: str, **kwargs: Any) -> str:
        events = [
            {
                "tool_name": "generate_research_tasks",
                "success": True,
                "raw_parameters": json.dumps(
                    {
                        "tasks": [
                            {
                                "title": "正确任务",
                                "intent": "目标",
                                "query": "检索词",
                            }
                        ]
                    }
                ),
            },
            {
                "tool_name": "note",
                "success": True,
                "raw_parameters": '{"action": "read"}',
            },
            {
                "tool_name": "generate_research_tasks",
                "success": False,
                "raw_parameters": '{"tasks": []}',
            },
        ]
        assert agent.tool_call_listener is not None
        for event in events:
            agent.tool_call_listener(event)
        return "规划完成"

    monkeypatch.setattr(agent, "run", fake_run)
    tasks = service.plan_todo_list(SummaryState(research_topic="主题"))
    assert tasks[0].title == "正确任务"
    assert agent.tool_call_listener is listener
    assert len(forwarded) == 3


def test_agent_iteration_limit_raises_instead_of_returning_error_text() -> None:
    llm = ScriptedLLM([tool_response({"tasks": []}, "unknown")])
    agent = SimpleAgent(name="test", llm=cast(MyLLM, llm))
    with pytest.raises(RuntimeError, match="迭代已达到上限"):
        agent.run("请求", max_tool_iterations=0)


def test_search_service_writes_results_into_shared_state(
    monkeypatch: pytest.MonkeyPatch,
    task: TodoItem,
) -> None:
    import deep_researcher.services.search as search_module

    config = Configuration(search_api=SearchAPI.DUCKDUCKGO)
    state = SummaryState(research_topic="模型能力", todo_items=[task])
    payload = {
        "backend": "duckduckgo",
        "answer": None,
        "notices": [],
        "results": [
            {
                "title": "来源标题",
                "url": "https://example.com/source",
                "content": "来源内容",
            }
        ],
    }

    def fake_dispatch(query, config, loop_count, *, search_tool=None):
        assert query == task.query
        return payload, [], None, "duckduckgo"

    monkeypatch.setattr(search_module, "dispatch_search", fake_dispatch)
    SearchService(config).search_task(state, task.id)

    assert state.search_contexts[task.id]
    assert task.sources_summary == "* 来源标题 : https://example.com/source"
    assert state.research_loop_count == 1


def test_reporter_reads_completed_tasks_from_state() -> None:
    state = SummaryState(
        research_topic="模型能力",
        todo_items=[
            TodoItem(
                id=1,
                title="基准比较",
                intent="比较模型能力",
                query="model benchmark",
                status="completed",
                summary="已验证的摘要",
                sources_summary="* 来源：https://example.com/source",
            )
        ],
    )
    llm = ScriptedLLM([answer("# 研究报告\n主要结论")])
    reporter = ReportingService(
        lambda: SimpleAgent(
            name="reporter-test",
            llm=cast(MyLLM, llm),
        )
    )

    report = reporter.generate_report(state)

    assert report == "# 研究报告\n主要结论"
    assert state.structured_report == report
    assert state.running_summary == report
    prompt = llm.requests[0]["messages"][-1]["content"]
    assert "已验证的摘要" in prompt
    assert "https://example.com/source" in prompt
