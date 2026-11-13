"""固定题集的逐题/批量执行入口；每次试验使用新的研究编号。"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from deep_researcher.config import Configuration
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.logging_config import log_context, log_exception_once, setup_logging
from evals.artifacts import export_attempt, read_json, write_json
from evals.dataset import DEFAULT_DATASET, Split, load_dataset, select_cases
from evals.telemetry import collect_telemetry

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = BACKEND_DIR / "evals" / "runs"
logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_code(directory: Path) -> dict[str, Any]:
    """只复制源码、评测脚本和依赖声明，排除凭证与运行数据。"""
    files = sorted(
        list((BACKEND_DIR / "src").rglob("*.py"))
        + list((BACKEND_DIR / "evals").glob("*.py"))
        + [BACKEND_DIR / "pyproject.toml", BACKEND_DIR / "uv.lock"]
    )
    digest = hashlib.sha256()
    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(BACKEND_DIR)
        content = path.read_bytes()
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        name = relative.as_posix()
        hashes[name] = hashlib.sha256(content).hexdigest()
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=BACKEND_DIR,
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"sha256": digest.hexdigest(), "git_commit": commit, "files": hashes}


def run_suite(
    *,
    variant: str,
    dataset_path: Path = DEFAULT_DATASET,
    split: Split = "dev",
    case_ids: list[str] | None = None,
    repeats: int = 1,
    max_tasks: int = 5,
    output_dir: Path = DEFAULT_RUNS_DIR,
    config: Configuration | None = None,
    researcher_factory: Callable[..., GraphDeepResearcher] = GraphDeepResearcher,
    mode: str = "live",
) -> Path:
    """串行执行；普通失败继续下一题，中断保留未开始的试验。"""
    if not variant.strip() or len(variant) > 120:
        raise ValueError("variant 必须为 1～120 字符")
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats 必须为 1～10")
    if type(max_tasks) is not int or not 1 <= max_tasks <= 20:
        raise ValueError("max_tasks 必须为 1～20")
    if mode not in {"live", "offline_test"}:
        raise ValueError("未知运行模式")

    dataset = load_dataset(dataset_path)
    cases = select_cases(dataset, split, case_ids)
    batch = output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    base = config if config is not None else Configuration.from_env()
    effective = Configuration.model_validate({
        **base.model_dump(), "notes_workspace": str(batch / "notes"),
    })
    effective.require_llm()
    effective.require_search()

    batch.mkdir(parents=True, exist_ok=False)
    write_json(batch / "dataset.json", dataset.model_dump(mode="json"))
    provenance = snapshot_code(batch / "source_snapshot")
    # 保存本批使用的评分说明；评分前可以核对当时的口径。
    shutil.copy2(BACKEND_DIR / "evals" / "rubric.md", batch / "rubric.md")
    attempts = [
        {
            "attempt_id": f"{case.case_id}-t{trial:03d}",
            "case_id": case.case_id,
            "trial": trial,
            "path": f"attempts/{case.case_id}/trial-{trial:03d}",
        }
        for trial in range(1, repeats + 1)
        for case in cases
    ]
    manifest = {
        "schema_version": 1, "kind": "fixed_question_suite",
        "variant": variant.strip(), "mode": mode, "split": split,
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset.fingerprint(), "provenance": provenance,
        "config": effective.snapshot(), "max_tasks": max_tasks,
        "repeats": repeats, "created_at": utc_now(), "status": "running",
        "attempts": attempts,
    }
    for entry in attempts:
        directory = batch / entry["path"]
        directory.mkdir(parents=True)
        write_json(directory / "attempt.json", {
            **entry, "execution_status": "not_started",
            "export_status": "not_started", "run_id": None,
            "research_duration_s": None, "telemetry": None,
        })
    write_json(batch / "manifest.json", manifest)
    setup_logging(log_dir=batch / "logs")
    print(f"评测目录：{batch}", flush=True)

    case_map = {case.case_id: case for case in cases}
    interrupted = False
    for index, entry in enumerate(attempts, 1):
        directory = batch / entry["path"]
        record_path = directory / "attempt.json"
        record = read_json(record_path)
        case = case_map[entry["case_id"]]
        record.update(execution_status="running", started_at=utc_now())
        write_json(record_path, record)
        print(f"[{index}/{len(attempts)}] {case.case_id}，第 {entry['trial']} 次", flush=True)
        researcher = None
        tracker = None
        snapshot = None
        started = perf_counter()
        try:
            researcher = researcher_factory(config=effective, data_dir=batch / "data")
            # 新建编号后立即记录；这里的 resume 只执行刚创建的研究。
            record["run_id"] = researcher.create_run(case.topic, max_tasks)
            write_json(record_path, record)
            with (
                log_context(run_id=record["run_id"], stage="评测执行"),
                collect_telemetry(record["run_id"]) as tracker,
            ):
                researcher.resume(record["run_id"])
                snapshot = researcher.read_run(record["run_id"])
                if not snapshot.report_ready:
                    raise RuntimeError("研究返回但最终报告尚未就绪")
            record["execution_status"] = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            record.update(
                execution_status="interrupted" if interrupted else "failed",
                execution_error_type=type(exc).__name__,
            )
            with log_context(run_id=record["run_id"], stage="评测执行"):
                record["error_id"] = log_exception_once(logger, "评测执行未完成", error=exc)
        finally:
            record["research_duration_s"] = round(perf_counter() - started, 6)
            record["finished_at"] = utc_now()
            record["telemetry"] = tracker.snapshot() if tracker is not None else None
            write_json(record_path, record)

        if record["execution_status"] == "completed":
            record["export_status"] = "running"
            write_json(record_path, record)
            try:
                assert snapshot is not None
                export_attempt(snapshot, case, directory)
                record["export_status"] = "completed"
            except (Exception, KeyboardInterrupt) as exc:
                interrupted = isinstance(exc, KeyboardInterrupt)
                record.update(
                    export_status="interrupted" if interrupted else "failed",
                    export_error_type=type(exc).__name__,
                )
                with log_context(run_id=record["run_id"], stage="评测导出"):
                    record["export_error_id"] = log_exception_once(
                        logger, "研究完成但评测导出未完成", error=exc,
                    )
            finally:
                write_json(record_path, record)
        if interrupted:
            break

    manifest.update(
        status="interrupted" if interrupted else "completed",
        finished_at=utc_now(),
    )
    write_json(batch / "manifest.json", manifest)
    from evals.summarize import summarize_suite
    summarize_suite(batch)
    return batch


def main() -> None:
    parser = argparse.ArgumentParser(description="固定问题集逐题/批量评测")
    parser.add_argument("--variant", required=True, help="版本标签；不自动切换源码")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument("--case-id", nargs="+")
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--max-tasks", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        dataset = load_dataset(args.dataset)
        cases = select_cases(dataset, args.split, args.case_id)
        if not args.variant.strip():
            raise ValueError("variant 不能为空")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(f"版本：{args.variant}；分组：{args.split}；试验数：{len(cases) * args.repeats}")
        print(f"题集指纹：{dataset.fingerprint()}")
        for case in cases:
            print(f"{case.case_id} [{case.category}] {case.topic}")
        print("预览完成；未读取凭证、调用模型或创建运行目录。")
        return
    try:
        batch = run_suite(
            variant=args.variant, dataset_path=args.dataset, split=args.split,
            case_ids=args.case_id, repeats=args.repeats, max_tasks=args.max_tasks,
            output_dir=args.output_dir,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    summary = read_json(batch / "summary.json")
    print(f"汇总：{batch / 'summary.md'}")
    if summary["status"] == "interrupted":
        raise SystemExit(130)
    if summary["counts"]["failed"] or summary["counts"]["export_failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
