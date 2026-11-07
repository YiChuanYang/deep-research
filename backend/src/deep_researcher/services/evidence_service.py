"""登记材料、校验引用，并生成可回查的参考条目。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from html import escape
from typing import Any, Literal
from urllib.parse import quote, urldefrag, urlsplit

from deep_researcher.logging_config import log_operation
from deep_researcher.models import Evidence, Source, SummaryState


logger = logging.getLogger(__name__)

# 捕获约定的 [E编号]，再进一步检查格式和范围。
CITATION = re.compile(r"\[(E[^\]\r\n]*)\]")
ID_PATTERN = re.compile(r"E\d{4,}")


class CitationError(ValueError):
    """引用格式、范围或关联数据无效。"""


class EvidenceService:
    """编号和引用由程序管理，模型只选择已有编号。"""

    @staticmethod
    def _next_id(
        prefix: str,
        records: Mapping[str, object],
    ) -> str:
        """分配当前研究中尚未使用的编号。"""
        number = len(records) + 1

        while f"{prefix}{number:04d}" in records:
            number += 1

        return f"{prefix}{number:04d}"

    @log_operation(
        "登记证据",
        state_parameter="state",
        task_parameter="task_id",
    )
    def ingest(
        self,
        state: SummaryState,
        task_id: int,
        results: list[dict[str, Any]],
        *,
        backend: str,
        max_chars: int,
        prefer_full_page: bool,
    ) -> list[str]:
        """重复材料复用编号，内容改变时保留新片段。"""
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正数")

        task = state.get_task(task_id)
        selected: list[str] = []
        previous_count = len(state.evidence)

        for row in results:
            raw_url = row.get("url")

            if not isinstance(raw_url, str):
                continue

            # 去除页面内锚点，其余 URL 参数保留。
            url = urldefrag(raw_url.strip()).url
            parts = urlsplit(url)

            if (
                parts.scheme not in {"http", "https"}
                or not parts.netloc
            ):
                continue

            raw = (
                row.get("raw_content")
                if prefer_full_page
                else None
            )

            kind: Literal[
                "search_snippet", "page_excerpt"
            ] = "page_excerpt"

            if not isinstance(raw, str) or not raw.strip():
                raw = row.get("content")
                kind = "search_snippet"

            # 只有标题和 URL，没有材料正文，不登记为证据。
            if not isinstance(raw, str) or not raw.strip():
                continue

            raw = raw.strip()
            text = raw[:max_chars]
            truncated = len(raw) > max_chars

            source = next(
                (
                    item
                    for item in state.sources.values()
                    if item.url == url
                ),
                None,
            )

            if source is None:
                source = Source(
                    source_id=self._next_id(
                        "S", state.sources
                    ),
                    url=url,
                    title=str(row.get("title") or url),
                )
                state.sources[source.source_id] = source

            # 同一任务、来源、检索方式和片段内容相同，
            # 视为重复取得同一份材料。
            evidence = next(
                (
                    item
                    for item in state.evidence.values()
                    if item.task_id == task_id
                    and item.source_id == source.source_id
                    and item.text == text
                    and item.kind == kind
                    and item.truncated == truncated
                    and item.query == task.query
                    and item.backend == backend
                ),
                None,
            )

            if evidence is None:
                evidence = Evidence(
                    evidence_id=self._next_id(
                        "E", state.evidence
                    ),
                    source_id=source.source_id,
                    task_id=task_id,
                    text=text,
                    retrieved_at=datetime.now(
                        timezone.utc
                    ).isoformat(timespec="seconds"),
                    query=task.query,
                    backend=backend,
                    kind=kind,
                    truncated=truncated,
                )

                state.evidence[evidence.evidence_id] = evidence

            if evidence.evidence_id not in selected:
                selected.append(evidence.evidence_id)

        task.evidence_ids = selected

        logger.info(
            "证据已登记 · 可用=%d 新增=%d",
            len(selected),
            len(state.evidence) - previous_count,
            extra={
                "event": "evidence_registered",
                "evidence_ids": selected,
                "evidence_count": len(selected),
            },
        )

        return selected

    @staticmethod
    def resolve(
        state: SummaryState,
        evidence_id: str,
    ) -> tuple[Evidence, Source]:
        """根据证据编号查回片段和来源。"""
        item = state.evidence.get(evidence_id)

        if item is None or item.evidence_id != evidence_id:
            raise CitationError(
                f"证据不存在或编号不一致：{evidence_id}"
            )

        source = state.sources.get(item.source_id)

        if (
            source is None
            or source.source_id != item.source_id
        ):
            raise CitationError(
                f"证据的来源不存在或编号不一致：{evidence_id}"
            )

        return item, source

    def context(
        self,
        state: SummaryState,
        evidence_ids: Iterable[str],
    ) -> str:
        """生成模型输入；正文使用已保存的片段，不再次裁剪。"""
        rows: list[dict[str, Any]] = []

        for evidence_id in dict.fromkeys(evidence_ids):
            item, source = self.resolve(
                state, evidence_id
            )

            rows.append({
                "evidence_id": item.evidence_id,
                "source_id": source.source_id,
                "title": source.title,
                "url": source.url,
                "kind": item.kind,
                "retrieved_at": item.retrieved_at,
                "truncated": item.truncated,
                "text": item.text,
            })

        if not rows:
            return ""

        return json.dumps(
            rows,
            ensure_ascii=False,
            indent=2,
        )

    def validate(
        self,
        state: SummaryState,
        text: str,
        allowed_ids: Iterable[str],
    ) -> list[str]:
        """校验引用格式和范围，不判断语义支持关系。"""
        if re.search(
            r"https?://|www\.",
            text,
            flags=re.IGNORECASE,
        ):
            raise CitationError(
                "正文不要生成 URL，请使用 [E编号] 引用"
            )

        found = list(dict.fromkeys(
            CITATION.findall(text)
        ))

        if not found:
            raise CitationError(
                "输出没有使用 [E编号] 引用"
            )

        allowed = set(allowed_ids)

        for evidence_id in found:
            if ID_PATTERN.fullmatch(evidence_id) is None:
                raise CitationError(
                    f"引用格式无效：[{evidence_id}]"
                )

            if evidence_id not in allowed:
                raise CitationError(
                    f"引用不在本次输入范围：{evidence_id}"
                )

            self.resolve(state, evidence_id)

        logger.debug(
            "引用结构校验通过 · %d 条",
            len(found),
            extra={
                "event": "citations_validated",
                "citation_ids": found,
            },
        )

        return found

    @staticmethod
    def _plain(text: str) -> str:
        """将外部标题和片段转为适合参考条目展示的文本。"""
        value = escape(
            " ".join(text.split()),
            quote=False,
        )
        return re.sub(
            r"([\\`*_\[\]])",
            r"\\\1",
            value,
        )

    def references(
        self,
        state: SummaryState,
        evidence_ids: Iterable[str],
    ) -> str:
        """生成来源及片段预览，完整正文仍保存在 state 中。"""
        blocks: list[str] = []

        for evidence_id in dict.fromkeys(evidence_ids):
            item, source = self.resolve(
                state, evidence_id
            )

            url = quote(
                source.url,
                safe=":/?=&%+#@!$'()*,-._~;",
            )

            preview = item.text[:240]
            if len(item.text) > 240:
                preview += "…"

            blocks.append(
                f"- **[{item.evidence_id}]** "
                f"{self._plain(source.title)}\n"
                f"  - 来源：<{url}>\n"
                f"  - 取得时间：{item.retrieved_at}；"
                f"材料类型：{item.kind}\n"
                f"  - 片段预览：{self._plain(preview)}"
            )

        return "\n\n".join(blocks) or "无引用证据"