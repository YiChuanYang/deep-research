"""根据统一配置访问模型接口。"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any
from collections.abc import Generator
from openai import OpenAI
from openai.types.chat import ChatCompletion

from deep_researcher.config import Configuration, get_config
from deep_researcher.llm.structured import ModelOutputError


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
            raise ModelOutputError("模型响应中没有 choices")

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

    def stream_text(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """
        流式生成纯文本。

        Generator[str, None, None] 的含义：

        - 每次 yield 一个字符串片段；
        - 不接收外部 send() 参数；
        - 不通过 return 返回业务结果。

        调用这个方法只会得到一个生成器。
        真正的 HTTP 请求发生在开始遍历生成器之后。
        """
        # 这条入口只给 Reporter 使用。
        # 工具调用参数不能在文本流中直接执行，
        # 因为工具 JSON 可能还没有接收完整。
        if any(
            key in kwargs
            for key in (
                "tools",
                "tool_choice",
                "functions",
                "function_call",
            )
        ):
            raise ValueError(
                "stream_text 只支持纯文本，不支持工具调用"
            )

        if kwargs.get("n", 1) != 1:
            raise ValueError(
                "流式报告只允许生成一条候选回复"
            )

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

        # stream=True 让 SDK 返回可遍历的流对象。
        request.update(
            model=self.model,
            messages=messages,
            stream=True,
        )

        started_at = perf_counter()
        first_text_at: float | None = None
        finish_reason: str | None = None
        total_tokens: int | None = None
        has_text = False

        logger.info(
            "开始流式生成 · model=%s",
            self.model,
            extra={
                "event": "model_stream_started",
                "provider": self.llm_provider,
                "model": self.model,
            },
        )

        # 此时返回值不是 ChatCompletion，
        # 而是可以逐块遍历的 Stream[ChatCompletionChunk]。
        stream = self.client.chat.completions.create(
            **request
        )

        try:
            for chunk in stream:
                # 某些兼容服务会在最后发送只有 usage 的分片。
                # 因此不能假设每个 chunk 都有 choices。
                usage = getattr(chunk, "usage", None)

                if usage is not None:
                    total_tokens = getattr(
                        usage,
                        "total_tokens",
                        None,
                    )

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                if getattr(delta, "refusal", None):
                    raise RuntimeError(
                        "模型拒绝生成报告"
                    )

                # 报告流式入口不接受工具调用。
                if (
                    getattr(delta, "tool_calls", None)
                    or getattr(delta, "function_call", None)
                ):
                    raise RuntimeError(
                        "纯文本流式入口收到工具调用"
                    )

                text = getattr(delta, "content", None)

                if text:
                    if first_text_at is None:
                        first_text_at = perf_counter()

                    if text.strip():
                        has_text = True

                    # 这里只返回正文分片。
                    # reasoning_content 等供应商扩展字段不进入报告。
                    yield text

                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason

            # finish_reason 不是 stop 时，
            # 不能把截断结果当作正式报告。
            if finish_reason != "stop":
                raise RuntimeError(
                    f"流式生成未正常结束：{finish_reason}"
                )

            if not has_text:
                raise RuntimeError(
                    "模型返回了空正文"
                )

            logger.info(
                "流式生成完成 · 耗时=%.2fs",
                perf_counter() - started_at,
                extra={
                    "event": "model_stream_completed",
                    "provider": self.llm_provider,
                    "model": self.model,
                    "ttft_s": (
                        None
                        if first_text_at is None
                        else first_text_at - started_at
                    ),
                    # 没有 usage 时保留 None，不伪造为 0。
                    "tokens": total_tokens,
                },
            )

        finally:
            # 正常结束、异常结束、上层主动 close，
            # 都必须释放 HTTP 流连接。
            close_method = getattr(
                stream,
                "close",
                None,
            )

            if callable(close_method):
                close_method()

    def close(self) -> None:
        """关闭模型客户端的连接资源。"""
        self.client.close()
