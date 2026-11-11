"""研究报告的自动结构检查和人工评分校验。"""

from __future__ import annotations

import hashlib
from typing import Any

from deep_researcher.models import SummaryState
from deep_researcher.services.evidence_service import (
    CITATION,
    ID_PATTERN,
    CitationError,
    EvidenceService,
)

# 当前系统在没有有效摘要时生成的固定正文。
NO_INFORMATION = "# 研究结果\n\n本轮未获得足以形成结论的资料。"


def text_hash(text: str) -> str:
    """计算文本 SHA-256，用来确认评分对应的是哪一版报告。"""
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def _has_text(value: Any) -> bool:
    """判断一个值是否是非空字符串。"""
    return isinstance(value, str) and bool(value.strip())

def extract_report_body(
    state: SummaryState,
) -> tuple[str | None, str]:
    """
    从完整报告中提取模型生成的正文。

    1. 模型生成的正文
    2. 程序追加的“引用证据”
    3. 程序追加的“研究范围说明”

    只有尾部完全符合当前程序生成格式时，才自动确认正文边界。
    """
    report = state.structured_report or ""

    if not report.strip():
        return None, "报告为空"

    labels = {
        "pending": "本轮未执行",
        "running": "尚未完成",
        "skipped": "无有效资料",
    }

    unfinished = [
        task
        for task in state.todo_items
        if task.status != "completed"
    ]

    scope = ""

    if unfinished:
        scope = (
            "\n\n## 研究范围说明\n\n"
            + "\n".join(
                (
                    f"- 任务 {task.id}「{task.title}」："
                    f"{labels.get(task.status, task.status)}。"
                )
                for task in unfinished
            )
        )

    # 有引用时，参考条目是 Reporter 程序自动生成的。
    if state.report_citation_ids:
        try:
            references = EvidenceService().references(
                state,
                state.report_citation_ids,
            )
        except CitationError as exc:
            return None, f"参考条目无法生成：{exc}"

        suffix = (
            "\n\n## 引用证据\n\n"
            f"{references}"
            f"{scope}"
        )

        if report.endswith(suffix):
            body = report[:-len(suffix)]
            return body, "已核对程序生成的完整尾部"

        return (
            None,
            "参考条目或范围说明与当前格式不一致，需要人工确认正文",
        )

    # 没有引用时，只接受系统已知的资料不足模板。
    if (
        not any(
            task.status == "completed" and task.summary
            for task in state.todo_items
        )
        and report == NO_INFORMATION + scope
    ):
        return (
            NO_INFORMATION,
            "已识别程序生成的资料不足说明",
        )

    return (
        None,
        "没有报告引用，且不属于已知的资料不足模板",
    )

def check_text(
    state: SummaryState,
    text: str,
    allowed_ids: list[str],
    recorded_ids: list[str],
) -> dict[str, Any]:
    """
    检查一段文本中的引用。

    检查内容：

    - 是否存在 [E0001] 格式的引用；
    - 引用是否存在于当前状态；
    - 引用是否属于允许范围；
    - 实际引用是否和状态中保存的 citation_ids 一致。
    """
    if not isinstance(text, str) or not text.strip():
        return {
            "status": "fail",
            "reason": "文本为空",
        }

    try:
        actual_ids = EvidenceService().validate(
            state,
            text,
            allowed_ids,
        )
    except CitationError as exc:
        return {
            "status": "fail",
            "reason": str(exc),
        }

    if set(actual_ids) != set(recorded_ids):
        return {
            "status": "fail",
            "reason": "正文引用与保存的 citation_ids 不一致",
            "actual_ids": actual_ids,
            "recorded_ids": recorded_ids,
        }

    return {
        "status": "pass",
        "reason": "引用格式、关联关系和允许范围符合当前协议",
        "actual_ids": actual_ids,
    }

