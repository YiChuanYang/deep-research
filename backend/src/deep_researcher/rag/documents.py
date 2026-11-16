"""保留 Markdown 结构与精确偏移的正文分块。"""
from __future__ import annotations

import re
from urllib.parse import urldefrag, urlsplit, urlunsplit

from deep_researcher.rag.store import PageSnapshot, TextChunk, digest


def public_url(value: str) -> str | None:
    """网页读取通过托管 Extract；拒绝凭证和明显本地地址。"""
    import ipaddress

    try:
        parts = urlsplit(urldefrag(value.strip()).url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def clean_markdown(text: str) -> str:
    """去掉脚本、图片和连续重复行，保留标题、表格、代码和否定条件。"""
    text = re.sub(r"<(script|style|nav)\b[^>]*>[\s\S]*?</\1>", "", text, flags=re.I)
    text = re.sub(r"!\[[^\]]*\]\([^\n)]*\)", "", text)
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\x00", "").splitlines():
        value = line.rstrip()
        if value and lines and value == lines[-1]:
            continue
        lines.append(value)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk_page(page: PageSnapshot, size: int, overlap: int) -> list[TextChunk]:
    """优先在段落/句末切分；位置相对于已保存的清理后正文。"""
    if size <= overlap or overlap < 0:
        raise ValueError("分块大小必须大于重叠长度")
    text = page.text
    headings: list[tuple[int, str]] = []
    stack: list[str] = []
    for match in re.finditer(r"(?m)^(#{1,6})\s+(.+)$", text):
        level = len(match[1])
        stack = stack[:level - 1]
        stack.append(match[2].strip())
        headings.append((match.start(), " / ".join(stack)))
    result: list[TextChunk] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            # 保留至少半块正文，避免短标题导致大量零碎片段。
            cuts = list(re.finditer(r"\n\n|[。！？.!?]\s", text[start + size // 2:end]))
            if cuts:
                end = start + size // 2 + cuts[-1].end()
        left = start
        while left < end and text[left].isspace():
            left += 1
        right = end
        while right > left and text[right - 1].isspace():
            right -= 1
        section = next((title for offset, title in reversed(headings) if offset <= left), "正文")
        if right > left:
            key = digest(f"{page.snapshot_id}:{left}:{right}")
            result.append(TextChunk(chunk_id=key, snapshot_id=page.snapshot_id,
                                    text=text[left:right], section=section, start=left, end=right))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return result
