"""执行检索并整理证据，不修改研究状态。"""

from __future__ import annotations

import logging
from time import perf_counter

from deep_researcher.config import Configuration
from deep_researcher.logging_config import log_operation
from deep_researcher.models import EvidenceBatch, SummaryState
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.tools.search_tool import SearchTool


logger = logging.getLogger(__name__)


class SearchService:
    """连接搜索工具与证据整理逻辑。"""

    def __init__(
        self,
        config: Configuration,
        *,
        search_tool: SearchTool | None = None,
    ) -> None:
        self._config = config
        self._search_tool = (
            search_tool
            if search_tool is not None
            else SearchTool(config=config)
        )

        if self._search_tool.backend != config.search_api.value:
            raise ValueError("搜索工具后端与当前配置不一致")

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
    ) -> EvidenceBatch:
        started_at = perf_counter()
        task = state.get_task(task_id)
        backend = self._search_tool.backend
        query = task.effective_query.strip()

        logger.info(
            "正在检索 · backend=%s · query=%s",
            backend,
            query,
            extra={
                "event": "search_started",
                "backend": backend,
                "query": query,
                "original_query": task.query,
            },
        )

        payload = self._search_tool.run({
            "input": query,
            "max_results": self._config.search_max_results,
        })

        batch = EvidenceService().collect(
            state,
            task_id,
            payload["results"],
            backend=backend,
            max_chars=self._config.evidence_max_chars,
            prefer_full_page=self._config.fetch_full_page,
            query=query,
        )

        batch.notices = list(payload.get("notices") or [])

        if payload.get("answer"):
            batch.notices.append(
                "搜索后端的生成式答案未登记为证据"
            )

        if batch.notices:
            logger.warning(
                "检索提示 · %s",
                "；".join(batch.notices),
            )

        logger.info(
            "检索完成 · %d 条证据 · 编号=%s · 耗时=%.2fs",
            len(batch.evidence_ids),
            "、".join(batch.evidence_ids) or "无",
            perf_counter() - started_at,
            extra={
                "event": "search_completed",
                "backend": backend,
                "evidence_ids": batch.evidence_ids,
            },
        )

        return batch
