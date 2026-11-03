from __future__ import annotations
import logging
from typing import Any, Optional, Tuple
from deep_researcher.tools.search_tool import SearchTool

from deep_researcher.config import Configuration
from deep_researcher.utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
)

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000
_GLOBAL_SEARCH_TOOL = SearchTool(backend="duckduckgo")

def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str]:
    """Execute configured search backend and normalise response payload."""

    search_api = get_config_value(config.search_api)

    logger.info(
        "🔎 搜索开始：backend=%s loop=%d query_chars=%d max_results=5",
        search_api,
        loop_count,
        len(query),
    )
    try:
        raw_response = _GLOBAL_SEARCH_TOOL.run(
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
        logger.exception(
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
        "✅ 搜索完成：backend=%s resolved_backend=%s results=%d has_answer=%s",
        search_api,
        backend_label,
        len(results),
        bool(answer_text),
    )

    return payload, notices, answer_text, backend_label


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: Optional[str],
    config: Configuration,
) -> tuple[str, str]:
    """Build structured context and source summary for downstream agents."""

    sources_summary = format_sources(search_result)
    context = deduplicate_and_format_sources(
        search_result or {"results": []},
        max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
        fetch_full_page=config.fetch_full_page,
    )

    if answer_text:
        context = f"AI直接答案：\n{answer_text}\n\n{context}"

    logger.info(
        "🧩 搜索上下文整理完成：sources=%d summary_chars=%d context_chars=%d",
        len((search_result or {}).get("results", [])),
        len(sources_summary),
        len(context),
    )

    return sources_summary, context