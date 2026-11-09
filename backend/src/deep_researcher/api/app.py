"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from deep_researcher.api.manager import ResearchManager
from deep_researcher.api.routes import router
from deep_researcher.logging_config import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时创建管理器，关闭时释放后台执行器。"""
    setup_logging()
    app.state.research_manager = ResearchManager()

    try:
        yield
    finally:
        app.state.research_manager.close()


app = FastAPI(
    title="Deep Researcher",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api")