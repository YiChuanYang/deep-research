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
        backend: Optional[str] = None,
        timeout: int = 10,
        config: Optional[Configuration] = None,
    ) -> None:
        super().__init__(name=name, description=description)

        # 1. 基础参数校验
        if not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("timeout 必须是正整数")

        # 2. 配置兜底
        self._config = config or get_config()

        # 3. 确定最终后端：优先传参，其次读配置（枚举取 value 转字符串）
        self.backend = backend or self._config.search_api.value
        self.timeout = timeout

        # 4. 后端专属客户端预初始化
        self._tavily_client: Optional[TavilyClient] = None
        if self.backend == "tavily":
            api_key = self._config.tavily_api_key
            if not api_key:
                raise ValueError("使用 tavily 后端请先配置 TAVILY_API_KEY")
            self._tavily_client = TavilyClient(api_key=api_key)

        logger.debug("✅ SearchTool 初始化成功 | backend=%s", self.backend)

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """统一入口：校验参数、分发后端、标准化返回"""

        if not isinstance(parameters, dict):
            raise ValueError("parameters 必须是字典")

        # 1. 核心参数校验
        query = parameters.get("input")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("input 必须是非空字符串")
        query = query.strip()

        max_results = parameters.get("max_results", 5)
        if type(max_results) is not int or max_results <= 0:
            raise ValueError("max_results 必须是正整数")

        fetch_full_page = parameters.get("fetch_full_page", self._config.fetch_full_page)
        if not isinstance(fetch_full_page, bool):
            raise ValueError("fetch_full_page 必须是布尔值")

        # 2. 按后端分发搜索（只拿原生结果，不做格式化）
        logger.debug(
            "🔍 搜索开始 | backend=%s query_len=%d max_results=%d",
            self.backend,
            len(query),
            max_results,
        )

        if self.backend == "duckduckgo":
            entries = self._search_duckduckgo(query, max_results)
        elif self.backend == "tavily":
            entries = self._search_tavily(query, max_results, fetch_full_page)
        else:
            raise ValueError(f"不支持的搜索后端：{self.backend}")

        # 3. 统一标准化（两个后端共用同一套解析逻辑）
        results, notices = self._normalize_results(entries)

        logger.debug(
            "✅ 搜索完成 | backend=%s results=%d notices=%d",
            self.backend,
            len(results),
            len(notices),
        )

        # 4. 统一返回结构，所有后端完全一致
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
            logger.exception("❌ DuckDuckGo 搜索失败 | error=%s", str(e))
            raise RuntimeError(f"DuckDuckGo 搜索失败：{e}") from e

    # ===================== 后端实现：Tavily =====================
    def _search_tavily(
        self,
        query: str,
        max_results: int = 5,
        fetch_full_page: bool = False,
    ) -> List[Dict[str, Any]]:
        """Tavily 原生搜索，返回原始结果列表"""
        try:
            logger.debug("🌐 Tavily 请求 | query=%s", query)
            response = self._tavily_client.search(
                query=query,
                search_depth="advanced" if fetch_full_page else "basic",
                max_results=max_results,
                include_answer=False,
                include_raw_content=fetch_full_page,
            )

            results = response.get("results", [])
            logger.debug("🌐 Tavily 返回 | 条数=%d", len(results))
            return results

        except Exception as e:
            logger.exception("❌ Tavily 搜索失败 | error=%s", str(e))
            raise RuntimeError(f"Tavily 搜索失败：{e}") from e

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
            })

        return results, notices

    # ===================== 工具参数定义（给 tool call 用） =====================
    def get_parameters(self) -> List[ToolParameter]:
        """返回工具的 OpenAI 参数 schema"""
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
                description="返回结果的最大条数，默认5条",
                required=False,
                default=5,
            ),
        ]
