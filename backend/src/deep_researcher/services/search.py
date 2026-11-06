from __future__ import annotations
import logging
from time import perf_counter
from typing import Any, Optional, Tuple
from deep_researcher.tools.search_tool import SearchTool

from deep_researcher.config import Configuration
from deep_researcher.models import SummaryState
from deep_researcher.utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
)

from deep_researcher.logging_config import log_operation

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000

@log_operation("搜索")
def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int=3,
    *,
    search_tool: SearchTool | None = None,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str]:
    """Execute configured search backend and normalise response payload."""

    started_at = perf_counter()
    search_api = get_config_value(config.search_api)
    active_search_tool = search_tool or SearchTool(config=config)

    logger.info(
        "正在检索 · backend=%s", search_api,
    )
    try:
        raw_response = active_search_tool.run(
            {
                "input": query,
                "backend": search_api,
                "mode": "structured",
                "fetch_full_page": config.fetch_full_page,
                "max_results": 5,
                "max_tokens_per_source": MAX_TOKENS_PER_SOURCE,
                "loop_count": loop_count,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.debug(
            "❌ 搜索失败：backend=%s error_type=%s",
            search_api,
            type(exc).__name__,
        )
        raise

    notices: list[str] = []
    if isinstance(raw_response, str):
        notices.append(raw_response)
        logger.warning(
            "⚠️ 搜索后端返回提示：backend=%s loop=%d",
            search_api,
            loop_count,
        )
        payload: dict[str, Any] = {
            "results": [],
            "backend": search_api,
            "answer": None,
            "notices": notices,
        }
    else:
        payload = raw_response
        notices.extend(
            notice
            for notice in payload.get("notices") or []
            if isinstance(notice, str)
        )

    backend_label = str(payload.get("backend") or search_api)
    answer_text = payload.get("answer")
    results = payload.get("results", [])

    if notices and not isinstance(raw_response, str):
        logger.warning(
            "⚠️ 搜索结果包含提示：backend=%s notice_count=%d",
            backend_label,
            len(notices),
        )
    logger.info(
        "检索完成 · %d 个来源 · 耗时=%.2fs",
        len(results), perf_counter() - started_at,
        extra={
            "event": "search_completed",
            "backend": backend_label,
            "source_count": len(results),
            "duration_s": round(perf_counter() - started_at, 3),
        },
    )

    return payload, notices, answer_text, backend_label


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: Optional[str]=None,
) -> tuple[str, str]:
    """Build structured context and source summary for downstream agents."""

    sources_summary = format_sources(search_result)
    context = deduplicate_and_format_sources(
        search_result or {"results": []},
        max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
    )

    if answer_text:
        context = f"AI直接答案：\n{answer_text}\n\n{context}"

    logger.debug(
        "🧩 搜索上下文整理完成：sources=%d summary_chars=%d context_chars=%d",
        len((search_result or {}).get("results", [])),
        len(sources_summary),
        len(context),
    )

    return sources_summary, context


class SearchService:
    """执行单个任务的搜索，并将结果写入共享 SummaryState。"""

    def __init__(self, config: Configuration) -> None:
        self._config = config
        self._search_tool = SearchTool(config=config)

    @log_operation("任务搜索", task_parameter="task_id")
    def search_task(self, state: SummaryState, task_id: int) -> None:
        task = state.get_task(task_id)
        loop_count = state.research_loop_count + 1

        payload, notices, answer_text, backend = dispatch_search(
            task.query,
            self._config,
            loop_count,
            search_tool=self._search_tool,
        )
        sources_summary, context = prepare_research_context(
            payload,
            answer_text,
        )

        task.sources_summary = sources_summary
        task.notices = notices
        state.search_contexts[task_id] = context
        state.sources_gathered.append(sources_summary)
        state.web_research_results.append(context)
        state.research_loop_count = loop_count

        logger.debug(
            "🔎 任务搜索结果已写入状态：task_id=%d backend=%s sources=%d",
            task_id,
            backend,
            len(payload.get("results", [])) if payload else 0,
        )