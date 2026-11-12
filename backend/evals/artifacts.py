"""保存和复核评测材料；模型输入与评分标签分开。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from deep_researcher.graph.graph_researcher import RunSnapshot
from deep_researcher.graph.graph_state import decode_state
from deep_researcher.models import SummaryState
from evals.checks import inspect_state, score_review
from evals.dataset import ResearchCase


def write_json(path: Path, value: Any) -> None:
    """同目录临时写入后原子替换；调用方负责创建目录。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def make_review_template(
    state: SummaryState, checks: dict[str, Any],
    case: ResearchCase | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": state.run_id,
        "report_sha256": checks["report_sha256"],
        "status": "pending",
        "reviewer": "",
        "key_points": [
            {"point": point, "score": None, "reason": ""}
            for point in (case.key_points if case is not None else [""])
        ],
        "evidence_sufficiency": "uncertain",
        "limitations_score": None,
        "limitations_reason": "",
        "claim_inventory_complete": False,
        "no_claims_reason": "",
        "claims": [],
        "notes": "",
        **({"case_id": case.case_id} if case is not None else {}),
    }


def export_attempt(
    snapshot: RunSnapshot, case: ResearchCase, directory: Path,
) -> dict[str, Any]:
    if not snapshot.report_ready or snapshot.state is None:
        raise ValueError("研究没有可交付的报告")
    state = snapshot.state
    if state.research_topic != case.topic:
        raise ValueError("研究主题与固定题目不一致")
    checks = inspect_state(state)
    write_json(directory / "case.json", case.model_dump(mode="json"))
    write_json(directory / "snapshot.json", {
        "run": asdict(snapshot.info),
        "state": asdict(state),
        "next_nodes": list(snapshot.next_nodes),
        "active_task_id": snapshot.active_task_id,
        "graph_finished": snapshot.graph_finished,
    })
    (directory / "report.md").write_text(state.structured_report or "", encoding="utf-8")
    if isinstance(checks["body"], str):
        (directory / "body.md").write_text(checks["body"], encoding="utf-8")
    write_json(directory / "evidence.json", {
        "sources": {key: asdict(value) for key, value in state.sources.items()},
        "evidence": {key: asdict(value) for key, value in state.evidence.items()},
    })
    write_json(directory / "checks.json", checks)
    write_json(directory / "review.json", make_review_template(state, checks, case))
    return checks


def read_assessment(
    directory: Path, case: ResearchCase | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """重算自动检查，评分必须对应原报告及冻结的题目要点。"""
    snapshot = read_json(directory / "snapshot.json")
    state = decode_state(snapshot["state"])
    if expected_run_id is not None and state.run_id != expected_run_id:
        raise ValueError("快照与本次运行编号不一致")
    if case is not None:
        saved_case = ResearchCase.model_validate(read_json(directory / "case.json"))
        if saved_case != case or state.research_topic != case.topic:
            raise ValueError("评测题目或验收要点与冻结题集不一致")
    report_path = directory / "report.md"
    if report_path.read_text(encoding="utf-8") != state.structured_report:
        raise ValueError("report.md 已变化，与快照正文不一致")
    checks = inspect_state(state)
    if read_json(directory / "evidence.json") != {
        "sources": {key: asdict(value) for key, value in state.sources.items()},
        "evidence": {key: asdict(value) for key, value in state.evidence.items()},
    }:
        raise ValueError("evidence.json 与冻结快照不一致")
    if isinstance(checks["body"], str) and (
        (directory / "body.md").read_text(encoding="utf-8") != checks["body"]
    ):
        raise ValueError("body.md 与冻结报告正文不一致")
    result = {
        "report_check": checks["report_check"],
        "task_checks": checks["task_checks"],
        "overall_check": checks["overall_check"],
        "counts": checks["counts"],
    }
    try:
        review = read_json(directory / "review.json")
        if not isinstance(review, dict):
            raise ValueError("评分表必须为 JSON 对象")
        if case is not None and review.get("case_id") != case.case_id:
            raise ValueError("人工评分表的 case_id 不一致")
        score = score_review(
            review, state, checks,
            expected_points=case.key_points if case is not None else None,
        )
        result.update(
            review_status="pending" if score is None else "reviewed",
            score=score,
        )
    except (ValueError, OSError, TypeError, KeyError) as exc:
        result.update(review_status="invalid_review", review_error=str(exc), score=None)
    return result
