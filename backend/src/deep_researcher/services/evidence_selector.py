"""清理候选、按需提取正文、选择原文片段；模型只决定编号。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urldefrag, urlsplit

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration
from deep_researcher.models import SummaryState

logger = logging.getLogger(__name__)
MAX_DOCUMENT_CHARS = 100_000
MAX_MODEL_CANDIDATES = 12
BOILERPLATE = {"首页", "登录", "注册", "返回顶部", "隐私政策", "privacy policy", "back to top"}


@dataclass(frozen=True)
class EvidenceCandidate:
    """临时片段，start/end 相对于审计中保存的提取文本，不是 HTML 行号。"""

    candidate_id: str
    page_id: str
    url: str
    title: str
    text: str
    kind: Literal["search_snippet", "page_excerpt"]
    start: int
    end: int
    document_hash: str
    truncated: bool
    retrieved_at: str

    def result_row(self) -> dict[str, Any]:
        """供证据登记使用；正文来自程序切片，而非模型回复。"""
        return {
            "url": self.url, "title": self.title, "content": self.text,
            "kind": self.kind, "truncated": self.truncated,
            "retrieved_at": self.retrieved_at,
        }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _url(value: Any) -> str:
    try:
        url = urldefrag(_text(value).strip()).url
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
            return url
    except ValueError:
        pass
    return ""


def _terms(text: str) -> set[str]:
    """英文词与中文相邻双字仅用于排序，不作为相关/不相关的硬判定。"""
    terms = set(re.findall(r"[a-z0-9]{2,}", text.lower()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(part[index:index + 2] for index in range(len(part) - 1))
    return terms


def _score(text: str, terms: set[str]) -> int:
    return len(_terms(text) & terms)


def _spans(text: str, limit: int) -> Iterator[tuple[int, int]]:
    """按空行打包相邻段落；长段优先在句末/换行切分，并保留重叠上下文。"""
    paragraphs = list(re.finditer(r"\S[\s\S]*?(?=\n[ \t]*\n|\Z)", text))
    start: int | None = None
    end = 0
    for paragraph in paragraphs:
        left, right = paragraph.span()
        while right > left and text[right - 1].isspace():
            right -= 1
        if start is not None and right - start <= limit:
            end = right
            continue
        if start is not None:
            yield start, end
            start = None
        if right - left <= limit:
            start, end = left, right
            continue
        # 单个长段/表格超预算时使用相邻窗口，不拼接不连续的句子。
        # 重叠能减少边界损失，但不能保证所有超长表格都保留完整表头。
        while left < right:
            stop = min(left + limit, right)
            if stop < right:
                boundaries = list(re.finditer(r"[。！？!?\n]|\.(?=\s)", text[left:stop]))
                if boundaries and boundaries[-1].end() >= limit // 2:
                    stop = left + boundaries[-1].end()
            yield left, stop
            if stop == right:
                break
            left = max(left + 1, stop - min(160, limit // 5))
    if start is not None:
        yield start, end


class EvidenceSelector:
    def __init__(
        self,
        config: Configuration,
        agent_factory: Callable[[], SimpleAgent] | None,
    ) -> None:
        self.config = config
        self._agent_factory = agent_factory

    def _decide(
        self, context: dict[str, str], phase: str, rows: list[dict[str, str]],
        limit: int, audit: dict[str, Any], notices: list[str],
    ) -> list[str]:
        """每阶段最多一次模型逻辑调用；程序校验编号全集，失败才降级。"""
        if not rows:
            return []
        supplied: list[dict[str, str]] = []
        for row in rows:
            candidate_prompt = json.dumps({
                **context, "phase": phase, "candidates": [*supplied, row],
            }, ensure_ascii=False)
            if len(candidate_prompt) <= self.config.selection_input_chars:
                supplied.append(row)
        # 不把未送入模型的片段记成被模型淘汰。
        step: dict[str, Any] = {
            "phase": phase, "input_ids": [row["id"] for row in supplied],
            "budget_omitted_ids": [row["id"] for row in rows if row not in supplied],
            "mode": "model", "decisions": [],
        }
        audit["selection_steps"].append(step)
        if step["budget_omitted_ids"]:
            notices.append(f"{phase} 筛选输入预算排除 {len(step['budget_omitted_ids'])} 项候选")
        if not supplied:
            notices.append(f"{phase} 没有候选满足输入预算")
            step["mode"] = "empty_budget"
            return []

        prompt = json.dumps({**context, "phase": phase, "candidates": supplied}, ensure_ascii=False)
        step["input_chars"] = len(prompt)
        step["prompt"] = prompt
        agent: SimpleAgent | None = None
        try:
            if self._agent_factory is None:
                raise ValueError("未提供筛选 Agent 工厂")
            agent = self._agent_factory()
            agent.enable_tool_call = False
            response = agent.run(
                input_text=prompt, max_tool_iterations=0,
                max_tokens=min(1800, self.config.llm_max_tokens),
            )
            step["response"] = response
            parsed = json.loads(response)
            if not isinstance(parsed, dict) or set(parsed) != {"decisions"}:
                raise ValueError("筛选输出必须只包含 decisions")
            decisions = parsed["decisions"]
            if not isinstance(decisions, list) or len(decisions) != len(supplied):
                raise ValueError("筛选结果必须覆盖全部输入编号")
            seen: set[str] = set()
            known = {row["id"] for row in supplied}
            for item in decisions:
                if not isinstance(item, dict) or set(item) != {"id", "decision", "reason"}:
                    raise ValueError("筛选项字段无效")
                key = item["id"]
                if not isinstance(key, str) or key not in known or key in seen:
                    raise ValueError("筛选编号未知或重复")
                if item["decision"] not in ("keep", "drop", "uncertain"):
                    raise ValueError("筛选判断无效")
                if not isinstance(item["reason"], str) or not 1 <= len(item["reason"].strip()) <= 300:
                    raise ValueError("筛选理由为空或过长")
                seen.add(key)
            step["decisions"] = decisions
        except Exception as exc:
            step["mode"] = "rule_fallback"
            step["error_type"] = type(exc).__name__
            # 模型故障与“明确判定全部无关”是两种情况；这里只处理前者。
            step["decisions"] = [
                {"id": row["id"], "decision": "uncertain", "reason": "语义筛选未完成，按规则排序保留候选"}
                for row in supplied
            ]
            notices.append(f"{phase} 语义筛选失败，已降级为规则选择，相关性未确认")
            logger.warning("证据筛选降级 · 阶段=%s · 原因=%s", phase, type(exc).__name__,
                           extra={"event": "evidence_selection_fallback", "phase": phase})
        finally:
            if agent is not None:
                agent.clear_history()

        by_id = {item["id"]: item for item in step["decisions"]}
        # 优先 keep，再 uncertain；同等级保持规则排名，避免模型输出顺序产生偏差。
        ordered = [row["id"] for label in ("keep", "uncertain") for row in supplied
                   if by_id[row["id"]]["decision"] == label]
        selected = ordered[:limit]
        step["selected_ids"] = selected
        if any(by_id[key]["decision"] == "uncertain" for key in selected):
            notices.append(f"{phase} 保留了相关性尚不确定的候选，请结合原文判断")
        return selected

    def select(
        self, state: SummaryState, task_id: int, results: list[dict[str, Any]],
        extract: Callable[[list[str]], dict[str, Any]] | None,
    ) -> tuple[list[EvidenceCandidate], dict[str, Any], list[str]]:
        task = state.get_task(task_id)
        context = {
            "research_topic": state.research_topic[:1500],
            "task_title": task.title[:300], "task_intent": task.intent[:1500],
            "query": task.effective_query[:500],
        }
        terms = _terms(" ".join(context.values()))
        notices: list[str] = []
        audit: dict[str, Any] = {
            "schema_version": 1, "run_id": state.run_id, "task_id": task_id,
            "query": task.effective_query, "context": context,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "selection_steps": [], "removed": [], "documents": {}, "candidates": [],
            "selected": [], "config": {
                name: getattr(self.config, name) for name in (
                    "enable_page_extraction", "selection_max_sources", "selection_max_fragments",
                    "selection_per_source", "selection_input_chars", "evidence_max_chars",
                )
            },
        }
        pages: dict[str, dict[str, Any]] = {}
        by_url: dict[str, str] = {}
        for rank, row in enumerate(results, 1):
            url = _url(row.get("url"))
            if not url:
                audit["removed"].append({"rank": rank, "reason": "无效页面地址"})
                continue
            snippet = _text(row.get("content")).strip()
            raw = _text(row.get("raw_content")).strip()
            if not snippet and not raw:
                audit["removed"].append({"rank": rank, "url": url, "reason": "没有可用文本"})
                continue
            if url in by_url:
                old = pages[by_url[url]]
                for field, value in (("snippet", snippet), ("raw_content", raw)):
                    if len(value) > len(old[field]):
                        old[field] = value[:MAX_DOCUMENT_CHARS]
                        old["input_clipped"] = old["input_clipped"] or len(value) > MAX_DOCUMENT_CHARS
                audit["removed"].append({"rank": rank, "url": url, "reason": "重复页面已合并"})
                continue
            key = f"P{rank:04d}"
            by_url[url] = key
            pages[key] = {
                "id": key, "url": url, "title": (_text(row.get("title")) or url)[:500],
                "snippet": snippet[:MAX_DOCUMENT_CHARS], "raw_content": raw[:MAX_DOCUMENT_CHARS],
                "input_clipped": max(len(raw), len(snippet)) > MAX_DOCUMENT_CHARS,
            }
        ranked_pages = sorted(pages.values(), key=lambda page: -_score(page["title"] + " " + page["snippet"], terms))
        audit["search_results"] = [dict(page) for page in pages.values()]
        page_ids = self._decide(context, "pages", [
            {"id": page["id"], "title": page["title"], "url": page["url"],
             "text": (page["snippet"] or page["raw_content"])[:600]}
            for page in ranked_pages
        ], self.config.selection_max_sources, audit, notices)
        logger.info("页面筛选完成 · 候选=%d · 选中=%d", len(pages), len(page_ids),
                    extra={"event": "pages_selected", "selected_page_ids": page_ids})

        wanted = [pages[key]["url"] for key in page_ids if not pages[key]["raw_content"]]
        extraction: dict[str, Any] = {"results": [], "failed_results": []}
        if self.config.enable_page_extraction and wanted:
            try:
                if extract is None:
                    raise ValueError("未提供正文提取工具")
                extraction = extract(wanted)
                if not isinstance(extraction, dict) or not isinstance(extraction.get("results", []), list):
                    raise ValueError("正文提取结果格式无效")
            except Exception as exc:
                extraction = {"results": [], "failed_results": [
                    {"url": url, "error": type(exc).__name__} for url in wanted
                ]}
                logger.warning("正文提取失败 · 使用搜索摘要 · %s", type(exc).__name__)
        for row in extraction.get("results", []):
            if not isinstance(row, dict):
                continue
            url = _url(row.get("url"))
            # 只接收本次请求的页面，供应商返回其他 URL 不会被偷偷登记。
            if url not in wanted:
                continue
            raw = _text(row.get("raw_content")).strip()
            if raw:
                page = pages[by_url[url]]
                page["raw_content"] = raw[:MAX_DOCUMENT_CHARS]
                page["input_clipped"] = len(raw) > MAX_DOCUMENT_CHARS
        audit["extraction"] = {
            "enabled": self.config.enable_page_extraction,
            "requested_urls": wanted if self.config.enable_page_extraction else [],
            "failed_urls": [url for url in wanted if not pages[by_url[url]]["raw_content"]] if self.config.enable_page_extraction else [],
            "used_snippet_urls": [url for url in wanted if not pages[by_url[url]]["raw_content"]],
        }
        if wanted and any(not pages[by_url[url]]["raw_content"] for url in wanted):
            notices.append("部分页面未取得正文，仍以 search_snippet 使用搜索摘要")

        candidates: list[EvidenceCandidate] = []
        seen_text: set[str] = set()
        for key in page_ids:
            page = pages[key]
            kind: Literal["page_excerpt", "search_snippet"] = "page_excerpt" if page["raw_content"] else "search_snippet"
            document = (page["raw_content"] or page["snippet"]).replace("\r\n", "\n").strip()
            digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
            retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            audit["documents"][key] = {
                "url": page["url"], "title": page["title"], "text": document,
                "sha256": digest, "kind": kind, "input_clipped": page["input_clipped"],
                "retrieved_at": retrieved_at,
            }
            if page["input_clipped"]:
                notices.append(f"页面 {key} 超过处理上限，仅处理取得文本的前 {MAX_DOCUMENT_CHARS} 字符")
            for start, end in _spans(document, self.config.evidence_max_chars):
                text = document[start:end]
                fingerprint = " ".join(text.lower().split())
                if fingerprint in BOILERPLATE or fingerprint in seen_text:
                    audit["removed"].append({"page_id": key, "start": start, "end": end, "reason": "重复或固定导航文本"})
                    continue
                seen_text.add(fingerprint)
                candidates.append(EvidenceCandidate(
                    candidate_id=f"C{len(candidates) + 1:04d}", page_id=key,
                    url=page["url"], title=page["title"], text=text, kind=kind,
                    start=start, end=end, document_hash=digest,
                    truncated=bool(page["input_clipped"] or start > 0 or end < len(document)),
                    retrieved_at=retrieved_at,
                ))
        # 每个来源先提供最相关的一段，剩余位置再按词项分数填充。
        # 零分也可能是同义表达，不能直接据此删除。
        ranked = sorted(candidates, key=lambda item: -_score(item.text, terms))
        first: list[EvidenceCandidate] = []
        seen_pages: set[str] = set()
        for candidate in ranked:
            if candidate.page_id not in seen_pages:
                first.append(candidate)
                seen_pages.add(candidate.page_id)
        shortlist = (first + [item for item in ranked if item not in first])[:MAX_MODEL_CANDIDATES]
        audit["candidates"] = [asdict(item) for item in candidates]
        audit["preselection_omitted_ids"] = [item.candidate_id for item in candidates if item not in shortlist]
        if audit["preselection_omitted_ids"]:
            notices.append(f"候选片段超过预算，{len(audit['preselection_omitted_ids'])} 段未进入模型筛选")
        selected_ids = self._decide(context, "fragments", [
            {"id": item.candidate_id, "title": item.title, "url": item.url, "kind": item.kind, "text": item.text}
            for item in shortlist
        ], len(shortlist), audit, notices)
        by_id = {item.candidate_id: item for item in candidates}
        chosen: list[EvidenceCandidate] = []
        counts: Counter[str] = Counter()
        for key in selected_ids:
            item = by_id[key]
            overlaps = any(old.page_id == item.page_id and max(old.start, item.start) < min(old.end, item.end) for old in chosen)
            if overlaps or counts[item.page_id] >= self.config.selection_per_source or len(chosen) >= self.config.selection_max_fragments:
                audit["removed"].append({"candidate_id": key, "reason": "重叠片段或最终数量预算"})
                continue
            chosen.append(item)
            counts[item.page_id] += 1
        audit["selected"] = [{"candidate_id": item.candidate_id} for item in chosen]
        audit["notices"] = list(dict.fromkeys(notices))
        logger.info("片段筛选完成 · 候选=%d · 保留=%d · 正文=%d · 摘要=%d",
                    len(candidates), len(chosen), sum(item.kind == "page_excerpt" for item in chosen),
                    sum(item.kind == "search_snippet" for item in chosen),
                    extra={"event": "evidence_selection_completed", "candidate_count": len(candidates), "selected_count": len(chosen)})
        return chosen, audit, audit["notices"]
