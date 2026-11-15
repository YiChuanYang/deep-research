"""真实报告迭代演示；诊断文件保留草稿、评审、补充任务与模型用量。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from deep_researcher.config import Configuration
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.logging_config import run_cli, setup_logging
from evals.checks import inspect_state
from evals.telemetry import collect_telemetry


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="真实模型和搜索接口的报告反思演示")
    parser.add_argument(
        "--topic",
        default="比较 BM25 与向量检索在企业知识库中的适用场景、准确性评测和部署成本；说明证据不足之处。",
    )
    parser.add_argument("--max-tasks", type=int, default=2)
    args = parser.parse_args()
    directory = Path("data/diagnostics") / (
        "report-iteration-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid4().hex[:6]
    )
    directory.mkdir(parents=True, exist_ok=False)
    setup_logging(log_dir=directory / "logs", color=False)
    researcher = GraphDeepResearcher(
        Configuration.from_env({"enable_report_reflection": True})
    )
    run_id = researcher.create_run(args.topic, max_tasks=args.max_tasks)
    print(f"研究编号：{run_id}\n诊断目录：{directory.resolve()}", flush=True)
    with collect_telemetry(run_id) as tracker:
        try:
            researcher.resume(run_id)
        finally:
            snapshot = researcher.read_run(run_id)
            write_json(
                directory / "snapshot.json",
                {
                    "run_id": run_id,
                    "status": snapshot.info.status,
                    "next_nodes": snapshot.next_nodes,
                    "report_ready": snapshot.report_ready,
                    "state": asdict(snapshot.state) if snapshot.state else None,
                },
            )
            write_json(directory / "telemetry.json", tracker.snapshot())
            if snapshot.report_iteration:
                write_json(
                    directory / "iterations.json",
                    snapshot.report_iteration.model_dump(mode="json"),
                )
            if snapshot.report_ready and snapshot.state:
                (directory / "report.md").write_text(
                    snapshot.state.structured_report or "", encoding="utf-8"
                )
                write_json(directory / "checks.json", inspect_state(snapshot.state))
            print(
                f"最终状态：{snapshot.info.status}；正式报告可用：{snapshot.report_ready}",
                flush=True,
            )
            if snapshot.report_iteration:
                cycle = snapshot.report_iteration
                print(
                    f"草稿版本：{len(cycle.drafts)}；补充任务：{len(cycle.supplements)}；交付状态：{cycle.status}",
                    flush=True,
                )


if __name__ == "__main__":
    run_cli(main)
