"""API 的请求与响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


RunStatus = Literal[
    "pending",
    "running",
    "failed",
    "completed",
]


class ResearchRequest(BaseModel):
    """创建研究请求。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    topic: str = Field(min_length=2, max_length=500)
    max_tasks: int | None = Field(default=None, gt=0)


class ResumeRequest(BaseModel):
    """恢复研究请求。"""

    recover_interrupted: bool = False


class AcceptedResponse(BaseModel):
    """后台任务已接受。"""

    run_id: str
    accepted: bool = True


class RunStatusResponse(BaseModel):
    """研究运行状态。"""

    run_id: str
    topic: str
    status: RunStatus
    error_message: str | None = None


class TaskResultResponse(BaseModel):
    """研究任务摘要。"""

    id: int
    title: str
    status: str
    summary: str | None
    citation_ids: list[str]


class CitationResponse(BaseModel):
    """报告引用对应的证据及来源。"""

    evidence_id: str
    title: str
    url: str
    text: str


class ResearchResultResponse(BaseModel):
    """研究结果。"""

    run_id: str
    report_markdown: str
    tasks: list[TaskResultResponse]
    citations: list[CitationResponse]