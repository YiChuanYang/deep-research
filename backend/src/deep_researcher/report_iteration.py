"""报告评审协议和一份集中保存的迭代记录。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportIssue(BaseModel):
    """缺材料才补搜；其余问题使用已有证据修订报告。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    kind: Literal["missing_evidence", "unsupported", "contradiction", "omission"]
    reason: str = Field(min_length=1, max_length=400)
    paragraph_id: str = ""
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    question: str = Field(default="", max_length=200)
    query: str = Field(default="", max_length=200)
    # 原需求片段仅用于核对缺口范围，旧持久化记录允许缺省。
    topic_quote: str = ""

    @model_validator(mode="after")
    def check_action(self) -> ReportIssue:
        """after 在字段完成类型校验后，检查问题与补搜参数的关系。"""
        if self.kind == "missing_evidence":
            if not self.question or not self.query:
                raise ValueError("missing_evidence 必须提供 question 和 query")
            if any(char in self.query for char in "\r\n"):
                raise ValueError("补充查询必须是单行")
        elif self.query or self.question:
            raise ValueError("非缺证据问题不能发起补搜")
        if self.kind in {"unsupported", "contradiction"} and not self.paragraph_id:
            raise ValueError("实质问题必须定位到报告段落")
        if self.kind in {"contradiction", "omission"} and not self.evidence_ids:
            raise ValueError("矛盾或遗漏已有内容必须指出对应证据")
        return self

    @property
    def blocking(self) -> bool:
        return self.kind in {"unsupported", "contradiction"}


class ReportReview(BaseModel):
    """空 issues 表示本次评审没有提出问题，不代表事实已被外部证实。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    issues: list[ReportIssue] = Field(max_length=3)

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)


class ReviewOutcome(BaseModel):
    """服务返回值；格式修复次数与业务改进次数分别统计。"""

    review: ReportReview | None = None
    error: str | None = None
    repairs_used: int = Field(default=0, ge=0, le=1)
    input_chars: int = 0


class ReportDraft(BaseModel):
    """评审直接绑定这一版正文，避免旧反馈被当成新版通过证明。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    text: str
    citation_ids: list[str] = Field(default_factory=list)
    review: ReportReview | None = None
    review_error: str | None = None
    review_input_chars: int = 0
    # 仅排版变化而复用反馈时，段落编号仍对应这个来源版本；旧记录默认 None。
    review_reused_from: int | None = None


class SupplementTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: int
    revision: int
    question: str
    query: str
    reason: str
    novel_evidence_ids: list[str] = Field(default_factory=list)
    search_completed: bool = False


class ReportIteration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    initial_task_ids: list[int] = Field(default_factory=list)
    supplements: list[SupplementTask] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    drafts: list[ReportDraft] = Field(default_factory=list)
    protocol_repairs_used: int = Field(default=0, ge=0, le=1)
    status: Literal[
        "researching",
        "reviewing",
        "improving",
        "accepted",
        "limited",
        "unreviewed",
        "blocked",
    ] = "researching"
    next_action: Literal[
        "review", "prepare", "tasks", "report", "finalize", "blocked"
    ] = "report"
    stop_reason: str = ""

    @property
    def selected_task_ids(self) -> list[int]:
        return [*self.initial_task_ids, *(item.task_id for item in self.supplements)]