def inspect_state(state: SummaryState) -> dict[str, Any]:
    """
    对一次研究状态执行自动检查。

    返回值是普通字典，后续可以直接写入 JSON。
    """
    service = EvidenceService()

    try:
        body, boundary_reason = extract_report_body(state)
    except Exception as exc:
        body = None
        boundary_reason = f"正文边界检查失败：{exc}"

    task_checks: list[dict[str, Any]] = []
    allowed_report_ids: list[str] = []

    for task in state.todo_items:
        if task.status != "completed":
            continue

        result = check_text(
            state=state,
            text=task.summary or "",
            allowed_ids=task.evidence_ids,
            recorded_ids=task.citation_ids,
        )

        task_checks.append(
            {
                "task_id": task.id,
                "task_title": task.title,
                **result,
            }
        )

        # Reporter 当前使用“已完成摘要中出现的引用”
        # 作为报告允许引用范围。
        for citation_id in CITATION.findall(
            task.summary or ""
        ):
            if ID_PATTERN.fullmatch(citation_id):
                allowed_report_ids.append(citation_id)

    association_errors: list[str] = []

    for citation_id in state.report_citation_ids:
        try:
            service.resolve(state, citation_id)
        except CitationError as exc:
            association_errors.append(str(exc))

    if body is None:
        report_check: dict[str, Any] = {
            "status": "unverified",
            "reason": boundary_reason,
        }
    elif (
        body == NO_INFORMATION
        and not state.report_citation_ids
    ):
        report_check = {
            "status": "not_applicable",
            "reason": "资料不足说明不要求引用，但是否合理需要人工判断",
        }
    else:
        report_check = check_text(
            state=state,
            text=body,
            allowed_ids=list(dict.fromkeys(allowed_report_ids)),
            recorded_ids=state.report_citation_ids,
        )

    return {
        "rule_version": 1,
        "report_sha256": text_hash(
            state.structured_report or ""
        ),
        "body": body,
        "body_boundary": boundary_reason,
        "task_checks": task_checks,
        "report_check": report_check,
        "association_errors": association_errors,
        "counts": {
            "planned_tasks": len(state.todo_items),
            "completed_tasks": sum(
                task.status == "completed"
                for task in state.todo_items
            ),
            "skipped_tasks": sum(
                task.status == "skipped"
                for task in state.todo_items
            ),
            "sources": len(state.sources),
            "evidence": len(state.evidence),
            "report_citations": len(
                set(state.report_citation_ids)
            ),
        },
    }


