"""离线评测命令：导出研究记录、生成评分表、汇总结果。"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from deep_researcher.config import Configuration
from deep_researcher.graph.graph_researcher import (
    GraphDeepResearcher,
)
from deep_researcher.graph.graph_state import decode_state
from deep_researcher.logging_config import setup_logging
from deep_researcher.models import SummaryState

from evals.checks import (
    inspect_state,
    score_review,
    text_hash,
)


logger = logging.getLogger(__name__)

# review.py 位于 backend/evals/review.py。
BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = BACKEND_DIR / "data" / "langgraph"
DEFAULT_OUTPUT_DIR = BACKEND_DIR / "evals" / "outputs"


@dataclass(frozen=True)
class CandidateRun:
    """可供评测的历史研究。"""

    run_id: str
    topic: str
    updated_at: str


def _utc_now() -> str:
    """返回带时区的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


def _write_json(path: Path, data: Any) -> None:
    """以 UTF-8 写入格式化 JSON。"""
    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    """读取 JSON。"""
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def _sqlite_readonly_uri(path: Path) -> str:
    """
    生成 SQLite 只读 URI。

    as_uri() 会把 Windows 路径转为 file:///C:/... 格式。
    mode=ro 表示只读打开。
    """
    return f"{path.resolve().as_uri()}?mode=ro"


def _backup_sqlite(
    source: Path,
    target: Path,
) -> None:
    """
    将 SQLite 数据库复制到评测目录。

    Connection.backup() 返回 None，
    但会把源数据库页面复制到目标数据库。
    """
    if not source.is_file():
        raise FileNotFoundError(
            f"数据库不存在：{source}"
        )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with (
        sqlite3.connect(
            _sqlite_readonly_uri(source),
            uri=True,
        ) as source_db,
        sqlite3.connect(target) as target_db,
    ):
        source_db.backup(target_db)


def _read_completed_runs(
    data_dir: Path,
) -> list[CandidateRun]:
    """只读取状态为 completed 的研究元数据。"""
    path = data_dir / "runs.sqlite3"

    if not path.is_file():
        raise FileNotFoundError(
            f"运行数据库不存在：{path}"
        )

    with sqlite3.connect(
        _sqlite_readonly_uri(path),
        uri=True,
    ) as db:
        db.row_factory = sqlite3.Row

        rows = db.execute(
            """
            SELECT run_id, data, updated_at
            FROM graph_runs
            WHERE status = 'completed'
            ORDER BY updated_at DESC, run_id DESC
            """
        ).fetchall()

    candidates: list[CandidateRun] = []

    for row in rows:
        data = json.loads(row["data"])

        candidates.append(
            CandidateRun(
                run_id=row["run_id"],
                topic=data["topic"],
                updated_at=row["updated_at"],
            )
        )

    return candidates


def _review_template(
    state: SummaryState,
    checks: dict[str, Any],
) -> dict[str, Any]:
    """创建一份等待人工填写的评分表。"""
    return {
        "schema_version": 1,
        "run_id": state.run_id,
        "report_sha256": checks["report_sha256"],
        "status": "pending",
        "reviewer": "",
        "key_points": [
            {
                "point": "",
                "score": None,
                "reason": "",
            }
        ],
        "evidence_sufficiency": "uncertain",
        "limitations_score": None,
        "limitations_reason": "",
        "claim_inventory_complete": False,
        "no_claims_reason": "",
        "claims": [],
        "notes": "",
    }


def _export_one(
    reader: GraphDeepResearcher,
    candidate: CandidateRun,
    case_dir: Path,
) -> dict[str, Any]:
    """导出一份研究，并生成自动检查结果。"""
    case_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    snapshot = reader.read_run(candidate.run_id)

    if not snapshot.report_ready:
        raise RuntimeError(
            "研究状态不是可交付的完整报告"
        )

    if snapshot.state is None:
        raise RuntimeError(
            "研究状态为空"
        )

    state = snapshot.state
    checks = inspect_state(state)

    _write_json(
        case_dir / "snapshot.json",
        {
            "run": {
                "run_id": candidate.run_id,
                "topic": candidate.topic,
                "updated_at": candidate.updated_at,
                "status": snapshot.info.status,
                # 这是历史运行保存的非敏感配置快照。
                "config": snapshot.info.config,
                "max_tasks": snapshot.info.max_tasks,
            },
            "state": asdict(state),
            "next_nodes": list(snapshot.next_nodes),
            "active_task_id": snapshot.active_task_id,
            "graph_finished": snapshot.graph_finished,
        },
    )

    (case_dir / "report.md").write_text(
        state.structured_report or "",
        encoding="utf-8",
    )

    if isinstance(checks.get("body"), str):
        (case_dir / "body.md").write_text(
            checks["body"],
            encoding="utf-8",
        )

    _write_json(
        case_dir / "evidence.json",
        {
            "sources": {
                key: asdict(value)
                for key, value in state.sources.items()
            },
            "evidence": {
                key: asdict(value)
                for key, value in state.evidence.items()
            },
        },
    )

    _write_json(
        case_dir / "checks.json",
        checks,
    )

    _write_json(
        case_dir / "review.json",
        _review_template(state, checks),
    )

    return {
        "run_id": candidate.run_id,
        "topic": candidate.topic,
        "status": "exported",
        "report_sha256": text_hash(
            state.structured_report or ""
        ),
    }


