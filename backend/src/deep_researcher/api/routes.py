"""研究 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from deep_researcher.api.manager import (
    ResearchManager,
    RunConflictError,
)
from deep_researcher.api.schemas import (
    AcceptedResponse,
    CitationResponse,
    ResearchRequest,
    ResearchResultResponse,
    ResumeRequest,
    RunStatusResponse,
    TaskResultResponse,
)


router = APIRouter()
def _manager(request: Request) -> ResearchManager:
    """取得应用启动时创建的研究管理器。"""
    return request.app.state.research_manager


def _get_info(
    manager: ResearchManager,
    run_id: str,
):
    try:
        return manager.get_run_info(run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="研究编号不存在",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

@router.get("/health")
def health()->dict[str,str]:
    return {"status":"ok"}

@router.post(
        "/research",
        response_model=AcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED)
def start_research(
    body:ResearchRequest,
    request:Request
)->AcceptedResponse:
    
    try:
        run_id= _manager(request).start(body.topic,body.max_tasks)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return AcceptedResponse(run_id=run_id)

@router.get("/research/{run_id}",
            response_model=RunStatusResponse)
def get_research_status(
    run_id:str,
    request:Request,
)->RunStatusResponse:
    info = _get_info(_manager(request), run_id)

    # 不把内部异常原文直接暴露给 HTTP 客户端。
    error_message = (
        "研究执行失败，请按 run_id 检查服务日志"
        if info.status == "failed"
        else None
    )

    return RunStatusResponse(
        run_id=info.run_id,
        topic=info.topic,
        status=info.status,
        error_message=error_message,
    )
@router.get(
    "/research/{run_id}/result",
    response_model=ResearchResultResponse,
)
def get_research_result(
    run_id: str,
    request: Request,
) -> ResearchResultResponse:
    manager = _manager(request)
    info = _get_info(manager, run_id)

    if info.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="研究尚未完成",
        )

    result = manager.get_result(run_id)

    citations = []
    for evidence_id in result.report_citation_ids:
        evidence = result.evidence[evidence_id]
        source = result.sources[evidence.source_id]

        citations.append(
            CitationResponse(
                evidence_id=evidence.evidence_id,
                title=source.title,
                url=source.url,
                text=evidence.text,
            )
        )

    tasks = [
        TaskResultResponse(
            id=task.id,
            title=task.title,
            status=task.status,
            summary=task.summary,
            citation_ids=task.citation_ids,
        )
        for task in result.todo_items
    ]

    return ResearchResultResponse(
        run_id=result.run_id,
        report_markdown=result.report_markdown or "",
        tasks=tasks,
        citations=citations,
    )


@router.post(
    "/research/{run_id}/resume",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_research(
    run_id: str,
    body: ResumeRequest,
    request: Request,
) -> AcceptedResponse:
    try:
        _manager(request).resume(
            run_id,
            recover_interrupted=body.recover_interrupted,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="研究编号不存在",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RunConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return AcceptedResponse(run_id=run_id)
    