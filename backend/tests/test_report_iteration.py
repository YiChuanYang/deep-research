"""报告反思闭环：真实图、搜索整理、引用校验、SQLite 和 API；外部请求使用固定回复。"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from openai import AuthenticationError, RateLimitError
from openai.types.chat import ChatCompletion

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.api.manager import ResearchManager
from deep_researcher.config import Configuration, SearchAPI
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import ResearchGraphState
from deep_researcher.graph.report_cycle import update
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.report_iteration import (
    ReportDraft,
    ReportIteration,
    ReportReview,
    ReviewOutcome,
)
from deep_researcher.services.report_reflection import ReportReflectionService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.search import SearchService
from deep_researcher.tools.search_tool import SearchTool
from evals.checks import inspect_state
from tests.test_streaming import client_for, parse_events, seed

BODY = "# Report\n\nFirst fact [E0001]"
MORE = "# Report\n\nFirst fact [E0001]\n\nAdditional fact [E0002]"
PASS = {"issues": []}


def gap(number: int = 1) -> dict[str, Any]:
    return {
        "issues": [
            {
                "kind": "missing_evidence",
                "reason": f"Need comparison {number}",
                "question": f"comparison question {number}",
                "query": f"comparison benchmark {number}",
            }
        ]
    }


BAD = {
    "issues": [
        {"kind": "unsupported", "paragraph_id": "P0002", "reason": "Overstated claim"}
    ]
}
OMISSION = {
    "issues": [
        {
            "kind": "omission",
            "reason": "Mention the condition",
            "evidence_ids": ["E0001"],
        }
    ]
}


class TextAgent:
    def __init__(self, text: str | Exception) -> None:
        self.text = text

    def run(self, **kwargs: Any) -> str:
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    def stream(self, **kwargs: Any) -> Iterator[str]:
        yield self.run(**kwargs)

    def clear_history(self) -> None:
        pass


def workflow(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    reviews: list[dict[str, Any] | Exception | ReviewOutcome],
    reports: list[str | Exception],
    *,
    repeated: bool = False,
    fail_search: bool = False,
    **options: Any,
) -> tuple[GraphDeepResearcher, Counter[str]]:
    calls: Counter[str] = Counter()
    cfg = Configuration(enable_web_rag=False, review_output_mode="tool",
        enable_notes=False,
        enable_query_rewrite=False,
        search_api=SearchAPI.DUCKDUCKGO,
        notes_workspace=str(directory / "notes"),
        **options,
    )

    class Search:
        backend = "duckduckgo"

        def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
            calls["search"] += 1
            if fail_search and calls["search"] == 2:
                raise RuntimeError("search interrupted")
            text = (
                "First fact"
                if calls["search"] == 1 or repeated
                else f"Additional fact {calls['search']}"
            )
            return {
                "results": [
                    {
                        "url": f"https://example.com/{calls['search']}",
                        "title": "Source",
                        "content": text,
                    }
                ]
            }

    tool = Search()
    monkeypatch.setattr(
        "deep_researcher.graph.graph_nodes.SearchService",
        lambda config: SearchService(config, search_tool=cast(SearchTool, tool)),
    )

    class Reviewer:
        def review(
            self, state: SummaryState, draft: ReportDraft, *, allow_repair: bool
        ) -> ReviewOutcome:
            calls["review"] += 1
            if not allow_repair:
                calls["repair_budget_off"] += 1
            assert state.structured_report is None
            response = reviews.pop(0)
            if isinstance(response, Exception):
                raise response
            if isinstance(response, ReviewOutcome):
                assert allow_repair or response.repairs_used == 0
                return response
            return ReviewOutcome(review=ReportReview.model_validate(response))

    class Nodes(ResearchNodes):
        def plan(
            self, state: SummaryState, values: ResearchGraphState
        ) -> dict[str, Any]:
            calls["plan"] += 1
            state.todo_items = [
                TodoItem(id=1, title="Initial", intent="Facts", query="initial query"),
                TodoItem(id=2, title="Not selected", intent="Extra", query="unused"),
            ]
            return update(ReportIteration(initial_task_ids=[1]))

        def summary_agent(self) -> SimpleAgent:
            calls["summary"] += 1
            return cast(SimpleAgent, TextAgent(f"Fact [E{calls['summary']:04d}]"))

        def reporter_agent(self) -> SimpleAgent:
            calls["report"] += 1
            return cast(SimpleAgent, TextAgent(reports.pop(0)))

        def report_reflection_service(self) -> ReportReflectionService:
            return cast(ReportReflectionService, Reviewer())

    return GraphDeepResearcher(cfg, data_dir=directory, nodes_factory=Nodes), calls


def test_supplement_bypasses_initial_limit_and_is_included_in_report_api_and_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(tmp_path, monkeypatch, [gap(), PASS], [BODY, MORE])
    result = researcher.run("test", max_tasks=1)
    assert calls == {"plan": 1, "search": 2, "summary": 2, "report": 2, "review": 2}
    assert [(task.id, task.status) for task in result.todo_items] == [
        (1, "completed"),
        (2, "pending"),
        (3, "completed"),
    ]
    snapshot = researcher.read_run(result.run_id)
    cycle = snapshot.report_iteration
    assert snapshot.report_ready and snapshot.selected_task_ids == [1, 3]
    assert (
        len(cycle.drafts) == 2
        and cycle.drafts[0].text == BODY
        and cycle.status == "accepted"
    )
    assert cycle.supplements[0].novel_evidence_ids == ["E0002"]
    assert result.evidence["E0002"].task_id == 3
    assert inspect_state(snapshot.state)["overall_check"]["status"] == "pass"
    manager = ResearchManager(researcher._base_config, data_dir=tmp_path)
    try:
        with client_for(manager) as client:
            prefix = f"/api/research/{result.run_id}"
            progress = client.get(prefix).json()
            assert progress["selected_tasks"] == progress["completed_tasks"] == 2
            assert progress["report_iteration"]["supplemental_total"] == 1
            assert [task["selected"] for task in progress["tasks"]] == [
                True,
                False,
                True,
            ]
            history = client.get(prefix + "/iterations").json()["iteration"]
            assert history["drafts"][0]["text"] == BODY
            events = parse_events(client.get(prefix + "/events").text)
            generations = {
                event["data"]["generation_id"]
                for event in events
                if event["type"] == "report.started"
            }
            assert len(generations) == 2
            assert client.get(prefix + "/result").status_code == 200
    finally:
        manager.close()


def test_existing_material_revision_does_not_search_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(
        tmp_path, monkeypatch, [OMISSION, PASS], [BODY, BODY + "\n\nCondition [E0001]"]
    )
    output = researcher.run("test", max_tasks=1)
    assert calls["search"] == 1 and calls["report"] == 2
    assert not researcher.read_run(output.run_id).report_iteration.supplements


def test_recovery_does_not_duplicate_supplement_or_initial_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(
        tmp_path, monkeypatch, [gap(), PASS], [BODY, MORE], fail_search=True
    )
    with pytest.raises(RuntimeError, match="search interrupted"):
        researcher.run("test", max_tasks=1)
    run_id = researcher.last_run_id
    snapshot = researcher.read_run(run_id)
    assert (
        snapshot.next_nodes == ("search",)
        and len(snapshot.report_iteration.supplements) == 1
    )
    assert snapshot.state.structured_report is None and not snapshot.report_ready
    restarted = GraphDeepResearcher(
        researcher._base_config,
        data_dir=tmp_path,
        nodes_factory=researcher._nodes_factory,
    )
    output = restarted.resume(run_id)
    assert (
        output.run_id == run_id and len(output.todo_items) == 3 and calls["plan"] == 1
    )
    assert calls["search"] == 3 and calls["summary"] == 2


def test_report_failure_preserves_draft_and_reuses_completed_supplement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(
        tmp_path,
        monkeypatch,
        [gap(), PASS],
        [BODY, RuntimeError("report interrupted"), MORE],
    )
    with pytest.raises(RuntimeError, match="report interrupted"):
        researcher.run("test", max_tasks=1)
    saved = researcher.read_run(researcher.last_run_id)
    assert (
        saved.next_nodes == ("report",)
        and saved.report_iteration.drafts[0].text == BODY
    )
    output = researcher.resume(researcher.last_run_id)
    assert (
        calls["search"] == 2
        and calls["summary"] == 2
        and output.report_markdown.startswith(MORE)
    )


def test_no_novel_content_stops_search_and_delivers_explicit_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(
        tmp_path, monkeypatch, [gap(), gap(2)], [BODY, MORE], repeated=True
    )
    result = researcher.run("test", max_tasks=1)
    cycle = researcher.read_run(result.run_id).report_iteration
    assert cycle.status == "limited" and calls["search"] == 2
    assert not cycle.supplements[0].novel_evidence_ids
    assert "没有取得内容不同的新证据" in cycle.stop_reason
    assert (
        "有限交付" in result.report_markdown
        and "Need comparison 2" in result.report_markdown
    )


def test_round_budget_and_global_supplement_budget_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(
        tmp_path,
        monkeypatch,
        [gap(), gap(2), gap(3)],
        [BODY, MORE, MORE + "\n\nNew [E0003]"],
    )
    result = researcher.run("test", max_tasks=1)
    cycle = researcher.read_run(result.run_id).report_iteration
    assert (
        cycle.revision == 2 and len(cycle.supplements) == 2 and len(cycle.drafts) == 3
    )
    assert cycle.status == "limited" and calls["search"] == 3 and calls["review"] == 3


def test_unresolved_critical_report_is_not_published_and_api_blocks_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, _ = workflow(
        tmp_path, monkeypatch, [BAD], [BODY], report_max_revisions=0
    )
    with pytest.raises(RuntimeError, match="不能发布"):
        researcher.run("test", max_tasks=1)
    run_id = researcher.last_run_id
    snapshot = researcher.read_run(run_id)
    assert (
        snapshot.next_nodes == ("block_report",)
        and snapshot.report_iteration.status == "blocked"
    )
    assert (
        snapshot.state.structured_report is None
        and snapshot.report_iteration.drafts[0].text == BODY
    )
    manager = ResearchManager(researcher._base_config, data_dir=tmp_path)
    try:
        with client_for(manager) as client:
            assert client.get(f"/api/research/{run_id}/result").status_code == 409
            response = client.post(f"/api/research/{run_id}/resume", json={})
            assert (
                response.status_code == 409
                and response.json()["error"]["code"] == "report_review_blocked"
            )
    finally:
        manager.close()


@pytest.mark.parametrize("known_problem", [False, True])
def test_review_unavailable_never_fakes_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, known_problem: bool
) -> None:
    unavailable = ReviewOutcome(error="protocol error", repairs_used=1)
    reviews = [BAD, unavailable] if known_problem else [unavailable]
    reports = [BODY, BODY + "\n\nRevision [E0001]"] if known_problem else [BODY]
    researcher, _ = workflow(tmp_path, monkeypatch, reviews, reports)
    if known_problem:
        with pytest.raises(RuntimeError, match="不能发布"):
            researcher.run("test", max_tasks=1)
    else:
        result = researcher.run("test", max_tasks=1)
        assert "未完成语义复核" in result.report_markdown
    cycle = researcher.read_run(researcher.last_run_id).report_iteration
    assert cycle.protocol_repairs_used == 1 and cycle.drafts[-1].review is None
    assert cycle.status == ("blocked" if known_problem else "unreviewed")


def test_unchanged_report_does_not_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(tmp_path, monkeypatch, [OMISSION], [BODY, BODY])
    result = researcher.run("test", max_tasks=1)
    assert calls["review"] == 1 and calls["report"] == 2
    assert researcher.read_run(result.run_id).report_iteration.status == "limited"


def test_config_keeps_old_runs_on_v2() -> None:
    cfg = Configuration(enable_web_rag=False, review_output_mode="tool", )
    assert cfg.enable_report_reflection
    assert not cfg.for_resume({}).enable_report_reflection
    assert cfg.for_resume(cfg.snapshot()).report_max_revisions == 2


def test_one_protocol_repair_is_shared_across_all_report_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repaired = ReviewOutcome(review=ReportReview.model_validate(gap()), repairs_used=1)
    researcher, calls = workflow(tmp_path, monkeypatch, [repaired, PASS], [BODY, MORE])
    output = researcher.run("test", max_tasks=1)
    assert calls["repair_budget_off"] == 1
    assert (
        researcher.read_run(output.run_id).report_iteration.protocol_repairs_used == 1
    )


def test_duplicate_gap_stops_even_when_search_found_new_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(tmp_path, monkeypatch, [gap(), gap()], [BODY, MORE])
    output = researcher.run("test", max_tasks=1)
    cycle = researcher.read_run(output.run_id).report_iteration
    assert cycle.supplements[0].novel_evidence_ids and calls["search"] == 2
    assert cycle.status == "limited" and len(cycle.supplements) == 1
    assert "问题或查询已尝试" in cycle.stop_reason


def test_final_note_failure_resumes_without_regenerating_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(tmp_path, monkeypatch, [PASS], [BODY])
    writes: list[int] = []

    def save(*args: Any, **kwargs: Any) -> None:
        writes.append(1)
        if len(writes) == 1:
            raise OSError("note storage failed")

    monkeypatch.setattr(
        "deep_researcher.services.note_service.NoteService.save_report_note", save
    )
    with pytest.raises(OSError, match="storage failed"):
        researcher.run("test", max_tasks=1)
    saved = researcher.read_run(researcher.last_run_id)
    assert saved.next_nodes == ("save_report",) and not saved.report_ready
    assert saved.state.structured_report and saved.report_iteration.status == "accepted"
    researcher.resume(researcher.last_run_id)
    assert calls["report"] == calls["review"] == 1 and len(writes) == 2


def test_service_does_not_truncate_over_budget_materials_and_fake_review() -> None:
    state = SummaryState()
    seed(state)
    state.evidence["E0001"] = replace(state.evidence["E0001"], text="material " * 2000)
    model = Model([PASS])
    service = ReportReflectionService(
        cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", report_review_max_chars=1000)
    )
    outcome = service.review(
        state, ReportDraft(revision=0, text=BODY), allow_repair=True
    )
    assert outcome.review is None and "输入预算" in outcome.error and not model.requests


def test_review_sees_completed_task_evidence_not_yet_used_in_report() -> None:
    state = SummaryState()
    seed(state)
    state.evidence["E0002"] = replace(
        state.evidence["E0001"], evidence_id="E0002", text="Extra comparison", task_id=2
    )
    state.todo_items.append(
        TodoItem(
            id=2,
            title="Extra",
            intent="Compare",
            query="extra",
            status="completed",
            summary="Extra comparison [E0002]",
            evidence_ids=["E0002"],
            citation_ids=["E0002"],
        )
    )
    model = Model(
        [
            {
                "issues": [
                    {
                        "kind": "omission",
                        "reason": "Missing comparison",
                        "evidence_ids": ["E0002"],
                    }
                ]
            }
        ]
    )
    outcome = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", )).review(
        state, ReportDraft(revision=0, text=BODY), allow_repair=True
    )
    payload = json.loads(model.requests[0]["messages"][1]["content"])
    assert {item["evidence_id"] for item in payload["evidence"]} == {"E0001", "E0002"}
    assert outcome.review.issues[0].kind == "omission"


def test_report_revision_receives_same_paragraph_anchors_as_review() -> None:
    state = SummaryState()
    seed(state)
    model = Model([BAD])
    review = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", )).review(
        state,
        ReportDraft(revision=0, text=BODY),
        allow_repair=True,
    )
    payload = json.loads(model.requests[0]["messages"][1]["content"])
    prompts: list[str] = []

    class Writer(TextAgent):
        def run(self, **kwargs: Any) -> str:
            prompts.append(kwargs["input_text"])
            return super().run(**kwargs)

    ReportingService(lambda: cast(SimpleAgent, Writer(BODY))).generate_report(
        state,
        previous_report=BODY,
        feedback=review.review.model_dump_json(),
        include_references=False,
    )
    assert json.dumps(payload["report_paragraphs"], ensure_ascii=False) in prompts[0]
    assert payload["report_paragraphs"]["P0002"] == "First fact [E0001]"


class Model:
    def __init__(
        self, values: list[dict[str, Any] | str | Exception], *, tool: bool = True
    ) -> None:
        self.values = values
        self.requests: list[dict[str, Any]] = []
        self.tool = tool

    def invoke(self, **kwargs: Any) -> ChatCompletion:
        self.requests.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict) and "issues" in value:
            # 图测试使用内部持久化格式，模型桩则发出新版的两组结果协议。
            revisions, gaps = [], []
            for item in value["issues"]:
                if item["kind"] == "missing_evidence":
                    gaps.append(
                        {
                            "topic_quote": json.loads(kwargs["messages"][1]["content"])[
                                "topic"
                            ],
                            **{
                                key: item[key]
                                for key in ("reason", "question", "query")
                            },
                        }
                    )
                else:
                    revisions.append({"paragraph_id": "", "evidence_ids": [], **item})
            value = {"revisions": revisions, "research_gaps": gaps}
        text = value if isinstance(value, str) else json.dumps(value)
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if self.tool:
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "submit_report_review", "arguments": text},
                    }
                ],
            }
        return ChatCompletion.model_validate(
            {
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if self.tool else "stop",
                        "message": message,
                    }
                ],
            }
        )


@pytest.mark.parametrize("tool", [True, False])
def test_review_service_validates_native_and_text_output_without_mutating_state(
    tool: bool,
) -> None:
    state = SummaryState(research_topic="test research topic")
    seed(state)
    before = asdict(state)
    model = Model([gap()], tool=tool)
    outcome = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", )).review(
        state,
        ReportDraft(revision=0, text=BODY, citation_ids=["E0001"]),
        allow_repair=True,
    )
    assert (
        outcome.review.issues[0].kind == "missing_evidence" and asdict(state) == before
    )
    assert model.requests[0]["tool_choice"] == "auto"
    schema = model.requests[0]["tools"][0]["function"]["parameters"]
    # 当前提供商不会可靠解析嵌套 $ref；实际发出的参数结构必须展开。
    assert "$defs" not in schema and "$ref" not in json.dumps(schema)
    for group in ("revisions", "research_gaps"):
        item_schema = schema["properties"][group]["items"]
        assert set(item_schema["required"]) == set(item_schema["properties"])


@pytest.mark.parametrize(
    "bad",
    [
        "invalid",
        {"issues": [{"kind": "unsupported", "reason": "bad", "paragraph_id": "P9999"}]},
        {"issues": [{"kind": "omission", "reason": "bad", "evidence_ids": ["E9999"]}]},
    ],
)
def test_protocol_repair_budget_is_explicit(bad: Any) -> None:
    state = SummaryState()
    seed(state)
    model = Model([bad, PASS])
    service = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", ))
    result = service.review(
        state,
        ReportDraft(revision=0, text=BODY, citation_ids=["E0001"]),
        allow_repair=True,
    )
    assert (
        result.review is not None
        and result.error is None
        and result.repairs_used == 1
        and len(model.requests) == 2
    )
    model.values = [bad, PASS]
    result = service.review(
        state,
        ReportDraft(revision=1, text=BODY, citation_ids=["E0001"]),
        allow_repair=False,
    )
    assert result.review is None and result.repairs_used == 0 and len(model.values) == 1


def test_review_transient_error_is_unavailable_but_auth_and_program_errors_raise() -> (
    None
):
    state = SummaryState()
    seed(state)
    request = httpx.Request("POST", "https://offline.invalid")
    errors = [
        RateLimitError(
            "limit", response=httpx.Response(429, request=request), body=None
        ),
        AuthenticationError(
            "key", response=httpx.Response(401, request=request), body=None
        ),
        ValueError("program bug"),
    ]
    for error in errors:
        model = Model([error])
        service = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", ))
        if isinstance(error, RateLimitError):
            assert (
                service.review(
                    state, ReportDraft(revision=0, text=BODY), allow_repair=True
                ).review
                is None
            )
        else:
            with pytest.raises(type(error)):
                service.review(
                    state, ReportDraft(revision=0, text=BODY), allow_repair=True
                )
