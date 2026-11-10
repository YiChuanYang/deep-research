"""FastAPI 应用创建与生命周期管理。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import (
    HTTPException as StarletteHTTPException,
)

from deep_researcher.api.errors import ApiError
from deep_researcher.api.manager import ResearchManager
from deep_researcher.api.routes import router
from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
    setup_logging,
)


logger = logging.getLogger(__name__)


def create_app(
    manager_factory: Callable[
        [], ResearchManager
    ] = ResearchManager,
) -> FastAPI:
    """创建应用；允许验证时传入模拟管理器。"""

    @asynccontextmanager
    async def lifespan(
        app: FastAPI,
    ) -> AsyncIterator[None]:
        setup_logging()

        manager = manager_factory()
        app.state.research_manager = manager

        logger.info(
            "API 服务已启动",
            extra={"event": "api_started"},
        )

        try:
            yield
        finally:
            # close() 会等待正在执行的线程。
            # 放到线程池中等待，避免阻塞事件循环。
            await run_in_threadpool(manager.close)

            logger.info(
                "API 服务已关闭",
                extra={"event": "api_stopped"},
            )

    app = FastAPI(
        title="Deep Researcher",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.include_router(router, prefix="/api")

    @app.exception_handler(ApiError)
    async def api_error_handler(
        request: Request,
        exc: ApiError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "run_id": exc.run_id,
                    "error_id": exc.error_id,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "请求参数不合法",
                    "fields": [
                        {
                            "field": ".".join(
                                str(part)
                                for part in item["loc"]
                            ),
                            "type": item["type"],
                        }
                        for item in exc.errors()
                    ],
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        run_id = request.path_params.get("run_id")

        with log_context(
            run_id=run_id,
            stage="API请求",
        ):
            error_id = log_exception_once(
                logger,
                "API 请求失败 · %s %s",
                request.method,
                request.url.path,
                error=exc,
            )

        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "服务内部错误，请查看日志",
                    "run_id": run_id,
                    "error_id": error_id,
                }
            },
        )

    return app


app = create_app()