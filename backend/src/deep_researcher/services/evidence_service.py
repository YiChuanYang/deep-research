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
from deep_researcher.models import (
    CitedText,
    Evidence,
    EvidenceBatch,
    Source,
    SummaryState,
)



logger = logging.getLogger(__name__)

# 捕获约定的 [E编号]，再进一步检查格式和范围。
CITATION = re.compile(r"\[(E[^\]\r\n]*)\]")
ID_PATTERN = re.compile(r"E\d{4,}")

# 只处理完整编号的逗号合写，例如 E0001, E0002 或 E0001，E0002。
# [0-9]{4,} 表示至少四位数字；[ \t]* 允许空格和制表符，但不跨行。
# (?:...) 将规则分组，末尾 + 要求至少再出现一个编号。
# 不展开范围、不补 E 或前导零、不猜测编号。
GROUPED_CITATION = re.compile(
    r"E[0-9]{4,}(?:[ \t]*[,，][ \t]*E[0-9]{4,})+"
)


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
    def collect(
        self,
        state: SummaryState,
        task_id: int,
        results: list[dict[str, Any]],
        *,
        backend: str,
        max_chars: int,
        prefer_full_page: bool,
        query: str | None = None,
    ) -> EvidenceBatch:
        """整理证据，返回新目录，不修改传入的研究状态。"""
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正数")

        task = state.get_task(task_id)
        # 记录实际发送给搜索引擎的查询，而不是仍沿用规划阶段的草案。
        # 可选参数兼容原有直接调用，未来多查询也可逐次传入实际值。
        actual_query = task.effective_query if query is None else query

        # 复制字典容器；后面只向这些新字典添加条目。
        sources = dict(state.sources)
        evidence_records = dict(state.evidence)

        selected: list[str] = []
        previous_count = len(evidence_records)

        for row in results:
            raw_url = row.get("url")
            if not isinstance(raw_url, str):
                continue

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

            if not isinstance(raw, str) or not raw.strip():
                continue

            raw = raw.strip()
            text = raw[:max_chars]
            truncated = len(raw) > max_chars

            source = next(
                (
                    item
                    for item in sources.values()
                    if item.url == url
                ),
                None,
            )

            if source is None:
                source = Source(
                    source_id=self._next_id("S", sources),
                    url=url,
                    title=str(row.get("title") or url),
                )
                sources[source.source_id] = source

            evidence = next(
                (
                    item
                    for item in evidence_records.values()
                    if item.task_id == task_id
                    and item.source_id == source.source_id
                    and item.text == text
                    and item.kind == kind
                    and item.truncated == truncated
                    and item.query == actual_query
                    and item.backend == backend
                ),
                None,
            )

            if evidence is None:
                evidence = Evidence(
                    evidence_id=self._next_id(
                        "E", evidence_records
                    ),
                    source_id=source.source_id,
                    task_id=task_id,
                    text=text,
                    retrieved_at=datetime.now(
                        timezone.utc
                    ).isoformat(timespec="seconds"),
                    query=actual_query,
                    backend=backend,
                    kind=kind,
                    truncated=truncated,
                )
                evidence_records[
                    evidence.evidence_id
                ] = evidence

            if evidence.evidence_id not in selected:
                selected.append(evidence.evidence_id)

        logger.debug(
            "证据已整理 · 可用=%d 新增=%d",
            len(selected),
            len(evidence_records) - previous_count,
            extra={
                "event": "evidence_prepared",
                "evidence_ids": selected,
            },
        )

        return EvidenceBatch(
            sources=sources,
            evidence=evidence_records,
            evidence_ids=selected,
        )

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
                "section": item.section,
                "snapshot_id": item.snapshot_id,
            })

        if not rows:
            return ""

        return json.dumps(
            rows,
            ensure_ascii=False,
            indent=2,
        )

    def prepare_text(
        self,
        state: SummaryState,
        text: str,
        allowed_ids: Iterable[str],
        *,
        document: Literal["summary", "report"] = "summary",
    ) -> CitedText:
        """规范化明确的引用合写，严格校验后返回正文与编号。"""
        # * 后面的 document 必须以关键字传入。
        # 沿用当前 [E编号] 引用协议，此方法不做通用 Markdown 解析。
        if not isinstance(text, str) or not text.strip():
            raise CitationError("待处理正文必须是非空字符串")

        # Iterable 也可能是只能遍历一次的生成器，先固定本次允许范围。
        allowed = tuple(allowed_ids)
        changes: list[dict[str, Any]] = []

        def replace_group(match: re.Match[str]) -> str:
            """为正则匹配到的一处引用生成候选替换文本。"""
            # Match 是匹配结果：group(0) 包含方括号，group(1) 是括号内文字。
            inner = match.group(1)
            # fullmatch 要求整个内容符合规则；不匹配时返回 None。
            if GROUPED_CITATION.fullmatch(inner) is None:
                return match.group(0)

            # split 返回字符串列表，只分隔逗号，不改动编号或顺序。
            ids = [item.strip() for item in re.split(r"[,，]", inner)]
            replacement = "".join(f"[{item}]" for item in ids)
            changes.append({
                # 位置基于输入正文，end 是匹配结束后的下一个字符位置。
                "start": match.start(),
                "end": match.end(),
                "before": match.group(0),
                "after": replacement,
            })
            return replacement

        # sub 接收回调，每个匹配调用一次；返回新字符串，不修改原 text。
        # 这里只生成候选文本，未知编号仍会在下面的严格校验中被拒绝。
        normalized = CITATION.sub(replace_group, text)
        try:
            ids = self.validate(state, normalized, allowed)
        except CitationError:
            if changes:
                logger.warning(
                    "引用规范化未通过校验 · 文档=%s · 候选修复=%d 处",
                    document,
                    len(changes),
                    extra={
                        "event": "citation_normalization_rejected",
                        "document": document,
                        "original_text": text,
                        "candidate_text": normalized,
                        "citation_changes": changes,
                    },
                )
            # 保留原始异常链；校验失败不返回部分结果，不提交业务状态。
            raise

        if changes:
            logger.info(
                "引用格式已规范化 · 文档=%s · 修复=%d 处",
                document,
                len(changes),
                extra={
                    "event": "citations_normalized",
                    "document": document,
                    "repair_count": len(changes),
                    "original_text": text,
                    "normalized_text": normalized,
                    "citation_changes": changes,
                    "citation_ids": ids,
                },
            )
            # 控制台显示简短正文，JsonFormatter 保存 extra 中的审计字段。
            # original_text 是进入本方法的正文，可能已经去除首尾空白和思考标签，
            # 不是供应商原始响应。审计信息保存在日志，不增加业务状态字段。

        # 一起返回处理后的正文和编号，避免校验了新文本却仍保存旧文本。
        return CitedText(text=normalized, citation_ids=tuple(ids))

    def validate(
        self,
        state: SummaryState,
        text: str,
        allowed_ids: Iterable[str],
    ) -> list[str]:
        """输入带有引用的文本，和所支持使用引用的编号列表，校验两点：
                1.正文是否规范
                2.实际引用id是否在所支持引用的列表里"""
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
                + (f"\n  - 正文位置：{self._plain(item.section)}；字符 {item.start}–{item.end}"
                   if item.snapshot_id else "")
            )

        return "\n\n".join(blocks) or "无引用证据"
