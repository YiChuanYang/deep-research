"""研究创建、查询、下载与恢复接口。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from deep_researcher.api.errors import ApiError
from deep_researcher.api.streaming import research_events

from deep_researcher.api.manager import ResearchManager
from deep_researcher.api.schemas import (
    AcceptedResponse,
    CitationResponse,
    ResearchRequest,
    ResearchResultResponse,
    ResumeRequest,
    RunId,
    RunItem,
    RunListResponse,
    RunStatusResponse,
    ReportIterationProgress,
    TaskProgress,
    TaskResultResponse,
)
from deep_researcher.graph.graph_repository import RunInfo
from deep_researcher.rag.store import WebRagStore
from pathlib import Path
from dataclasses import asdict


router = APIRouter()


def _manager(request: Request) -> ResearchManager:
    return cast(
        ResearchManager,
        request.app.state.research_manager,
    )


def _accepted(run_id: str) -> AcceptedResponse:
    return AcceptedResponse(
        run_id=run_id,
        status_url=f"/api/research/{run_id}",
        result_url=f"/api/research/{run_id}/result",
    )


def _item(
    info: RunInfo,
    manager: ResearchManager,
) -> RunItem:
    """组合持久化状态和本进程调度信息，用于展示。"""
    execution = manager.execution(info.run_id)

    display_status = (
        "pending"
        if execution == "queued"
        else "running"
        if execution == "executing"
        else info.status
    )

    return RunItem(
        run_id=info.run_id,
        topic=info.topic,
        status=display_status,
        updated_at=info.updated_at,
        max_tasks=info.max_tasks,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/research",
    response_model=AcceptedResponse,
    status_code=202,
)
def start_research(
    body: ResearchRequest,
    request: Request,
) -> AcceptedResponse:
    run_id = _manager(request).start(
        body.topic,
        body.max_tasks,
    )
    return _accepted(run_id)


@router.get(
    "/research",
    response_model=RunListResponse,
)
def list_research(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    manager = _manager(request)

    rows, has_more = manager.list_runs(
        limit=limit,
        offset=offset,
    )

    return RunListResponse(
        items=[_item(info, manager) for info in rows],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get(
    "/research/{run_id}",
    response_model=RunStatusResponse,
)
def get_research_status(
    run_id: RunId,
    request: Request,
) -> RunStatusResponse:
    manager = _manager(request)
    snapshot = manager.get_snapshot(run_id)
    info = snapshot.info

    tasks = (
        snapshot.state.todo_items
        if snapshot.state is not None
        else []
    )

    selected_ids = set(snapshot.selected_task_ids)
    selected = [task for task in tasks if task.id in selected_ids]
    cycle = snapshot.report_iteration
    completed_ids = {task.id for task in tasks if task.status in {"completed", "skipped"}}
    iteration_progress = None
    if cycle is not None:
        last = cycle.drafts[-1] if cycle.drafts else None
        iteration_progress = ReportIterationProgress(
            revision=cycle.revision, max_revisions=info.config.get("report_max_revisions", 2),
            status=cycle.status, stop_reason=cycle.stop_reason, draft_count=len(cycle.drafts),
            initial_total=len(cycle.initial_task_ids), initial_completed=len(set(cycle.initial_task_ids) & completed_ids),
            supplemental_total=len(cycle.supplements),
            supplemental_completed=sum(item.task_id in completed_ids for item in cycle.supplements),
            issue_count=len(last.review.issues) if last and last.review else None,
        )

    execution = manager.execution(run_id)
    item = _item(info, manager)

    # 图已完成但元数据未及时更新时，
    # 结果可用性以图检查点为准，不在 GET 接口中修改数据库。
    display_status = (
        "completed"
        if snapshot.report_ready
        else item.status
    )

    return RunStatusResponse(
        **{
            **item.model_dump(),
            "status": display_status,
        },
        execution=execution,
        next_nodes=list(snapshot.next_nodes),
        active_task_id=snapshot.active_task_id,
        planned_tasks=len(tasks),
        selected_tasks=len(selected),
        completed_tasks=sum(
            task.status == "completed"
            for task in selected
        ),
        skipped_tasks=sum(
            task.status == "skipped"
            for task in selected
        ),
        tasks=[
            TaskProgress(
                id=task.id,
                title=task.title,
                status=task.status,
                selected=task.id in selected_ids,
                evidence_count=len(task.evidence_ids),
                citation_count=len(task.citation_ids),
                retrieval=task.retrieval,
                notices=task.notices,
            )
            for task in tasks
        ],
        report_ready=snapshot.report_ready,
        requires_recovery_confirmation=(
            info.status == "running"
            and execution == "idle"
            and not snapshot.report_ready
        ),
        error_message=(
            "报告存在未解决的问题，草稿已保留；请查看迭代记录并按主题新建研究"
            if cycle is not None and cycle.status == "blocked"
            else
            "上次执行失败，可恢复研究；详情请按 run_id 查看日志"
            if display_status == "failed"
            else None
        ),
        report_iteration=iteration_progress,
    )


@router.get("/research/{run_id}/iterations")
def get_report_iterations(run_id: RunId, request: Request) -> dict[str, object]:
    """草稿是独立的只读资源；取得草稿不表示正式报告已经可交付。"""
    cycle = _manager(request).get_snapshot(run_id).report_iteration
    return {"run_id": run_id, "iteration": cycle.model_dump(mode="json") if cycle else None}


@router.get(
    "/research/{run_id}/result",
    response_model=ResearchResultResponse,
)
def get_research_result(
    run_id: RunId,
    request: Request,
) -> ResearchResultResponse:
    result = _manager(request).get_result(run_id)

    citations: list[CitationResponse] = []

    for evidence_id in result.report_citation_ids:
        evidence = result.evidence[evidence_id]
        source = result.sources[evidence.source_id]

        citations.append(
            CitationResponse(
                evidence_id=evidence_id,
                title=source.title,
                url=source.url,
                text=evidence.text,
                snapshot_id=evidence.snapshot_id,
                section=evidence.section,
                start=evidence.start,
                end=evidence.end,
                retrieved_at=evidence.retrieved_at,
                kind=evidence.kind,
                truncated=evidence.truncated,
                content_hash=evidence.content_hash,
            )
        )

    return ResearchResultResponse(
        run_id=result.run_id,
        report_markdown=result.report_markdown or "",
        tasks=[
            TaskResultResponse(
                id=task.id,
                title=task.title,
                status=task.status,
                summary=task.summary,
                citation_ids=task.citation_ids,
            )
            for task in result.todo_items
        ],
        citations=citations,
    )


@router.get(
    "/research/{run_id}/report",
    response_class=Response,
)
def download_report(
    run_id: RunId,
    request: Request,
) -> Response:
    result = _manager(request).get_result(run_id)

    return Response(
        content=result.report_markdown or "",
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f'attachment; filename="research-{run_id}.md"'
            ),
        },
    )


@router.post(
    "/research/{run_id}/resume",
    response_model=AcceptedResponse,
    status_code=202,
)
def resume_research(
    run_id: RunId,
    body: ResumeRequest,
    request: Request,
) -> AcceptedResponse:
    _manager(request).resume(
        run_id,
        recover_interrupted=body.recover_interrupted,
    )
    return _accepted(run_id)

@router.delete(
      "/research/{run_id}",
      status_code=204,
      response_class=Response,
  )
def delete_research(
      run_id: RunId,
      request: Request,
  ) -> Response:
      _manager(request).delete(run_id)

      return Response(status_code=204)

@router.get("/research/{run_id}/evidence/{evidence_id}/snapshot")
def get_evidence_snapshot(run_id: RunId, evidence_id: str, request: Request) -> dict[str, object]:
    """按研究已有证据读取当时的正文快照，不重新联网。"""
    snapshot = _manager(request).get_snapshot(run_id)
    item = snapshot.state.evidence.get(evidence_id) if snapshot.state else None
    workspace = snapshot.info.config.get("web_rag_workspace")
    if (item is None or not item.snapshot_id or not workspace or
            not (Path(workspace) / run_id / "corpus.sqlite3").is_file()):
        raise ApiError(404, "snapshot_not_found", "该证据没有可用的网页快照", run_id=run_id)
    try:
        page = WebRagStore(workspace, run_id).describe(item.snapshot_id)
    except KeyError as exc:
        raise ApiError(404, "snapshot_not_found", "网页快照不存在", run_id=run_id) from exc
    return {**asdict(page), "evidence_id": evidence_id, "start": item.start, "end": item.end}


@router.get("/research/{run_id}/retrieval/{task_id}")
def get_retrieval_audit(run_id: RunId, task_id: int, request: Request) -> dict[str, object]:
    snapshot = _manager(request).get_snapshot(run_id)
    if snapshot.state is None or not any(t.id == task_id for t in snapshot.state.todo_items):
        raise ApiError(404, "task_not_found", "研究任务不存在", run_id=run_id)
    workspace = snapshot.info.config.get("web_rag_workspace")
    if not workspace or not (Path(workspace) / run_id / "corpus.sqlite3").is_file():
        return {"task_id": task_id, "stages": {}}
    store = WebRagStore(workspace, run_id)
    stages = {name: store.record(f"task:{task_id}:{name}") for name in
              ["discover", "read", "index", "retrieve:local", "local_coverage", "retrieve:web"]}
    return {"task_id": task_id, "stages": stages}


@router.get("/research/{run_id}/events")
def subscribe_research(
    run_id: RunId,
    request: Request,
    after: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
) -> StreamingResponse:
    """Last-Event-ID 用于自动重连，after 用于页面主动重新建立连接。"""
    manager = _manager(request)
    # 在发送 200 响应头之前检查存在性，使不存在的研究正常返回 404。
    manager.get_snapshot(run_id)
    raw_cursor = request.headers.get("last-event-id")
    try:
        cursor = int(raw_cursor) if raw_cursor is not None else after
        if not 0 <= cursor <= 9223372036854775807:
            raise ValueError("invalid cursor")
    except ValueError as exc:
        raise ApiError(422, "invalid_event_id", "事件编号不合法") from exc

    return StreamingResponse(
        research_events(request, manager, run_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
