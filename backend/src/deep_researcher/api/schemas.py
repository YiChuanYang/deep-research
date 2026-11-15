"""HTTP 请求和响应模型。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from deep_researcher.graph.graph_repository import RunStatus
from deep_researcher.models import TaskStatus


RunId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{32}$"),
]


class ResearchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    topic: str = Field(min_length=2, max_length=500)
    max_tasks: int | None = Field(
        default=None,
        gt=0,
        le=20,
        strict=True,
    )


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recover_interrupted: bool = Field(
        default=False,
        strict=True,
    )


class AcceptedResponse(BaseModel):
    run_id: str
    accepted: bool = True
    status_url: str
    result_url: str


class RunItem(BaseModel):
    run_id: str
    topic: str
    status: RunStatus
    updated_at: str
    max_tasks: int | None


class RunListResponse(BaseModel):
    items: list[RunItem]
    limit: int
    offset: int
    has_more: bool


class TaskProgress(BaseModel):
    id: int
    title: str
    status: TaskStatus
    selected: bool
    evidence_count: int
    citation_count: int


class ReportIterationProgress(BaseModel):
    revision: int
    max_revisions: int
    status: str
    stop_reason: str
    draft_count: int
    initial_total: int
    initial_completed: int
    supplemental_total: int
    supplemental_completed: int
    issue_count: int | None


class RunStatusResponse(RunItem):
    execution: Literal["idle", "queued", "executing"]

    next_nodes: list[str]
    active_task_id: int | None

    planned_tasks: int
    selected_tasks: int
    completed_tasks: int
    skipped_tasks: int

    tasks: list[TaskProgress]

    report_ready: bool
    requires_recovery_confirmation: bool
    error_message: str | None
    report_iteration: ReportIterationProgress | None = None


class TaskResultResponse(BaseModel):
    id: int
    title: str
    status: TaskStatus
    summary: str | None
    citation_ids: list[str]


class CitationResponse(BaseModel):
    evidence_id: str
    title: str
    url: str
    text: str


class ResearchResultResponse(BaseModel):
    run_id: str
    report_markdown: str
    tasks: list[TaskResultResponse]
    citations: list[CitationResponse]
