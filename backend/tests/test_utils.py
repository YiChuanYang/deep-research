from enum import Enum

from deep_researcher.utils import (
    get_config_value,
    strip_thinking_tokens,
    deduplicate_and_format_sources,
    format_sources,
)


class MockSearchAPI(Enum):
    TAVILY = "tavily"


def test_get_config_value():
    # 字符串输入
    assert get_config_value("tavily") == "tavily"
    # 枚举输入
    assert get_config_value(MockSearchAPI.TAVILY) == "tavily"


def test_strip_thinking_tokens():
    # 单组标签
    text = "开头<think>思考过程</think>最终答案"
    assert strip_thinking_tokens(text) == "开头最终答案"
    # 无标签
    assert strip_thinking_tokens("纯回答") == "纯回答"


def test_deduplicate_and_format_sources():
    data = [
        {"url": "https://a.com", "title": "A", "content": "内容A"},
        {"url": "https://a.com", "title": "A重复", "content": "重复"},
        {"url": "https://b.com", "title": "B", "content": "内容B"},
    ]

    result = deduplicate_and_format_sources(data, 100)
    # 去重后只剩2条
    assert result.count("信息来源:") == 2
    assert "https://a.com" in result
    assert "https://b.com" in result


def test_format_sources():
    data = {
        "results": [
            {"title": "文章1", "url": "https://a.com"},
            {"title": "文章2", "url": "https://b.com"},
        ]
    }
    result = format_sources(data)
    assert "* 文章1 : https://a.com" in result
    assert "* 文章2 : https://b.com" in result
    # 空输入
    assert format_sources(None) == ""