"""根据统一配置访问模型接口。"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletion

from deep_researcher.config import Configuration, get_config


logger = logging.getLogger(__name__)


class MyLLM:
    """封装当前使用的 Chat Completions 与原生工具调用协议。"""

    def __init__(
        self,
        config: Configuration | None = None,
    ) -> None:
        self.config = config if config is not None else get_config()
        self.config.require_llm()

        self.llm_provider = self.config.llm_provider
        self.model = self.config.llm_model_id
        self.base_url = self.config.llm_base_url
        self.timeout = self.config.llm_timeout

        key = self.config.llm_api_key
        assert key is not None

        self.client = OpenAI(
            api_key=key.get_secret_value(),
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.config.llm_max_retries,
        )

        logger.debug(
            "模型客户端已初始化 · "
            "provider=%s model=%s timeout=%ss retries=%d",
            self.llm_provider,
            self.model,
            self.timeout,
            self.config.llm_max_retries,
            extra={
                "event": "llm_initialized",
                "provider": self.llm_provider,
                "model": self.model,
            },
        )

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> ChatCompletion:
        """执行一次模型调用，允许覆盖本次生成参数。"""
        request: dict[str, Any] = dict(kwargs)

        request.setdefault(
            "max_tokens",
            self.config.llm_max_tokens,
        )

        if self.config.llm_temperature is not None:
            request.setdefault(
                "temperature",
                self.config.llm_temperature,
            )

        request.update(
            model=self.model,
            messages=messages,
            stream=False,
        )

        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        logger.debug(
            "模型请求开始 · model=%s messages=%d tools=%d",
            self.model,
            len(messages),
            len(tools or []),
            extra={
                "event": "model_request",
                "provider": self.llm_provider,
                "model": self.model,
            },
        )

        started_at = perf_counter()

        response = self.client.chat.completions.create(**request)

        if not response.choices:
            raise ValueError("模型响应中没有 choices")

        usage = getattr(response, "usage", None)
        duration = perf_counter() - started_at

        logger.debug(
            "模型响应完成 · model=%s tokens=%s 耗时=%.2fs",
            self.model,
            getattr(usage, "total_tokens", None),
            duration,
            extra={
                "event": "model_response",
                "provider": self.llm_provider,
                "model": self.model,
                "tokens": getattr(usage, "total_tokens", None),
                "duration_s": round(duration, 3),
            },
        )

        return response

    def close(self) -> None:
        """关闭模型客户端的连接资源。"""
        self.client.close()