"""RAG 内部步骤计时；它是节点耗时的分解，不能再与节点耗时相加。"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

logger = logging.getLogger(__name__)


@contextmanager
def rag_timer(stage: str, **details: Any) -> Iterator[dict[str, Any]]:
    """返回可补充的诊断字典，在成功或异常退出时都发出结构化记录。"""
    started = perf_counter()
    status = "completed"
    try:
        yield details
    except BaseException:
        status = "failed"
        raise
    finally:
        duration = perf_counter() - started
        logger.debug("检索计时 · step=%s · %.3fs · %s", stage, duration, status,
                     extra={**details, "event": "web_rag_step_completed", "rag_step": stage,
                            "step_status": status, "duration_s": duration})
