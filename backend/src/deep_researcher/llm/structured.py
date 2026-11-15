"""结果工具的接收边界，不执行模型返回的工具。"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import APIConnectionError, APIStatusError
from openai.types.chat import ChatCompletion


class ModelOutputError(ValueError):
    """模型输出协议错误，与程序状态错误区分。"""


def result_arguments(response: ChatCompletion, name: str) -> str:
    if len(response.choices) != 1:
        raise ModelOutputError("需要唯一的模型结果")
    choice = response.choices[0]
    message = choice.message
    if choice.finish_reason in {"length", "content_filter"} or message.refusal:
        raise ModelOutputError("模型输出被截断或拒绝")
    calls = message.tool_calls or []
    if not calls:
        if isinstance(message.content, str) and message.content.strip():
            return message.content
        raise ModelOutputError("模型没有返回结果")
    if len(calls) != 1 or getattr(calls[0], "type", None) != "function":
        raise ModelOutputError("只能提交一次结果工具")
    if calls[0].function.name != name:
        raise ModelOutputError("模型返回了未知工具")
    return calls[0].function.arguments


def parse_json(text: str) -> Any:
    """只兼容包住整个 JSON 的代码围栏，不猜测缺失字段。"""
    fence = re.fullmatch(
        r"```(?:json)?\s*\n([\s\S]*?)\n```", text.strip(), re.IGNORECASE
    )
    try:
        return json.loads(fence.group(1) if fence else text)
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"JSON 格式错误：{exc.msg}") from exc


def transient_model_error(error: Exception) -> bool:
    # 鉴权、程序错误和数据库错误不属于可降级的模型故障。
    return isinstance(error, APIConnectionError) or (
        isinstance(error, APIStatusError)
        and (error.status_code in {408, 429} or error.status_code >= 500)
    )
