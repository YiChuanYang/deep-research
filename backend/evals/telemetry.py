"""从现有结构化日志收集单次研究用量，不解析自然语言日志正文。"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator

from deep_researcher.logging_config import ContextFilter


class RunTelemetry(logging.Handler):
    """逻辑 SDK 调用次数，不包括 SDK 内部透明重试次数。"""

    def __init__(self, run_id: str) -> None:
        super().__init__(level=logging.DEBUG)
        self.run_id = run_id
        self.addFilter(ContextFilter())
        self._guard = Lock()
        self.model_requests = 0
        self.model_responses = 0
        self.search_requests = 0
        self.search_responses = 0
        self.usage_responses = 0
        self.reported_tokens = 0
        self.report_ttft_s: list[float] = []
        self.report_iteration_counts: dict[str, int] = {}
        self.web_rag_counts: dict[str, int] = {}
        self.node_duration_s: dict[str, float] = {}
        self.node_calls: dict[str, int] = {}
        self.phase_duration_s: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "run_id", None) != self.run_id:
            return
        event = getattr(record, "event", None)
        with self._guard:
            if event == "graph_node_completed":
                duration = getattr(record, "duration_s", None)
                node = getattr(record, "node", None)
                phase = getattr(record, "research_stage", None)
                if (type(duration) in {int, float} and math.isfinite(duration) and duration >= 0
                        and isinstance(node, str) and node):
                    self.node_duration_s[node] = self.node_duration_s.get(node, 0.0) + float(duration)
                    self.node_calls[node] = self.node_calls.get(node, 0) + 1
                    if isinstance(phase, str) and phase:
                        self.phase_duration_s[phase] = self.phase_duration_s.get(phase, 0.0) + float(duration)
            if isinstance(event, str) and (event.startswith("web_") or event == "embedding_completed"):
                self.web_rag_counts[event] = self.web_rag_counts.get(event, 0) + 1
            if event in {"report_review_started", "report_review_completed", "report_review_protocol_invalid",
                         "report_review_protocol_repair", "report_review_unavailable", "report_supplement_planned",
                         "report_revision_planned", "report_adopted", "report_gap_rejected",
                         "report_revision_unchanged", "supplement_summary_rejected"}:
                self.report_iteration_counts[event] = self.report_iteration_counts.get(event, 0) + 1
            if event in {"model_request", "model_stream_started"}:
                self.model_requests += 1
            elif event in {"model_response", "model_stream_completed"}:
                self.model_responses += 1
                tokens = getattr(record, "tokens", None)
                if type(tokens) is int and tokens >= 0:
                    self.reported_tokens += tokens
                    self.usage_responses += 1
                ttft = getattr(record, "ttft_s", None)
                if type(ttft) in {int, float} and math.isfinite(ttft) and ttft >= 0:
                    self.report_ttft_s.append(float(ttft))
            elif event in {"search_started", "web_discovery_started"}:
                self.search_requests += 1
            elif event in {"search_completed", "web_discovery_completed"}:
                self.search_responses += 1

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            complete = (
                self.model_requests == self.model_responses == self.usage_responses
            )
            return {
                "model_requests": self.model_requests,
                "model_responses": self.model_responses,
                "search_requests": self.search_requests,
                "search_responses": self.search_responses,
                "usage_responses": self.usage_responses,
                "usage_complete": complete,
                "reported_tokens": self.reported_tokens,
                "total_tokens": self.reported_tokens if complete else None,
                "report_ttft_s": self.report_ttft_s[-1] if self.report_ttft_s else None,
                "cost": None,
                "report_iteration": dict(self.report_iteration_counts),
                "web_rag": dict(self.web_rag_counts),
                "phase_duration_s": {key: round(value, 6) for key, value in self.phase_duration_s.items()},
                "node_duration_s": {key: round(value, 6) for key, value in self.node_duration_s.items()},
                "node_calls": dict(self.node_calls),
            }


@contextmanager
def collect_telemetry(run_id: str) -> Iterator[RunTelemetry]:
    target = logging.getLogger("deep_researcher")
    old_level = target.level
    handler = RunTelemetry(run_id)
    target.setLevel(logging.DEBUG)
    target.addHandler(handler)
    try:
        yield handler
    finally:
        target.removeHandler(handler)
        target.setLevel(old_level)
        handler.close()
