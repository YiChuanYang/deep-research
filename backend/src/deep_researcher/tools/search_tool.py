"""搜索工具：执行搜索，统一结果格式"""
from __future__ import annotations

import logging
from typing import Any, List, Dict, Optional

from ddgs import DDGS
from ddgs.exceptions import DDGSException
from tavily import TavilyClient

from deep_researcher.tools.base import Tool, ToolParameter
from deep_researcher.config import Configuration, get_config

logger = logging.getLogger(__name__)


class SearchTool(Tool):
    """多后端搜索工具，统一输出格式"""

    def __init__(
        self,
        name: str = "search_tool",
        description: str = "调用搜索引擎获取外部信息",
        config: Configuration | None = None,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
        )

        self._config = (
            config if config is not None else get_config()
        )
        self._config.require_search()

        self.backend = self._config.search_api.value
        self.timeout = self._config.search_timeout

        self._tavily_client: TavilyClient | None = None

        if self.backend == "tavily":
            key = self._config.tavily_api_key
            assert key is not None

            self._tavily_client = TavilyClient(
                api_key=key.get_secret_value()
            )

        logger.debug(
            "搜索工具已初始化 · "
            "backend=%s timeout=%ss max_results=%d",
            self.backend,
            self.timeout,
            self._config.search_max_results,
            extra={
                "event": "search_initialized",
                "backend": self.backend,
            },
        )

    def run(
        self,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """执行搜索；后端和连接设置由配置决定。"""
        if not isinstance(parameters, dict):
            raise ValueError("parameters 必须是字典")

        unknown = set(parameters) - {"input", "max_results"}
        if unknown:
            raise ValueError(
                f"不支持的搜索参数：{sorted(unknown)}"
            )

        query = parameters.get("input")

        if not isinstance(query, str) or not query.strip():
            raise ValueError("input 必须是非空字符串")

        query = query.strip()

        max_results = parameters.get(
            "max_results",
            self._config.search_max_results,
        )

        if type(max_results) is not int or not 1 <= max_results <= 20:
            raise ValueError("max_results 必须是 1～20 的整数")

        logger.debug(
            "搜索开始 · backend=%s query_chars=%d max_results=%d",
            self.backend,
            len(query),
            max_results,
        )

        if self.backend == "duckduckgo":
            entries = self._search_duckduckgo(
                query,
                max_results,
            )
        elif self.backend == "tavily":
            entries = self._search_tavily(
                query,
                max_results,
                self._config.fetch_full_page,
            )
        else:
            raise ValueError(
                f"不支持的搜索后端：{self.backend}"
            )

        results, notices = self._normalize_results(entries)

        logger.debug(
            "搜索完成 · backend=%s results=%d notices=%d",
            self.backend,
            len(results),
            len(notices),
        )

        return {
            "results": results,
            "backend": self.backend,
            "answer": None,
            "notices": notices,
        }

    # ===================== 后端实现：DuckDuckGo =====================
    def _search_duckduckgo(
        self,
        query: str,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """DuckDuckGo 原生搜索，返回原始结果列表"""
        try:
            logger.debug("🌐 DuckDuckGo 请求 | query=%s", query)
            with DDGS(timeout=self.timeout) as client:
                entries = client.text(
                    query,
                    backend=self.backend,
                    max_results=max_results,
                )
            logger.debug("🌐 DuckDuckGo 返回 | 条数=%d", len(entries))
            return entries

        except DDGSException as e:
            logger.debug("❌ DuckDuckGo 搜索失败 | error=%s", str(e))
            raise RuntimeError(f"DuckDuckGo 搜索失败：{e}") from e

    # ===================== 后端实现：Tavily =====================
    def _search_tavily(
        self,
        query: str,
        max_results: int = 5,
        fetch_full_page: bool = False,
    ) -> list[dict[str, Any]]:
        """请求 Tavily，并使用统一的搜索超时。"""
        if self._tavily_client is None:
            raise RuntimeError("Tavily 客户端尚未初始化")

        try:
            response = self._tavily_client.search(
                query=query,
                search_depth=(
                    "advanced" if fetch_full_page else "basic"
                ),
                max_results=max_results,
                include_answer=False,
                include_raw_content=fetch_full_page,
                timeout=self.timeout,
            )

            return response.get("results", [])

        except Exception as exc:
            raise RuntimeError(
                f"Tavily 搜索失败：{exc}"
            ) from exc

    # ===================== 统一标准化逻辑 =====================
    @staticmethod
    def _normalize_results(
        entries: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], list[str]]:
        """统一字段格式：同时兼容 DuckDuckGo 和 Tavily 原生字段"""
        results: List[Dict[str, Any]] = []
        notices: list[str] = []

        for index, entry in enumerate(entries, start=1):
            # 兼容两种字段名：DuckDuckGo=href/body，Tavily=url/content
            url = entry.get("href") or entry.get("url")
            if not isinstance(url, str) or not url.strip():
                notices.append(f"第{index}条结果缺少有效URL，已忽略")
                continue
            url = url.strip()

            title = entry.get("title") or url
            if not isinstance(title, str) or not title.strip():
                notices.append(f"第{index}条结果缺少标题，已使用URL代替")
                title = url
            else:
                title = title.strip()

            content = entry.get("body") or entry.get("content") or ""

            results.append({
                "url": url,
                "title": title,
                "content": content,
                "raw_content": entry.get("raw_content") or "",
            })

        return results, notices

    # ===================== 工具参数定义（给 tool call 用） =====================
    def get_parameters(self) -> list[ToolParameter]:
        """描述模型可以传入的搜索参数。"""
        return [
            ToolParameter(
                name="input",
                type="string",
                description="搜索关键词，建议使用精准短语",
                required=True,
            ),
            ToolParameter(
                name="max_results",
                type="integer",
                description=(
                    "返回结果的最大条数，范围 1～20；"
                    "未指定时使用应用配置"
                ),
                required=False,
                default=self._config.search_max_results,
            ),
        ]