def export_batch(
    data_dir: Path,
    output_dir: Path,
    run_ids: list[str] | None,
    limit: int,
) -> Path:
    """导出一批历史研究。"""
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1 到 100 之间")

    candidates = _read_completed_runs(data_dir)
    candidate_map = {
        item.run_id: item
        for item in candidates
    }

    if run_ids:
        invalid_ids = [
            run_id
            for run_id in run_ids
            if re.fullmatch(r"[0-9a-f]{32}", run_id)
            is None
        ]

        if invalid_ids:
            raise ValueError(
                f"研究编号格式错误：{invalid_ids}"
            )

        missing_ids = [
            run_id
            for run_id in run_ids
            if run_id not in candidate_map
        ]

        if missing_ids:
            raise ValueError(
                f"找不到已完成研究：{missing_ids}"
            )

        selected = [
            candidate_map[run_id]
            for run_id in run_ids
        ]
    else:
        selected = candidates[:limit]

    if not selected:
        raise RuntimeError(
            "没有可导出的 completed 研究"
        )

    batch_dir = (
        output_dir
        / (
            datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            + "-"
            + uuid4().hex[:8]
        )
    )

    batch_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    database_dir = batch_dir / "database_snapshot"
    database_dir.mkdir()

    # 先复制数据库，再读取复制品。
    # 这样评测过程不会触碰原始 checkpoint。
    _backup_sqlite(
        data_dir / "runs.sqlite3",
        database_dir / "runs.sqlite3",
    )
    _backup_sqlite(
        data_dir / "checkpoints.sqlite3",
        database_dir / "checkpoints.sqlite3",
    )

    reader = GraphDeepResearcher(
        config=Configuration(),
        data_dir=database_dir,
    )

    case_results: list[dict[str, Any]] = []

    for candidate in selected:
        case_dir = batch_dir / "cases" / candidate.run_id

        try:
            result = _export_one(
                reader,
                candidate,
                case_dir,
            )
            case_results.append(result)

            logger.info(
                "🧪 评测样本已导出 · run_id=%s · topic=%s",
                candidate.run_id,
                candidate.topic,
            )
        except Exception as exc:
            case_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            _write_json(
                case_dir / "error.json",
                {
                    "run_id": candidate.run_id,
                    "status": "export_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )

            case_results.append(
                {
                    "run_id": candidate.run_id,
                    "topic": candidate.topic,
                    "status": "export_failed",
                    "error_type": type(exc).__name__,
                }
            )

            logger.exception(
                "❌ 评测样本导出失败 · run_id=%s",
                candidate.run_id,
            )

    manifest = {
        "schema_version": 1,
        "exported_at": _utc_now(),
        "rule_version": 1,
        "source_data_dir": str(data_dir.resolve()),
        "selected_count": len(selected),
        "cases": case_results,
        "note": (
            "本目录是历史运行的评测副本；"
            "exported_at 是导出时间，不是研究生成时间。"
        ),
    }

    _write_json(
        batch_dir / "manifest.json",
        manifest,
    )

    print(f"评测批次已生成：{batch_dir}")
    return batch_dir


def _format_ratio(value: Any) -> str:
    """将比例转为百分比文本。"""
    if value is None:
        return "N/A"

    return f"{float(value) * 100:.1f}%"


def summarize_batch(batch_dir: Path) -> None:
    """读取人工评分表，并生成汇总 JSON 和 Markdown。"""
    manifest = _read_json(
        batch_dir / "manifest.json"
    )

    rows: list[dict[str, Any]] = []

    for item in manifest["cases"]:
        run_id = item["run_id"]

        if item["status"] != "exported":
            rows.append(
                {
                    "run_id": run_id,
                    "status": item["status"],
                }
            )
            continue

        case_dir = batch_dir / "cases" / run_id

        try:
            snapshot = _read_json(
                case_dir / "snapshot.json"
            )
            checks = _read_json(
                case_dir / "checks.json"
            )
            review = _read_json(
                case_dir / "review.json"
            )

            state = decode_state(
                snapshot["state"]
            )

            score = score_review(
                review,
                state,
                checks,
            )

            if score is None:
                rows.append(
                    {
                        "run_id": run_id,
                        "status": "pending",
                        "rule_status": checks[
                            "report_check"
                        ]["status"],
                    }
                )
            else:
                rows.append(
                    {
                        "run_id": run_id,
                        "status": "reviewed",
                        "rule_status": checks[
                            "report_check"
                        ]["status"],
                        **score,
                    }
                )

        except Exception as exc:
            rows.append(
                {
                    "run_id": run_id,
                    "status": "invalid_review",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    counts = {
        "total": len(rows),
        "reviewed": sum(
            row["status"] == "reviewed"
            for row in rows
        ),
        "pending": sum(
            row["status"] == "pending"
            for row in rows
        ),
        "invalid_review": sum(
            row["status"] == "invalid_review"
            for row in rows
        ),
        "export_failed": sum(
            row["status"] == "export_failed"
            for row in rows
        ),
    }

    summary = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "counts": counts,
        "rows": rows,
        "note": (
            "当前汇总只报告逐样本结果，"
            "不把 pending 或 invalid_review 当作 0 分。"
        ),
    }

    _write_json(
        batch_dir / "summary.json",
        summary,
    )

    lines = [
        "# 评测汇总",
        "",
        f"- 样本数：{counts['total']}",
        f"- 已完成人工评分：{counts['reviewed']}",
        f"- 待评分：{counts['pending']}",
        f"- 评分无效：{counts['invalid_review']}",
        f"- 导出失败：{counts['export_failed']}",
        "",
        "| run_id | 状态 | 自动引用检查 | 覆盖率 | 引用覆盖 | 引用支持 | 限制处理 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]

    for row in rows:
        if row["status"] == "reviewed":
            lines.append(
                "| "
                f"{row['run_id'][:8]} | "
                "reviewed | "
                f"{row['rule_status']} | "
                f"{_format_ratio(row['coverage_ratio'])} | "
                f"{_format_ratio(row['citation_coverage'])} | "
                f"{_format_ratio(row['citation_support'])} | "
                f"{row['limitations_score']}/2 |"
            )
        else:
            lines.append(
                "| "
                f"{row['run_id'][:8]} | "
                f"{row['status']} | "
                f"{row.get('rule_status', 'N/A')} | "
                "待评 | 待评 | 待评 | 待评 |"
            )

    (batch_dir / "summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print(f"汇总已生成：{batch_dir / 'summary.md'}")


def list_runs(
    data_dir: Path,
    limit: int,
) -> None:
    """打印最近的 completed 研究。"""
    candidates = _read_completed_runs(data_dir)

    for item in candidates[:limit]:
        print(
            f"{item.run_id} | "
            f"{item.updated_at} | "
            f"{item.topic}"
        )


def build_parser() -> argparse.ArgumentParser:
    """创建命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="deep-researcher 离线评测工具"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    list_parser = subparsers.add_parser(
        "list",
        help="列出已完成研究",
    )
    list_parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=10,
    )

    export_parser = subparsers.add_parser(
        "export",
        help="导出历史研究评测副本",
    )
    export_parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
    )
    export_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    export_parser.add_argument(
        "--limit",
        type=int,
        default=5,
    )
    export_parser.add_argument(
        "--run-id",
        nargs="+",
        help="指定一个或多个 32 位 run_id",
    )

    summary_parser = subparsers.add_parser(
        "summarize",
        help="汇总人工评分",
    )
    summary_parser.add_argument(
        "batch_dir",
        type=Path,
    )

    return parser


def main() -> None:
    """命令行入口。"""
    setup_logging(
        console_level="INFO",
        log_dir=None,
    )

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        list_runs(
            data_dir=args.data_dir,
            limit=args.limit,
        )
        return

    if args.command == "export":
        export_batch(
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            run_ids=args.run_id,
            limit=args.limit,
        )
        return

    if args.command == "summarize":
        summarize_batch(args.batch_dir)
        return

    parser.error("未知命令")


if __name__ == "__main__":
    main()