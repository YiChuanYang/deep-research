from __future__ import annotations
import logging
from time import perf_counter
from typing import Any, Optional, Tuple
from deep_researcher.tools.search_tool import SearchTool
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.config import Configuration, get_config
from deep_researcher.models import SummaryState
from deep_researcher.utils import (
    deduplicate_and_format_sources,
    format_sources
)

from deep_researcher.logging_config import log_operation

logger = logging.getLogger(__name__)

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

    search_api = config.search_api.value
    active_search_tool = (
        search_tool
        if search_tool is not None
        else SearchTool(config=config)
    )

    if active_search_tool.backend != search_api:
        raise ValueError(
            "注入的搜索工具后端与当前配置不一致"
        )

    logger.info(
        "正在检索 · backend=%s", search_api,
        extra={"event": "search_started", "backend": search_api},
    )
    try:
        raw_response = active_search_tool.run({
            "input": query,
            "max_results": config.search_max_results,
        })
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
    answer_text: str | None = None,
    config: Configuration | None = None,
) -> tuple[str, str]:
    """根据当前配置整理来源和研究上下文。"""
    active_config = (
        config if config is not None else get_config()
    )

    sources_summary = format_sources(search_result)

    context = deduplicate_and_format_sources(
        search_result or {"results": []},
        max_tokens_per_source=(
            active_config.max_tokens_per_source
        ),
        fetch_full_page=active_config.fetch_full_page,
    )

    if answer_text:
        context = f"AI直接答案：\n{answer_text}\n\n{context}"

    logger.debug(
        "搜索上下文已整理 · "
        "sources=%d context_chars=%d full_page=%s",
        len((search_result or {}).get("results", [])),
        len(context),
        active_config.fetch_full_page,
    )

    return sources_summary, context


class SearchService:
    """执行单个任务的搜索，并将结果写入共享 SummaryState。"""

    def __init__(self, config: Configuration) -> None:
        self._config = config
        self._search_tool = SearchTool(config=config)

    @log_operation(
        "任务搜索",
        task_parameter="task_id",
        state_parameter="state",
        checkpoint="search",
    )
    def search_task(
        self,
        state: SummaryState,
        task_id: int,
    ) -> None:
        """搜索材料，登记证据，再生成带编号的上下文。"""
        task = state.get_task(task_id)
        loop_count = state.research_loop_count + 1

        payload, notices, answer_text, backend = dispatch_search(
            task.query,
            self._config,
            loop_count,
            search_tool=self._search_tool,
        )

        task.notices = list(notices)

        if answer_text:
            task.notices.append(
                "搜索后端的生成式答案未登记为证据"
            )

        evidence_service = EvidenceService()

        evidence_ids = evidence_service.ingest(
            state,
            task_id,
            (payload or {}).get("results", []),
            backend=backend,
            max_chars=self._config.evidence_max_chars,
            prefer_full_page=self._config.fetch_full_page,
        )

        context = evidence_service.context(
            state, evidence_ids
        )

        sources_summary = evidence_service.references(
            state, evidence_ids
        )

        task.sources_summary = sources_summary
        state.search_contexts[task_id] = context

        # 暂时保留现有兼容字段。
        state.sources_gathered.append(sources_summary)
        state.web_research_results.append(context)
        state.research_loop_count = loop_count

        logger.debug(
            "🔎 任务搜索结果已写入状态：task_id=%d backend=%s sources=%d",
            task_id,
            backend,
            len(payload.get("results", [])) if payload else 0,
        )