def score_review(
    review: dict[str, Any],
    state: SummaryState,
    checks: dict[str, Any],
) -> dict[str, Any] | None:
    """
    校验人工评分表并计算描述性指标。

    status=pending 时返回 None，表示人工评分还没有完成。
    status=done 时才会验证完整字段。
    """
    status = review.get("status")

    if status == "pending":
        return None

    if status != "done":
        raise ValueError(
            "review.status 必须是 pending 或 done"
        )

    if review.get("run_id") != state.run_id:
        raise ValueError("评分表和研究编号不匹配")

    expected_hash = text_hash(
        state.structured_report or ""
    )

    if review.get("report_sha256") != expected_hash:
        raise ValueError(
            "报告已经变化，不能直接复用旧评分"
        )

    reviewer = review.get("reviewer")

    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("请填写 reviewer")

    points = review.get("key_points")

    if not isinstance(points, list) or not points:
        raise ValueError(
            "请填写至少一个预期覆盖要点"
        )

    total_score = 0

    for point in points:
        if not isinstance(point, dict):
            raise ValueError("key_points 中的每一项必须是对象")

        score = point.get("score")

        if type(score) is not int or score not in {0, 1, 2}:
            raise ValueError(
                "每个要点评分必须为整数 0、1、2"
            )

        if not _has_text(point.get("point")):
            raise ValueError("每个要点都需要填写 point")

        if not _has_text(point.get("reason")):
            raise ValueError("每个要点都需要填写 reason")

        total_score += score

    limitations_score = review.get("limitations_score")

    if (
        type(limitations_score) is not int
        or limitations_score not in {0, 1, 2}
    ):
        raise ValueError(
            "limitations_score 必须为整数 0、1、2"
        )

    if not _has_text(
        review.get("limitations_reason")
    ):
        raise ValueError(
            "请填写资料不足和限制处理的评分理由"
        )

    evidence_sufficiency = review.get(
        "evidence_sufficiency"
    )

    if evidence_sufficiency not in {
        "enough",
        "limited",
        "insufficient",
        "uncertain",
    }:
        raise ValueError(
            "evidence_sufficiency 值无效"
        )

    claims = review.get("claims", [])

    if not isinstance(claims, list):
        raise ValueError("claims 必须是列表")

    inventory_complete = review.get(
        "claim_inventory_complete"
    )

    if type(inventory_complete) is not bool:
        raise ValueError(
            "claim_inventory_complete 必须是布尔值"
        )

    if not claims and not _has_text(
        review.get("no_claims_reason")
    ):
        raise ValueError(
            "没有评审结论时，请填写 no_claims_reason"
        )

    report_body = checks.get("body")
    full_report = state.structured_report or ""

    cited_claims = 0
    supported_claims = 0
    partial_claims = 0
    uncertain_claims = 0

    valid_verdicts = {
        "supported",
        "partial",
        "unsupported",
        "uncertain",
    }

    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError("claims 中的每一项必须是对象")

        claim_text = claim.get("text")
        citation_ids = claim.get("citation_ids")
        verdict = claim.get("verdict")
        reason = claim.get("reason")

        if not _has_text(claim_text):
            raise ValueError("结论 text 不能为空")

        # 报告正文边界已识别时，结论必须原样摘录正文。
        # 边界无法自动识别时，退回整份报告做人工确认。
        searchable_text = (
            report_body
            if isinstance(report_body, str)
            else full_report
        )

        if claim_text not in searchable_text:
            raise ValueError(
                "结论 text 必须原样摘录报告"
            )

        if (
            not isinstance(citation_ids, list)
            or not all(
                isinstance(item, str)
                for item in citation_ids
            )
        ):
            raise ValueError(
                "citation_ids 必须是字符串列表"
            )

        if not set(citation_ids).issubset(
            state.report_citation_ids
        ):
            raise ValueError(
                "结论引用超出报告已保存的引用范围"
            )

        # 人工填写的引用编号必须实际出现在该结论文本中。
        claim_text_ids = set(
            CITATION.findall(claim_text)
        )

        if claim_text_ids != set(citation_ids):
            raise ValueError(
                "结论中的 citation_ids 必须和结论文本中的引用一致"
            )

        if verdict not in valid_verdicts:
            raise ValueError("结论 verdict 无效")

        if not _has_text(reason):
            raise ValueError(
                "每条结论都需要填写 reason"
            )

        if not citation_ids and verdict != "unsupported":
            raise ValueError(
                "没有引用的结论必须判为 unsupported"
            )

        if citation_ids:
            cited_claims += 1

        if verdict == "supported":
            supported_claims += 1
        elif verdict == "partial":
            partial_claims += 1
        elif verdict == "uncertain":
            uncertain_claims += 1

    audited_claims = len(claims)

    return {
        "run_id": state.run_id,
        "reviewer": reviewer,
        "coverage_points": len(points),
        "coverage_score": total_score,
        "coverage_max": 2 * len(points),
        "coverage_ratio": total_score / (2 * len(points)),
        "audited_claims": audited_claims,
        "cited_claims": cited_claims,
        "supported_claims": supported_claims,
        "partial_claims": partial_claims,
        "uncertain_claims": uncertain_claims,
        "citation_coverage": (
            cited_claims / audited_claims
            if audited_claims
            else None
        ),
        "citation_support": (
            supported_claims / cited_claims
            if cited_claims
            else None
        ),
        "claim_inventory_complete": inventory_complete,
        "evidence_sufficiency": evidence_sufficiency,
        "limitations_score": limitations_score,
    }