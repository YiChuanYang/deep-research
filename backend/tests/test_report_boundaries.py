"""以真实失败材料验证报告反馈、外部研究问题和事实证据的边界。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_state import decode_state
from deep_researcher.graph.report_cycle import commit_draft, prepare_round
from deep_researcher.llm.llm import MyLLM
from deep_researcher.llm.structured import ModelOutputError, parse_json
from deep_researcher.models import Evidence, Source, SummaryState, TodoItem
from deep_researcher.report_iteration import ReportDraft, ReportIteration, ReportReview
from deep_researcher.services.report_format import comparable_report
from deep_researcher.services.report_reflection import ReportReflectionService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.research_constraints import validate_research_question
from deep_researcher.services.review_protocol import ReviewSubmission, review_schema
from deep_researcher.services.summarizer import SummarizationService
from evals.report_cycle_probe import read_input, run_pair
from tests.test_report_iteration import BODY, OMISSION, Model, TextAgent, gap, workflow
from tests.test_streaming import seed

FIXTURE = Path(__file__).parent / "fixtures" / "report_cycle_hyperspectral.json"


@pytest.fixture
def recorded() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_real_whitespace_only_revision_reuses_original_review(
    recorded: dict[str, Any],
) -> None:
    first, second = recorded["drafts"]
    assert first != second and comparable_report(first) == comparable_report(second)
    state = decode_state(recorded["initial_state"])
    review = ReportReview.model_validate(parse_json(recorded["review_responses"][0]))
    ids = state.todo_items[0].citation_ids
    cycle = ReportIteration(
        revision=1,
        drafts=[ReportDraft(revision=0, text=first, citation_ids=ids, review=review)],
    )
    commit_draft(cycle, second, ids)
    assert cycle.next_action == "finalize" and cycle.status == "limited"
    assert cycle.drafts[-1].review_reused_from == 0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a b", "ab"),
        ("A\nB", "AB"),
        ("A  B", "A B"),
        ("成本为 10", "成本为 100"),
        ("事实 [E0001]", "事实 [E0002]"),
        ('```python\nx="""a\n\nb"""\n```', '```python\nx="""a\nb"""\n```'),
    ],
)
def test_progress_comparison_preserves_meaningful_changes(
    left: str, right: str
) -> None:
    assert comparable_report(left) != comparable_report(right)


def test_graph_does_not_review_blank_line_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    researcher, calls = workflow(
        tmp_path, monkeypatch, [OMISSION], [BODY, BODY.replace("\n\n", "\n\n\n")]
    )
    result = researcher.run("test", max_tasks=1)
    assert calls["review"] == 1 and calls["report"] == 2
    assert (
        researcher.read_run(result.run_id)
        .report_iteration.drafts[-1]
        .review_reused_from
        == 0
    )


def test_real_bad_question_cannot_create_supplement_even_from_old_checkpoint(
    recorded: dict[str, Any],
) -> None:
    state = decode_state(recorded["initial_state"])
    review = ReportReview.model_validate(parse_json(recorded["review_responses"][0]))
    cycle = ReportIteration(
        drafts=[ReportDraft(revision=0, text=recorded["drafts"][0], review=review)]
    )
    before = asdict(state)
    prepare_round(state, cycle, Configuration(enable_web_rag=False, review_output_mode="tool", ))
    assert (
        not cycle.supplements
        and asdict(state) == before
        and cycle.next_action == "finalize"
    )


@pytest.mark.parametrize(
    "text",
    [
        "报告是否提及 FCN 的应用？",
        "正文有没有包含成本比较？",
        "当前报告缺少什么？",
        "高光谱 FCN 应用 报告提及",
        "Does the report mention FCN?",
    ],
)
def test_report_questions_are_rejected(text: str) -> None:
    with pytest.raises(ModelOutputError):
        validate_research_question(text, "FCN hyperspectral application")


def test_external_questions_and_valid_edits_have_separate_protocols() -> None:
    schema = review_schema()
    assert "$ref" not in json.dumps(schema)
    assert "query" not in schema["properties"]["revisions"]["items"]["properties"]
    assert "kind" not in schema["properties"]["research_gaps"]["items"]["properties"]
    assert schema["properties"]["revisions"]["maxItems"] == 2
    assert schema["properties"]["research_gaps"]["maxItems"] == 1
    result = ReviewSubmission.model_validate(
        {
            "revisions": [],
            "research_gaps": [
                {
                    "reason": "原问题要求方法对比，缺少适用条件",
                    "topic_quote": "FCN 高光谱分类",
                    "question": "FCN 高光谱分类有哪些适用条件？",
                    "query": "FCN hyperspectral classification limitations",
                }
            ],
        }
    ).to_review("FCN 高光谱分类有哪些适用条件？")
    assert result.issues[0].kind == "missing_evidence"
    with pytest.raises(ValidationError):
        ReviewSubmission.model_validate(
            {
                "revisions": [
                    {
                        "kind": "omission",
                        "reason": "missing",
                        "paragraph_id": "P0001",
                        "evidence_ids": ["E0001"],
                        "query": "search",
                    }
                ],
                "research_gaps": [],
            }
        )


def test_actual_conflicting_outputs_never_become_passes(
    recorded: dict[str, Any],
) -> None:
    state = decode_state(recorded["initial_state"])
    model = Model(recorded["review_responses"][1:], tool=False)
    result = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", )).review(
        state,
        ReportDraft(revision=1, text=recorded["drafts"][1]),
        allow_repair=True,
    )
    assert (
        result.review is None and result.repairs_used == 1 and len(model.requests) == 2
    )


def test_meta_question_uses_protocol_repair_and_does_not_silently_disappear(
    recorded: dict[str, Any],
) -> None:
    old = parse_json(recorded["review_responses"][0])["issues"][0]
    response = {
        "revisions": [],
        "research_gaps": [
            {
                "topic_quote": recorded["initial_state"]["research_topic"],
                **{key: old[key] for key in ("reason", "question", "query")},
            }
        ],
    }
    state = decode_state(recorded["initial_state"])
    model = Model([response, response])
    outcome = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", )).review(
        state,
        ReportDraft(revision=0, text=recorded["drafts"][0]),
        allow_repair=True,
    )
    assert (
        outcome.review is None
        and outcome.repairs_used == 1
        and "外部资料" in outcome.error
    )


def test_feedback_reason_is_not_copied_into_research_task() -> None:
    state = SummaryState()
    seed(state)
    feedback = gap()
    feedback["issues"][0]["reason"] = "报告完全没介绍该方法"
    cycle = ReportIteration(
        drafts=[
            ReportDraft(
                revision=0, text=BODY, review=ReportReview.model_validate(feedback)
            )
        ]
    )
    prepare_round(state, cycle, Configuration(enable_web_rag=False, review_output_mode="tool", ))
    assert cycle.supplements[0].reason not in state.todo_items[-1].intent
    assert cycle.supplements[0].question in state.todo_items[-1].intent


def test_supplement_summary_rejects_real_feedback_echo(
    recorded: dict[str, Any],
) -> None:
    state = decode_state(recorded["initial_state"])
    state.sources.update(
        {key: Source(**value) for key, value in recorded["supplement_sources"].items()}
    )
    state.evidence.update(
        {
            key: Evidence(**value)
            for key, value in recorded["supplement_evidence"].items()
        }
    )
    state.todo_items.append(
        TodoItem(
            id=4,
            title="报告未提及 FCN",
            intent="报告未提及 FCN",
            query="FCN hyperspectral classification",
            evidence_ids=list(recorded["supplement_evidence"]),
        )
    )
    prompts: list[str] = []

    class Agent(TextAgent):
        def run(self, **kwargs: Any) -> str:
            prompts.append(kwargs["input_text"])
            return super().run(**kwargs)

    service = SummarizationService(
        lambda: cast(SimpleAgent, Agent(recorded["bad_supplement"]["summary"])),
        Configuration(enable_web_rag=False, review_output_mode="tool", ),
    )
    result = service.summarize_task(
        state, 4, research_question="FCN 高光谱分类有哪些代表方法？"
    )
    assert result.text == "暂无可用信息" and not result.citation_ids
    assert "报告未提及 FCN" not in prompts[0]
    assert state.todo_items[-1].summary is None


def test_supplement_summary_keeps_supported_domain_result() -> None:
    state = SummaryState()
    seed(state)
    result = SummarizationService(
        lambda: cast(SimpleAgent, TextAgent(BODY)), Configuration(enable_web_rag=False, review_output_mode="tool", )
    ).summarize_task(
        state,
        1,
        research_question="What is the first fact?",
    )
    assert result.citation_ids == ("E0001",)


def test_bad_query_rewrite_falls_back_before_external_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SummaryState()
    seed(state)
    cycle = ReportIteration(
        drafts=[
            ReportDraft(
                revision=0, text=BODY, review=ReportReview.model_validate(gap())
            )
        ]
    )
    prepare_round(state, cycle, Configuration(enable_web_rag=False, review_output_mode="tool", ))
    task = state.todo_items[-1]
    monkeypatch.setattr(
        "deep_researcher.graph.graph_nodes.QueryRewriteService.rewrite_task",
        lambda *args: "报告是否提及比较结果",
    )
    nodes = ResearchNodes(Configuration(enable_web_rag=False, review_output_mode="tool", ), "offline")
    nodes.rewrite_query(
        state, {"active_task_id": task.id, "report_iteration": cycle.model_dump()}
    )
    assert task.search_query == cycle.supplements[0].query


def test_revised_report_receives_current_supplement_separately() -> None:
    state = SummaryState()
    seed(state)
    prompts: list[str] = []

    class Agent(TextAgent):
        def run(self, **kwargs: Any) -> str:
            prompts.append(kwargs["input_text"])
            return super().run(**kwargs)

    ReportingService(lambda: cast(SimpleAgent, Agent(BODY))).generate_report(
        state,
        previous_report=BODY,
        feedback=json.dumps(OMISSION),
        supplement_task_ids=[1],
    )
    data = prompts[0].split("本轮补充研究结果：\n")[1].split("\n逐项核对")[0]
    assert json.loads(data)[0]["evidence_ids"] == ["E0001"]


def test_resuming_at_search_checks_old_bad_task_before_creating_tool(
    recorded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_researcher.report_iteration import SupplementTask

    state = decode_state(recorded["initial_state"])
    task = TodoItem(
        id=4, title="报告是否提及 FCN", intent="audit", query="FCN", status="running"
    )
    state.todo_items.append(task)
    cycle = ReportIteration(
        initial_task_ids=[1, 2, 3],
        supplements=[
            SupplementTask(
                task_id=4,
                revision=1,
                question="报告是否提及 FCN",
                query="FCN",
                reason="missing",
            )
        ],
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("old report question must not create a search service")

    monkeypatch.setattr("deep_researcher.graph.graph_nodes.SearchService", forbidden)
    updates = ResearchNodes(Configuration(enable_web_rag=False, review_output_mode="tool", ), "offline").search(
        state,
        {
            "active_task_id": 4,
            "report_iteration": cycle.model_dump(),
        },
    )
    assert (
        task.status == "skipped"
        and updates["report_iteration"]["supplements"][0]["search_completed"]
    )


def test_actual_feedback_echo_marks_supplement_skipped_in_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    researcher, calls = workflow(tmp_path, monkeypatch, [gap()], [BODY, BODY])
    original = researcher._nodes_factory.summary_agent

    def summary_agent(nodes: ResearchNodes) -> SimpleAgent:
        if calls["search"] == 2:
            return cast(SimpleAgent, TextAgent("报告未提及应用实例 [E0002]。"))
        return original(nodes)

    monkeypatch.setattr(researcher._nodes_factory, "summary_agent", summary_agent)
    output = researcher.run("test", max_tasks=1)
    task = output.todo_items[-1]
    assert task.status == "skipped" and not task.citation_ids and task.note_id is None
    assert output.report_citation_ids == ["E0001"]
    assert calls["review"] == 1


def test_frozen_pair_uses_same_input_without_researching_initial_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_researcher.report_iteration import ReviewOutcome

    state, body = read_input(FIXTURE)
    before = FIXTURE.read_bytes()
    inputs: list[str] = []

    def review(*args: Any, **kwargs: Any) -> ReviewOutcome:
        inputs.append(args[2].text)
        return ReviewOutcome(review=ReportReview(issues=[]))

    monkeypatch.setattr(ReportReflectionService, "review", review)
    monkeypatch.setattr(ResearchNodes, "llm", lambda self: None)
    record = run_pair(FIXTURE, tmp_path / "pair", Configuration(enable_web_rag=False, review_output_mode="tool", enable_notes=False))
    assert inputs == [body] and record["report_ready"] and not record["body_changed"]
    assert record["telemetry"]["search_requests"] == 0 and record["supplements"] == 0
    assert record["run_id"] != state.run_id and FIXTURE.read_bytes() == before
    assert (tmp_path / "pair" / "baseline.md").read_text(encoding="utf-8") == body


def test_protocol_repair_does_not_turn_existing_material_into_search() -> None:
    state = SummaryState()
    seed(state)
    invalid = {
        "revisions": [
            {
                "kind": "omission",
                "reason": "condition missing",
                "paragraph_id": "P0002",
                "evidence_ids": ["E0001"],
                "query": "search",
            }
        ],
        "research_gaps": [],
    }
    valid = {
        "revisions": [
            {
                "kind": "omission",
                "reason": "condition missing",
                "paragraph_id": "P0002",
                "evidence_ids": ["E0001"],
            }
        ],
        "research_gaps": [],
    }
    model = Model([invalid, valid])
    outcome = ReportReflectionService(cast(MyLLM, model), Configuration(enable_web_rag=False, review_output_mode="tool", )).review(
        state,
        ReportDraft(revision=0, text=BODY),
        allow_repair=True,
    )
    assert outcome.repairs_used == 1 and outcome.error is None
    cycle = ReportIteration(
        drafts=[ReportDraft(revision=0, text=BODY, review=outcome.review)]
    )
    prepare_round(state, cycle, Configuration(enable_web_rag=False, review_output_mode="tool", ))
    assert cycle.next_action == "report" and not cycle.supplements


def test_research_gap_must_anchor_to_original_user_topic() -> None:
    payload = {
        "revisions": [],
        "research_gaps": [
            {
                "reason": "需要比较成本",
                "topic_quote": "两种检索方法的部署成本",
                "question": "相同数据规模下两种检索方法的部署成本如何比较？",
                "query": "检索方法 部署成本 对比",
            }
        ],
    }
    submission = ReviewSubmission.model_validate(payload)
    with pytest.raises(ModelOutputError, match="逐字摘录原问题"):
        submission.to_review("高光谱图像分类的深度学习方法主要分为哪些呢")
    # 该问题在用户确实问成本时是合法的，不能针对词语或特定题目写黑名单。
    review = submission.to_review("比较两种检索方法的部署成本")
    assert review.issues[0].topic_quote == "两种检索方法的部署成本"
