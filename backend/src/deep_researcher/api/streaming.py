"""只订阅已保存事件；HTTP 断开不影响后台研究。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from deep_researcher.api.errors import ApiError
from deep_researcher.api.manager import ResearchManager


async def research_events(
    request: Request,
    manager: ResearchManager,
    run_id: str,
    after_event_id: int,
    *,
    max_seconds: float = 60,
) -> AsyncIterator[str]:
    """异步生成 SSE 帧；等待期间不占用线程池工作线程。"""
    cursor = after_event_id
    deadline = monotonic() + max_seconds
    heartbeat = monotonic()
    yield "retry: 1500\n\n"

    while monotonic() < deadline:
        if await request.is_disconnected():
            return

        # SQLite 是同步 IO，通过线程池执行短查询，不阻塞事件循环。
        events = await run_in_threadpool(
            manager.events.read_after,
            run_id=run_id,
            after_event_id=cursor,
        )
        if events:
            for event in events:
                cursor = event.event_id
                yield manager.events.sse_message(event)
            # 不因历史 failed/completed 事件退出，后面可能还有恢复执行。
            continue

        try:
            snapshot = await run_in_threadpool(manager.get_snapshot, run_id)
        except ApiError as exc:
            if exc.status_code != 404:
                raise
            yield 'event: research.removed\ndata: {}\n\n'
            return

        idle = manager.execution(run_id) == "idle"
        terminal = snapshot.report_ready or (
            idle and snapshot.info.status == "failed"
        )
        if terminal:
            # 查状态前后可能刚好写入了最后一批事件，退出前再读一次。
            tail = await run_in_threadpool(
                manager.events.read_after,
                run_id=run_id,
                after_event_id=cursor,
            )
            if tail:
                for event in tail:
                    cursor = event.event_id
                    yield manager.events.sse_message(event)
                continue
            payload = json.dumps({"terminal": True})
            yield f"event: research.sync\ndata: {payload}\n\n"
            return

        if monotonic() - heartbeat >= 15:
            yield ": keep-alive\n\n"
            heartbeat = monotonic()

        # asyncio.sleep 允许断线/服务关闭取消当前订阅。
        # 到期只轮换连接，客户端按已接收的游标重连，后台研究继续。
        await asyncio.sleep(0.25)
