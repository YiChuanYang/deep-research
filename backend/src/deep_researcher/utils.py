"""模型输出的通用文本处理。"""


def strip_thinking_tokens(text: str) -> str:
    """去掉模型返回中的思考部分"""
    while "<think>" in text and "</think>" in text:
        start = text.find("<think>")
        end = text.find("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text
