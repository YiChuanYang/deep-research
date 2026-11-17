"""报告循环的确定性状态转换：预算与任务分配不交给模型。"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from deep_researcher.config import Configuration
from deep_researcher.llm.structured import ModelOutputError
from deep_researcher.models import Evidence, SummaryState, TodoItem
from deep_researcher.report_iteration import (
    ReportDraft,
    ReportIteration,
    SupplementTask,
)
from deep_researcher.services.report_format import comparable_report
from deep_researcher.services.research_constraints import validate_research_question

logger = logging.getLogger(__name__)


def iteration(values: dict[str, Any]) -> ReportIteration:
    raw = values.get("report_iteration")
    if raw is None:
        raise ValueError("报告迭代检查点缺失")
    return ReportIteration.model_validate(raw)


def update(cycle: ReportIteration) -> dict[str, Any]:
    return {"report_iteration": cycle.model_dump(mode="json")}


def selected_ids(state: SummaryState, values: dict[str, Any]) -> list[int]:
    if values.get("report_iteration") is not None:
        ids = iteration(values).selected_task_ids
    else:
        limit = values.get("max_tasks")
        ids = [
            task.id
            for task in (
                state.todo_items if limit is None else state.todo_items[:limit]
            )
        ]
    if len(ids) != len(set(ids)) or not set(ids).issubset(
        task.id for task in state.todo_items
    ):
        raise ValueError("执行任务编号无效")
    return ids


def normalized(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold())


def evidence_fingerprint(item: Evidence) -> str:
    """跨任务比较材料本身：新 E 编号、查询变化、镜像 URL 都不算新内容。"""
    return hashlib.sha256(" ".join(item.text.split()).encode("utf-8")).hexdigest()


def stop(cycle: ReportIteration, reason: str) -> None:
    draft = cycle.drafts[-1]
    cycle.stop_reason = reason
    if draft.review and draft.review.blocking:
        cycle.status, cycle.next_action = "blocked", "blocked"
    else:
        cycle.status, cycle.next_action = "limited", "finalize"


def decide_after_review(cycle: ReportIteration, config: Configuration) -> None:
    draft = cycle.drafts[-1]
    if draft.review is None:
        # 曾发现实质问题但当前复核失败，不能凭协议失败绕过已有反馈。
        known_blocking = any(
            item.review and item.review.blocking for item in cycle.drafts
        )
        cycle.status = "blocked" if known_blocking else "unreviewed"
        cycle.next_action = "blocked" if known_blocking else "finalize"
        cycle.stop_reason = draft.review_error or "报告评审未完成"
    elif not draft.review.issues:
        cycle.status, cycle.next_action = "accepted", "finalize"
        cycle.stop_reason = "模型评审未提出需要继续处理的问题"
    elif cycle.revision >= config.report_max_revisions:
        stop(cycle, "已达到报告改进轮次上限")
    else:
        cycle.status, cycle.next_action = "improving", "prepare"


def prepare_round(
    state: SummaryState, cycle: ReportIteration, config: Configuration
) -> None:
    draft = cycle.drafts[-1]
    if (
        not draft.review
        or not draft.review.issues
        or cycle.revision >= config.report_max_revisions
    ):
        raise ValueError("没有可执行的报告改进计划或预算已用尽")
    prior_queries = {
        normalized(task.effective_query)
        for task in state.todo_items
        if task.status in {"completed", "skipped"}
    }
    prior_queries.update(normalized(item.query) for item in cycle.supplements)
    prior_questions = {normalized(item.question) for item in cycle.supplements}
    # 没有取得新材料之后停止继续补搜，仍允许使用已获得材料修订报告。
    no_progress = bool(
        cycle.supplements
        and cycle.supplements[-1].search_completed
        and not cycle.supplements[-1].novel_evidence_ids
    )
    invalid_questions: list[str] = []
    parallel = config.enable_parallel_tasks and config.enable_web_rag
    if parallel and cycle.supplements:
        previous = [s for s in cycle.supplements if s.revision == cycle.supplements[-1].revision]
        no_progress = all(s.search_completed for s in previous) and not any(s.novel_evidence_ids for s in previous)
    planned = False
    for issue in draft.review.issues:
        if issue.kind != "missing_evidence":
            continue
        try:
            # 历史检查点可能保存旧协议通过的自评问题；恢复时也要挡在搜索前。
            validate_research_question(issue.question, issue.query)
        except ModelOutputError as exc:
            invalid_questions.append(str(exc))
            logger.warning(
                "未安排补搜 · %s", exc, extra={"event": "report_gap_rejected"}
            )
            continue
        if (
            len(cycle.supplements) >= config.report_max_supplement_tasks
            or no_progress
            or normalized(issue.query) in prior_queries
            or normalized(issue.question) in prior_questions
        ):
            continue
        # 同一轮多个缺口只增加一次报告修订轮次，追加任务仍受总预算限制。
        if not planned:
            cycle.revision += 1
        planned = True
        task_id = max((task.id for task in state.todo_items), default=0) + 1
        state.todo_items.append(
            TodoItem(
                id=task_id,
                title="补充研究：" + issue.question,
                # 评审 reason 是待核对的判断，只保存在 SupplementTask；不能充当事实前提。
                intent="根据外部证据回答："
                + issue.question
                + "。材料不足时明确说明，不能评价当前报告。",
                query=issue.query,
            )
        )
        cycle.supplements.append(
            SupplementTask(
                task_id=task_id,
                revision=cycle.revision,
                question=issue.question,
                query=issue.query,
                reason=issue.reason,
            )
        )
        cycle.next_action = "tasks"
        logger.info(
            "安排补充研究 · 任务 %d · %s",
            task_id,
            issue.question,
            extra={
                "event": "report_supplement_planned",
                "task_id": task_id,
                "revision": cycle.revision,
            },
        )
        prior_queries.add(normalized(issue.query))
        prior_questions.add(normalized(issue.question))
        if not parallel:
            return
    if planned:
        return
    if any(issue.kind != "missing_evidence" for issue in draft.review.issues):
        cycle.revision += 1
        cycle.next_action = "report"
        logger.info(
            "使用已有材料修订报告 · 第 %d 版",
            cycle.revision + 1,
            extra={"event": "report_revision_planned", "revision": cycle.revision},
        )
    else:
        # 保存实际命中的停止原因，不能让用户猜究竟是哪一条规则。
        if invalid_questions:
            reason = "剩余补充问题不能由外部资料回答，未发起搜索"
        elif len(cycle.supplements) >= config.report_max_supplement_tasks:
            reason = "已达到全研究的补充任务上限"
        elif no_progress:
            reason = "上一次补搜没有取得内容不同的新证据"
        else:
            reason = "剩余缺口的问题或查询已尝试，停止重复补搜"
        stop(cycle, reason)
        logger.info("停止补搜 · %s", reason)


def commit_draft(cycle: ReportIteration, text: str, citation_ids: list[str]) -> None:
    if len(cycle.drafts) != cycle.revision:
        raise ValueError("报告草稿数量与当前轮次不一致")
    previous = cycle.drafts[-1] if cycle.drafts else None
    draft = ReportDraft(revision=cycle.revision, text=text, citation_ids=citation_ids)
    cycle.drafts.append(draft)
    if (
        previous
        and comparable_report(text) == comparable_report(previous.text)
        and citation_ids == previous.citation_ids
    ):
        draft.review, draft.review_error = previous.review, previous.review_error
        # 空行变动可能改变 P 编号，明确这份反馈属于哪一版原文，不能冒充新评审。
        draft.review_reused_from = (
            previous.review_reused_from
            if previous.review_reused_from is not None
            else previous.revision
        )
        stop(cycle, "报告修订没有改变正文，停止重复迭代")
        logger.info(
            "修订没有实质变化 · 停止重复评审",
            extra={"event": "report_revision_unchanged"},
        )
    else:
        cycle.status, cycle.next_action = "reviewing", "review"
    if previous:
        logger.info(
            "报告修订 · 新采用证据=%s · 移除引用=%s",
            "、".join(sorted(set(citation_ids) - set(previous.citation_ids))) or "无",
            "、".join(sorted(set(previous.citation_ids) - set(citation_ids))) or "无",
            extra={
                "event": "report_revision_materials",
                "revision": cycle.revision,
                "added_citation_ids": sorted(
                    set(citation_ids) - set(previous.citation_ids)
                ),
            },
        )
