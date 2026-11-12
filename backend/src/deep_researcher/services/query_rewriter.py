"""按研究上下文改写单条查询；失败回退，服务本身不修改研究状态。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from time import perf_counter

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.logging_config import log_operation
from deep_researcher.models import SummaryState

logger = logging.getLogger(__name__)
MAX_QUERY_CHARS = 200


class QueryRewriteService:
    """一个执行任务至多发起一次改写逻辑调用，不追加搜索或自我修订循环。"""

    def __init__(
        self,
        agent_factory: Callable[[], SimpleAgent],
        config: Configuration,
    ) -> None:
        # Callable[[], SimpleAgent] 表示一个无参数、返回 Agent 的工厂函数。
        # 保存工厂而不是立即创建，关闭功能或复用结果时不会初始化模型客户端。
        self._agent_factory = agent_factory
        self._config = config

    @staticmethod
    def _validate_query(response: str) -> str:
        """只接受短的单行文本；格式检查不等于语义相关性判定。"""
        if not isinstance(response, str):
            raise ValueError("改写结果必须是字符串")
        query = response.strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ValueError("改写查询必须为 1～200 个字符")
        if any(ord(char) < 32 or ord(char) == 127 for char in query):
            raise ValueError("改写查询不能包含换行或控制字符")
        if query.startswith(("{", "[", "#", "- ", "* ", "查询：", "Query:")) or "`" in query:
            raise ValueError("改写查询不能是结构化对象、解释或 Markdown")
        if "<think" in query.lower() or "</think>" in query.lower():
            raise ValueError("改写查询不能包含思考标签")
        return query

    @log_operation("查询改写", state_parameter="state", task_parameter="task_id")
    def rewrite_task(self, state: SummaryState, task_id: int) -> str:
        task = state.get_task(task_id)
        # 已有结果来自检查点：包括成功改写、关闭功能和异常回退的结果。
        if task.search_query is not None:
            return task.search_query
        if not isinstance(task.query, str) or not task.query.strip():
            raise ValueError("规划查询不能为空，无法执行查询改写或回退")
        original = task.query.strip()
        if not self._config.enable_query_rewrite:
            logger.debug("查询改写已关闭 · 沿用规划查询", extra={"event": "query_rewrite_disabled"})
            return original

        prompt = json.dumps({
            "research_topic": state.research_topic,
            "task_title": task.title,
            "task_intent": task.intent,
            "original_query": original,
        }, ensure_ascii=False)
        started_at = perf_counter()
        agent: SimpleAgent | None = None
        response: str | None = None
        logger.info("正在改写查询 · 原查询=%s", original, extra={
            "event": "query_rewrite_started", "original_query": original,
        })
        try:
            agent = self._agent_factory()
            agent.enable_tool_call = False
            # max_tool_iterations=0 禁用工具循环；一条任务只提交一次逻辑模型请求。
            # SDK 自身的网络重试仍由现有 llm_max_retries 控制。
            response = agent.run(input_text=prompt, max_tool_iterations=0, max_tokens=256)
            query = self._validate_query(response)
        except Exception as exc:
            # 改写是可降级的增强步骤。用户中断不属于 Exception，继续向上传播。
            logger.warning("查询改写未采用 · 沿用原查询 · 原因=%s", type(exc).__name__, extra={
                "event": "query_rewrite_fallback",
                "original_query": original,
                "search_query": original,
                "response_text": response,
                "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                "duration_s": round(perf_counter() - started_at, 3),
            })
            return original
        finally:
            if agent is not None:
                agent.clear_history()

        logger.info("查询已确定 · %s", query, extra={
            "event": "query_rewrite_completed",
            "original_query": original,
            "search_query": query,
            "changed": query != original,
            "duration_s": round(perf_counter() - started_at, 3),
        })
        # 只返回结果，由图节点写入状态；查询没有变时同样保存，避免再次改写。
        return query
