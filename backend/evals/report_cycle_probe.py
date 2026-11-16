"""固定初始报告和材料，对照关闭/开启反思；补搜仍调用真实搜索接口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from deep_researcher.config import Configuration
from deep_researcher.graph import report_cycle
from deep_researcher.graph.graph_nodes import ResearchNodes
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.graph.graph_state import (
    ResearchGraphState,
    decode_state,
    encode_state,
)
from deep_researcher.logging_config import setup_logging
from deep_researcher.models import SummaryState
from deep_researcher.report_iteration import ReportIteration
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.services.report_format import comparable_report
from evals.artifacts import write_json
from evals.checks import extract_report_body, inspect_state
from evals.telemetry import collect_telemetry

logger = logging.getLogger(__name__)


def read_input(path: Path) -> tuple[SummaryState, str]:
    """接受回归 fixture 或评测 snapshot，不读取或恢复原运行数据库。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if "initial_state" in value:
        state = decode_state(value["initial_state"])
        body = value["drafts"][0]
    else:
        state = decode_state(value["state"])
        body, error = extract_report_body(state)
        if body is None:
            raise ValueError(error)
    allowed = [
        key
        for task in state.todo_items
        if task.status == "completed"
        for key in task.citation_ids
    ]
    EvidenceService().validate(state, body, allowed)
    state.structured_report = None
    state.report_citation_ids = []
    state.report_limitations = []
    state.report_note_id = state.report_note_path = None
    for task in state.todo_items:
        task.note_id = task.note_path = None
    return state, body


def run_pair(source: Path, directory: Path, config: Configuration) -> dict[str, Any]:
    """A 为冻结初稿；B 从同一初稿进入真实反思图，只衡量后续增量。"""
    state, body = read_input(source)
    directory.mkdir(parents=True, exist_ok=False)
    frozen = encode_state(state)
    ids = EvidenceService().validate(
        state,
        body,
        [
            key
            for task in state.todo_items
            if task.status == "completed"
            for key in task.citation_ids
        ],
    )
    (directory / "baseline.md").write_text(body, encoding="utf-8")
    write_json(directory / "input.json", {"state": frozen, "body": body})

    class ReplayNodes(ResearchNodes):
        def plan(
            self, target: SummaryState, graph_state: ResearchGraphState
        ) -> dict[str, Any]:
            # decode 每次创建独立对象，图中的更新不会污染对照输入。
            copied = decode_state(frozen)
            target.todo_items, target.sources, target.evidence = (
                copied.todo_items,
                copied.sources,
                copied.evidence,
            )
            return report_cycle.update(
                ReportIteration(
                    initial_task_ids=[
                        task.id
                        for task in copied.todo_items
                        if task.status == "completed"
                    ]
                )
            )

        def report(
            self, target: SummaryState, graph_state: ResearchGraphState
        ) -> dict[str, Any]:
            cycle = report_cycle.iteration(graph_state)
            if not cycle.drafts:
                logger.info("复用冻结初稿 · 只比较后续反思增量")
                report_cycle.commit_draft(cycle, body, ids)
                return report_cycle.update(cycle)
            return super().report(target, graph_state)

    effective = config.model_copy(
        update={"enable_report_reflection": True, "enable_notes": False}
    )
    researcher = GraphDeepResearcher(
        effective, data_dir=directory / "data", nodes_factory=ReplayNodes
    )
    run_id = researcher.create_run(state.research_topic)
    record: dict[str, Any] = {
        "mode": "frozen_report_ab",
        "source": str(source.resolve()),
        "run_id": run_id,
        "topic": state.research_topic,
        "input_sha256": hashlib.sha256(
            json.dumps(
                {"state": frozen, "body": body}, sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest(),
        "config": effective.snapshot(),
        "baseline_incremental_model_requests": 0,
        "quality_score": None,
        "note": "只比较冻结报告之后的增量；不是全链路质量评分或重复试验",
    }
    write_json(directory / "result.json", record)
    print(f"运行：{run_id} · {state.research_topic}", flush=True)
    started = perf_counter()
    with collect_telemetry(run_id) as tracker:
        try:
            researcher.resume(run_id)
        except Exception as exc:
            record["error"] = str(exc)
            record["error_type"] = type(exc).__name__
        finally:
            record["incremental_duration_s"] = round(perf_counter() - started, 3)
            record["telemetry"] = tracker.snapshot()
            snapshot = researcher.read_run(run_id)
            cycle = snapshot.report_iteration
            record["report_ready"] = snapshot.report_ready
            if snapshot.state:
                write_json(directory / "state.json", asdict(snapshot.state))
            if cycle:
                write_json(directory / "iterations.json", cycle.model_dump(mode="json"))
                last = cycle.drafts[-1] if cycle.drafts else None
                record.update(
                    status=cycle.status,
                    stop_reason=cycle.stop_reason,
                    drafts=len(cycle.drafts),
                    supplements=len(cycle.supplements),
                )
                if last:
                    record["body_changed"] = comparable_report(
                        last.text
                    ) != comparable_report(body)
                    record["added_citation_ids"] = sorted(
                        set(last.citation_ids) - set(ids)
                    )
                    (directory / "last_draft.md").write_text(
                        last.text, encoding="utf-8"
                    )
            if snapshot.report_ready and snapshot.state:
                (directory / "report.md").write_text(
                    snapshot.state.structured_report or "", encoding="utf-8"
                )
                write_json(directory / "checks.json", inspect_state(snapshot.state))
            write_json(directory / "result.json", record)
    print(
        f"完成：{record.get('status')} · 新版={record.get('body_changed')} · 补充={record.get('supplements')}",
        flush=True,
    )
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs", type=Path, nargs="+", help="回归 fixture 或历史评测 snapshot.json"
    )
    parser.add_argument("--max-revisions", type=int, choices=range(4), default=2)
    args = parser.parse_args()
    config = Configuration.from_env(
        {"report_max_revisions": args.max_revisions, "report_max_supplement_tasks": 2}
    )
    config.require_llm()
    directory = Path("data/diagnostics") / (
        "report-ab-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid4().hex[:6]
    )
    directory.mkdir(parents=True, exist_ok=False)
    setup_logging(log_dir=directory / "logs", color=False)
    print(f"对照目录：{directory.resolve()}", flush=True)
    results = []
    for index, path in enumerate(args.inputs, 1):
        results.append(run_pair(path, directory / f"case-{index:02d}", config))
        write_json(directory / "summary.json", results)


if __name__ == "__main__":
    main()
