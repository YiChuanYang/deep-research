"""重算批次指标；失败、缺失、未评分分别保留。"""

from __future__ import annotations

import argparse
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, median
from typing import Any, Callable

from evals.artifacts import read_assessment, read_json, write_json
from evals.dataset import load_dataset

EXECUTION_STATUSES = {"not_started", "running", "completed", "failed", "interrupted"}
EXPORT_STATUSES = {"not_started", "running", "completed", "failed", "interrupted"}


def numeric(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def stats(values: list[float]) -> dict[str, Any]:
    """P95 使用位置 (n-1)*0.95 的线性插值；样本数必须同时展示。"""
    values = sorted(value for value in values if numeric(value))
    if not values:
        return dict(n=0, mean=None, median=None, p95=None, min=None, max=None)
    position = (len(values) - 1) * 0.95
    lower, upper = math.floor(position), math.ceil(position)
    p95 = values[lower] + (values[upper] - values[lower]) * (position - lower)
    return dict(n=len(values), mean=fmean(values), median=median(values),
                p95=p95, min=values[0], max=values[-1])


def macro_stats(
    rows: list[dict[str, Any]], getter: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """同题多次先取均值，再让每道题等权；缺失值不填零。"""
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = getter(row)
        if numeric(value):
            grouped[row["case_id"]].append(float(value))
    per_case = {key: fmean(values) for key, values in sorted(grouped.items())}
    return {
        "n_attempts": sum(len(values) for values in grouped.values()),
        "n_cases": len(per_case),
        "mean": fmean(per_case.values()) if per_case else None,
        "per_case": per_case,
    }


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator, "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def stage_duration_stats(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    """对每次研究内已累计的阶段耗时计算跨研究分布。"""
    samples: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        telemetry = row.get("telemetry")
        values = telemetry.get(field) if isinstance(telemetry, dict) else None
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if isinstance(name, str) and numeric(value) and value >= 0:
                samples[name].append(float(value))
    return {name: stats(values) for name, values in sorted(samples.items())}


def safe_attempt_dir(batch: Path, entry: dict[str, Any]) -> Path:
    expected = f"attempts/{entry['case_id']}/trial-{entry['trial']:03d}"
    if entry["path"] != expected:
        raise ValueError("manifest 中的试验路径不符合协议")
    directory = (batch / entry["path"]).resolve()
    if not directory.is_relative_to(batch.resolve() / "attempts"):
        raise ValueError("试验目录超出本批次范围")
    return directory


def load_rows(batch: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("kind") != "fixed_question_suite" or manifest.get("schema_version") != 1:
        raise ValueError("不是受支持的固定题集批次")
    dataset = load_dataset(batch / "dataset.json")
    if dataset.fingerprint() != manifest.get("dataset_sha256"):
        raise ValueError("冻结题集的指纹已变化")
    cases = {case.case_id: case for case in dataset.cases}
    entries = manifest.get("attempts")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest 缺少试验清单")
    seen: set[tuple[str, int]] = set()
    rows = []
    for entry in entries:
        case = cases[entry["case_id"]]
        trial = entry["trial"]
        if case.split != manifest["split"] or type(trial) is not int or trial < 1:
            raise ValueError("试验分组或重复编号不合法")
        pair = (case.case_id, trial)
        if pair in seen or entry["attempt_id"] != f"{case.case_id}-t{trial:03d}":
            raise ValueError("试验编号重复或不一致")
        seen.add(pair)
        directory = safe_attempt_dir(batch, entry)
        row = {
            **entry, "category": case.category,
            "execution_status": "invalid_record", "export_status": "not_started",
            "review_status": "not_available", "research_duration_s": None,
            "telemetry": None, "score": None,
        }
        try:
            record = read_json(directory / "attempt.json")
            if any(record.get(key) != value for key, value in entry.items()):
                raise ValueError("attempt.json 与 manifest 身份不一致")
            if record.get("execution_status") not in EXECUTION_STATUSES:
                raise ValueError("execution_status 无效")
            if record.get("export_status") not in EXPORT_STATUSES:
                raise ValueError("export_status 无效")
            if record["export_status"] != "not_started" and record["execution_status"] != "completed":
                raise ValueError("研究未完成却存在导出状态")
            if record["execution_status"] == "completed" and not record.get("run_id"):
                raise ValueError("完成记录缺少 run_id")
            duration = record.get("research_duration_s")
            if duration is not None and (not numeric(duration) or duration < 0):
                raise ValueError("耗时必须为非负有限数")
            if record.get("telemetry") is not None and not isinstance(record["telemetry"], dict):
                raise ValueError("telemetry 必须为对象或 null")
            row.update(record)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            row["record_error"] = str(exc)
            rows.append(row)
            continue
        if row["export_status"] == "completed":
            try:
                row.update(read_assessment(directory, case, record["run_id"]))
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                row.update(review_status="invalid_artifact", artifact_error=str(exc))
        rows.append(row)
    for case_id in {key[0] for key in seen}:
        trials = {trial for item, trial in seen if item == case_id}
        if trials != set(range(1, manifest["repeats"] + 1)):
            raise ValueError("manifest 的试验清单与 repeats 不一致")
    return rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    executions = Counter(row["execution_status"] for row in rows)
    exports = Counter(row["export_status"] for row in rows)
    reviews = Counter(row["review_status"] for row in rows)
    counts = {
        "scheduled": len(rows),
        "cases": len({row["case_id"] for row in rows}),
        "started": sum(executions[key] for key in {"running", "completed", "failed", "interrupted"}),
        **{key: executions[key] for key in sorted(EXECUTION_STATUSES | {"invalid_record"})},
        "exported": exports["completed"],
        "export_failed": exports["failed"],
        "export_interrupted": exports["interrupted"],
        "export_running": exports["running"],
        **{key: reviews[key] for key in [
            "reviewed", "pending", "invalid_review", "invalid_artifact", "not_available",
        ]},
    }
    rates = {
        "execution_completion": ratio(counts["completed"], counts["started"]),
        "export_completion": ratio(counts["exported"], counts["completed"]),
        "review_completion": ratio(counts["reviewed"], counts["exported"]),
    }
    automatic = {}
    for name, getter in [
        ("report", lambda row: [row["report_check"]] if "report_check" in row else []),
        ("overall", lambda row: [row["overall_check"]] if "overall_check" in row else []),
        ("task", lambda row: row.get("task_checks", [])),
    ]:
        statuses = Counter(check["status"] for row in rows for check in getter(row))
        automatic[name] = {
            "counts": {key: statuses[key] for key in ["pass", "fail", "unverified", "not_applicable"]},
            "pass_rate": ratio(statuses["pass"], statuses["pass"] + statuses["fail"]),
        }
    reviewed = [row for row in rows if row["review_status"] == "reviewed"]
    quality = {
        name: macro_stats(reviewed, lambda row, key=name: row["score"].get(key))
        for name in ["coverage_ratio", "limitations_score"]
    }
    # 抽查和全文逐条核对不能混成同一个引用质量数值。
    citation_quality = {}
    for scope in ["complete", "sampled"]:
        scoped = [row for row in reviewed if row["score"]["review_scope"] == scope]
        citation_quality[scope] = {
            "reviewed_attempts": len(scoped),
            **{name: macro_stats(scoped, lambda row, key=name: row["score"].get(key))
               for name in ["citation_coverage", "citation_support"]},
            "claim_counts": {
                key: sum(row["score"][key] for row in scoped)
                for key in ["audited_claims", "cited_claims", "supported_claims",
                            "partial_claims", "uncertain_claims", "unsupported_claims", "uncited_claims"]
            },
        }
    completed = [row for row in rows if row["execution_status"] == "completed"]
    finished = [row for row in rows if row["execution_status"] in {"completed", "failed", "interrupted"}]
    efficiency = {
        "research_duration_s_completed": stats([row["research_duration_s"] for row in completed]),
        "research_duration_s_finished": stats([row["research_duration_s"] for row in finished]),
    }
    for key in ["model_requests", "model_responses", "search_requests", "search_responses",
                "reported_tokens", "total_tokens", "report_ttft_s"]:
        efficiency[key] = stats([(row.get("telemetry") or {}).get(key) for row in completed])
    efficiency["token_usage_complete"] = ratio(
        sum((row.get("telemetry") or {}).get("usage_complete") is True for row in completed),
        len(completed),
    )
    efficiency["cost"] = None
    stage_durations = {
        "phases": stage_duration_stats(rows, "phase_duration_s"),
        "nodes": stage_duration_stats(rows, "node_duration_s"),
        "rag_steps": stage_duration_stats(rows, "rag_step_duration_s"),
    }
    evidence_counts = Counter(row["score"]["evidence_sufficiency"] for row in reviewed)
    diagnostics = {
        key: stats([row.get("counts", {}).get(key) for row in rows])
        for key in ["planned_tasks", "completed_tasks", "skipped_tasks", "sources", "evidence", "report_citations"]
    }
    iteration_counts: Counter[str] = Counter()
    observed_iterations = 0
    for row in rows:
        values = (row.get("telemetry") or {}).get("report_iteration")
        if isinstance(values, dict):
            observed_iterations += 1
            iteration_counts.update(values)
    return {
        "counts": counts, "rates": rates, "automatic": automatic,
        "quality": quality, "citation_quality": citation_quality,
        "evidence_sufficiency": {
            key: evidence_counts[key] for key in ["enough", "limited", "insufficient", "uncertain"]
        },
        "efficiency": efficiency, "stage_durations": stage_durations,
        "diagnostics": diagnostics,
        "report_iteration": {"observed_researches": observed_iterations,
                             "missing_researches": len(rows) - observed_iterations,
                             "counts": dict(iteration_counts)},
    }


def display(value: Any, percent: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1%}" if percent else f"{value:.3f}"


def markdown(summary: dict[str, Any]) -> str:
    counts = summary["counts"]
    lines = [
        f"# 评测汇总：{summary['variant']}", "",
        f"模式：{summary['mode']}；分组：{summary['split']}；批次状态：{summary['status']}。",
        f"计划 {counts['scheduled']} 次 / {counts['cases']} 题；开始 {counts['started']} 次；"
        f"完成 {counts['completed']} 次；失败 {counts['failed']} 次；中断 {counts['interrupted']} 次；"
        f"仍运行 {counts['running']} 次；未开始 {counts['not_started']} 次。",
        f"导出 {counts['exported']} 次；导出失败 {counts['export_failed']} 次；"
        f"导出中断 {counts['export_interrupted']} 次；仍在导出 {counts['export_running']} 次；"
        f"有效评分 {counts['reviewed']} 次；待评分 {counts['pending']} 次；"
        f"评分无效 {counts['invalid_review']} 次；材料无效 {counts['invalid_artifact']} 次；"
        f"运行记录无效 {counts['invalid_record']} 次。", "",
        "| 指标 | 分子 / 分母 | 值 |", "|---|---:|---:|",
    ]
    for name, value in summary["rates"].items():
        lines.append(f"| {name} | {value['numerator']} / {value['denominator']} | {display(value['value'], True)} |")
    for name, value in summary["automatic"].items():
        rate = value["pass_rate"]
        lines.append(f"| {name}_rule_pass | {rate['numerator']} / {rate['denominator']} | {display(rate['value'], True)} |")
    lines += ["", "| 自动规则 | pass | fail | unverified | not_applicable |",
              "|---|---:|---:|---:|---:|"]
    for name, value in summary["automatic"].items():
        statuses = value["counts"]
        lines.append(f"| {name} | {statuses['pass']} | {statuses['fail']} | {statuses['unverified']} | {statuses['not_applicable']} |")
    lines += ["", "自动规则的 unverified / not_applicable 不进入通过率分母。",
              "质量指标只统计有效评分：每题先平均，再对题目等权平均；缺失不填零。", "",
              "| 质量指标 | 有效试验 / 题目 | 均值 |", "|---|---:|---:|"]
    metrics = dict(summary["quality"])
    for scope, group in summary["citation_quality"].items():
        for key in ["citation_coverage", "citation_support"]:
            metrics[f"{key} ({scope})"] = group[key]
    for name, value in metrics.items():
        lines.append(f"| {name} | {value['n_attempts']} / {value['n_cases']} | {display(value['mean'], name != 'limitations_score')} |")
    lines += ["", "| 效率指标 | 有效次数 | 均值 | 中位数 | P95 |", "|---|---:|---:|---:|---:|"]
    for name, value in summary["efficiency"].items():
        if isinstance(value, dict) and "n" in value:
            lines.append(f"| {name} | {value['n']} | {display(value['mean'])} | {display(value['median'])} | {display(value['p95'])} |")
    lines += [
        "",
        "图阶段耗时按每次研究先累计同阶段节点，再跨研究计算分布；失败和中断研究也计入已完成阶段。",
        "未记录的步骤不补零；研究阶段、图节点和 RAG 内部步骤是嵌套计时，不能相加。",
        "",
        "| 范围 | 阶段 / 节点 | 有记录研究数 | 均值(s) | 中位数(s) | P95(s) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for scope, label in [("phases", "研究阶段"), ("nodes", "图节点"), ("rag_steps", "RAG 步骤")]:
        for name, value in summary["stage_durations"][scope].items():
            lines.append(
                f"| {label} | {name} | {value['n']} | {display(value['mean'])} | "
                f"{display(value['median'])} | {display(value['p95'])} |"
            )
    usage = summary["efficiency"]["token_usage_complete"]
    lines += ["", f"完整 token 用量：{usage['numerator']} / {usage['denominator']}，{display(usage['value'], True)}。",
              "total_tokens 只统计 usage 完整的研究；reported_tokens 是已观察到的部分用量。费用未计算。",
              "complete 表示评审者声明完整枚举结论；sampled 是抽查。引用支持不等于外部事实正确。", "",
              "证据充足性（评审分类，评审者见 review.json）：" + "；".join(f"{key}={value}" for key, value in summary["evidence_sufficiency"].items()) + "。", "",
              "| 诊断数量 | 有效次数 | 均值 |", "|---|---:|---:|"]
    for name, value in summary["diagnostics"].items():
        lines.append(f"| {name} | {value['n']} | {display(value['mean'])} |")
    iteration = summary["report_iteration"]
    lines += ["", f"报告迭代遥测：已记录 {iteration['observed_researches']} 次研究，缺失 {iteration['missing_researches']} 次。",
              "", "| 迭代事件 | 次数 |", "|---|---:|"]
    lines.extend(f"| {key} | {value} |" for key, value in iteration["counts"].items())
    lines += ["", "包含失败研究的已记录事件；模型评审完成不代表质量提升。草稿与补充任务见 report_iterations.json。"]
    lines += ["", "诊断数量较多不自动表示质量较高。", "",
              "| 题目 | 次数 | 执行 | 导出 | 评审状态 | 详情 |", "|---|---:|---|---|---|---|"]
    for row in summary["rows"]:
        lines.append(
            f"| {row['case_id']} | {row['trial']} | {row['execution_status']} | "
            f"{row['export_status']} | {row['review_status']} | [{row['attempt_id']}]({row['path']}/attempt.json) |"
        )
    return "\n".join(lines) + "\n"


def summarize_suite(batch: Path) -> dict[str, Any]:
    batch = batch.resolve()
    manifest = read_json(batch / "manifest.json")
    rows = load_rows(batch, manifest)
    result = {
        "schema_version": 1, "rule_version": 2,
        **{key: manifest[key] for key in [
            "variant", "mode", "split", "status", "dataset_version", "dataset_sha256",
            "config", "max_tasks", "repeats", "provenance",
        ]},
        **aggregate(rows), "rows": rows,
    }
    write_json(batch / "summary.json", result)
    (batch / "summary.md").write_text(markdown(result), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总固定问题集评测；不调用模型")
    parser.add_argument("batch_dir", type=Path)
    args = parser.parse_args()
    try:
        result = summarize_suite(args.batch_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(f"汇总：{args.batch_dir / 'summary.md'}")
    print(f"有效评分：{result['counts']['reviewed']} / {result['counts']['exported']}")


if __name__ == "__main__":
    main()
