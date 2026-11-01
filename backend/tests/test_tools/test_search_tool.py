from deep_researcher.tools.search_tool import SearchTool
import pytest

#校验空查询抛出值错误
def test_empty_query():
    tool = SearchTool()
    with pytest.raises(ValueError,match="input 必须是非空字符串"):
        tool.run({'input': "  "})

def test_normalize_results():
    # 模拟 DuckDuckGo 原始返回：正常、缺标题、缺URL、字段名不统一 4 种场景
    raw_entries = [
        {"href": "https://a.com", "title": "文章A", "body": "内容A"},
        {"href": "https://b.com", "title": "", "body": "内容B"},
        {"title": "无链接文章", "body": "没有url，应该被跳过"},
        {"url": "https://c.com", "title": "文章C", "content": "内容C"},
    ]

    results, notices = SearchTool._normalize_results(raw_entries)

    # 3条有效结果，1条被跳过的通知
    assert len(results) == 3
    assert len(notices) == 1

    # 字段映射正确
    assert results[0]["url"] == "https://a.com"
    assert results[0]["content"] == "内容A"

    # 缺标题用 URL 兜底
    assert results[1]["title"] == "https://b.com"

    # 兼容 url/content 字段名
    assert results[2]["url"] == "https://c.com"
    assert results[2]["content"] == "内容C"

    # 缺 URL 的条目被正确跳过
    assert "缺少有效URL" in notices[0]