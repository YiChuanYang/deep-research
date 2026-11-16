"""模型返回协议与持久化评审模型分离，旧检查点仍使用 ReportReview。"""

from __future__ import annotations

from typing import Any, Literal
import re

from pydantic import BaseModel, ConfigDict, Field

from deep_researcher.llm.structured import ModelOutputError
from deep_researcher.report_iteration import ReportIssue, ReportReview
from deep_researcher.services.research_constraints import validate_research_question


class RevisionIssue(BaseModel):
    """已有材料中的问题不提供搜索参数，减少两种动作混填。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    kind: Literal["unsupported", "contradiction", "omission"]
    reason: str = Field(min_length=1, max_length=400)
    paragraph_id: str
    evidence_ids: list[str] = Field(max_length=6)


class ResearchGap(BaseModel):
    """缺口只提交外部研究问题；reason 仅留在评审记录，不进入任务目标。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=400)
    topic_quote: str = Field(
        min_length=2,
        max_length=200,
        description="逐字摘录原始研究主题中需要本次补搜回答的部分",
    )
    question: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=200)


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # 每轮只执行一次补搜；在 Schema 内表达上限，避免模型返回 3+3 项后才失败。
    revisions: list[RevisionIssue] = Field(max_length=2)
    research_gaps: list[ResearchGap] = Field(max_length=1)

    def to_review(self, topic: str) -> ReportReview:
        """转换为原持久化结构；无效项不静默删除，整次结果需要修复。"""
        issues = [
            ReportIssue.model_validate(item.model_dump()) for item in self.revisions
        ]
        for gap in self.research_gaps:
            if gap.topic_quote not in topic or not any(
                char.isalnum() for char in gap.topic_quote
            ):
                raise ModelOutputError(
                    "topic_quote 必须逐字摘录原问题；不能用示例或报告内容代替用户需求"
                )
            validate_research_question(gap.question, gap.query)
            issues.append(ReportIssue(kind="missing_evidence", **gap.model_dump()))
        return ReportReview(issues=issues)


def review_schema() -> dict[str, Any]:
    """model_json_schema 返回参数字典；展开本协议两种条目的局部引用。"""
    schema = ReviewSubmission.model_json_schema()
    definitions = schema.pop("$defs")
    for field, model in (
        ("revisions", "RevisionIssue"),
        ("research_gaps", "ResearchGap"),
    ):
        schema["properties"][field]["items"] = definitions[model]
    return schema


def requirement_map(topic: str) -> dict[str, str]:
    """只分割用户原文，不让模型重写要求；编号用于选取而非自由复制。"""
    parts = [m.group().strip() for m in re.finditer(r"[^。！？!?\n]+[。！？!?]?", topic) if m.group().strip()]
    return {f"R{i:02d}": text for i, text in enumerate(parts or [topic], 1)}


def json_review_schema(requirements: dict[str, str]) -> dict[str, Any]:
    schema = review_schema()
    gap = schema["properties"]["research_gaps"]["items"]
    del gap["properties"]["topic_quote"]
    gap["properties"]["requirement_id"] = {"type": "string", "enum": list(requirements)}
    gap["required"] = ["reason", "requirement_id", "question", "query"]
    return schema


def resolve_requirements(value: Any, requirements: dict[str, str]) -> Any:
    """只转换明确合法的编号，不猜测缺失问题，也不静默删除无效项。"""
    if not isinstance(value, dict) or not isinstance(value.get("research_gaps"), list):
        return value
    converted = []
    for item in value["research_gaps"]:
        if not isinstance(item, dict) or "requirement_id" not in item:
            converted.append(item)  # 兼容旧回放的逐字摘录，仍须通过原验证。
            continue
        if ("topic_quote" in item or not isinstance(item["requirement_id"], str)
                or item["requirement_id"] not in requirements):
            raise ModelOutputError("补充研究必须选择当前输入中存在的 requirement_id")
        mapped = {k: v for k, v in item.items() if k != "requirement_id"}
        mapped["topic_quote"] = requirements[item["requirement_id"]]
        converted.append(mapped)
    return {**value, "research_gaps": converted}
