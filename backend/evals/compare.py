"""同题、同重复编号的配对对照，不把缺失评分当零分。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from uuid import uuid4

from evals.artifacts import write_json
from evals.summarize import display, macro_stats, numeric, summarize_suite

DEFAULT_COMPARISONS_DIR = Path(__file__).resolve().parent / "comparisons"


def compare_suites(baseline_dir: Path, candidate_dir: Path) -> dict[str, Any]:
    baseline = summarize_suite(baseline_dir)
    candidate = summarize_suite(candidate_dir)
    for key in ["dataset_sha256", "split", "mode", "rule_version"]:
        if baseline[key] != candidate[key]:
            raise ValueError(f"不能直接对照：{key} 不一致")
    if (baseline_dir / "rubric.md").read_bytes() != (candidate_dir / "rubric.md").read_bytes():
        raise ValueError("不能直接对照：人工评分规则不同")
    left = {(row["case_id"], row["trial"]): row for row in baseline["rows"]}
    right = {(row["case_id"], row["trial"]): row for row in candidate["rows"]}
    common = sorted(left.keys() & right.keys())
    if not common:
        raise ValueError("两个批次没有可配对的题目和重复编号")

    def metric_value(row: dict[str, Any], metric: str) -> float | None:
        if metric == "execution_completion":
            if row["execution_status"] in {"completed", "failed", "interrupted"}:
                return float(row["execution_status"] == "completed")
            return None
        if row["execution_status"] != "completed":
            return None
        if metric == "overall_rule_pass":
            status = row.get("overall_check", {}).get("status")
            return {"pass": 1.0, "fail": 0.0}.get(status)
        if metric == "research_duration_s":
            return row.get("research_duration_s")
        if metric in {"model_requests", "search_requests", "total_tokens", "report_ttft_s"}:
            return (row.get("telemetry") or {}).get(metric)
        if row["review_status"] != "reviewed":
            return None
        score = row["score"]
        if metric in {"coverage_ratio", "limitations_score"}:
            return score.get(metric)
        for scope in ["complete", "sampled"]:
            suffix = "_" + scope
            if metric.endswith(suffix):
                if score["review_scope"] != scope:
                    return None
                return score.get(metric[:-len(suffix)])
        return None

    metrics = {}
    for name in [
        "execution_completion", "overall_rule_pass", "coverage_ratio", "limitations_score",
        "citation_coverage_complete", "citation_support_complete",
        "citation_coverage_sampled", "citation_support_sampled",
        "research_duration_s", "model_requests", "search_requests", "total_tokens", "report_ttft_s",
    ]:
        pairs = []
        for key in common:
            a, b = metric_value(left[key], name), metric_value(right[key], name)
            if numeric(a) and numeric(b):
                pairs.append({
                    "case_id": key[0], "trial": key[1],
                    "baseline": a, "candidate": b, "delta": b - a,
                })
        metrics[name] = {
            "n_pairs": len(pairs),
            "excluded_pairs": len(common) - len(pairs),
            "n_cases": len({pair["case_id"] for pair in pairs}),
            "baseline_mean": macro_stats(pairs, lambda pair: pair["baseline"])["mean"],
            "candidate_mean": macro_stats(pairs, lambda pair: pair["candidate"])["mean"],
            "delta_mean": macro_stats(pairs, lambda pair: pair["delta"])["mean"],
            "pairs": pairs,
        }
    config_keys = (baseline["config"].keys() | candidate["config"].keys()) - {"notes_workspace"}
    changes = {
        key: {"baseline": baseline["config"].get(key), "candidate": candidate["config"].get(key)}
        for key in sorted(config_keys)
        if baseline["config"].get(key) != candidate["config"].get(key)
    }
    if baseline["max_tasks"] != candidate["max_tasks"]:
        changes["max_tasks"] = {"baseline": baseline["max_tasks"], "candidate": candidate["max_tasks"]}
    return {
        "schema_version": 1,
        "baseline": {"directory": str(baseline_dir.resolve()), "variant": baseline["variant"],
                     "counts": baseline["counts"], "source_sha256": baseline["provenance"]["sha256"]},
        "candidate": {"directory": str(candidate_dir.resolve()), "variant": candidate["variant"],
                      "counts": candidate["counts"], "source_sha256": candidate["provenance"]["sha256"]},
        "dataset_sha256": baseline["dataset_sha256"], "split": baseline["split"], "mode": baseline["mode"],
        "paired_attempts": len(common),
        "only_baseline": [list(key) for key in sorted(left.keys() - right.keys())],
        "only_candidate": [list(key) for key in sorted(right.keys() - left.keys())],
        "config_changes": changes,
        "metrics": metrics,
    }


def markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# 对照：{result['baseline']['variant']} → {result['candidate']['variant']}", "",
        "差值统一为候选版本减基线。比例差值为原始 0～1 数值，乘 100 得到百分点。",
        "每项指标只使用双方都有有效值的同题同次试验；同题先平均，再对题目等权平均。",
        "执行完成率仅配对双方都已结束的试验；效率指标仅配对双方均完成的研究。", "",
        f"可配对 {result['paired_attempts']} 次；仅基线 {len(result['only_baseline'])} 次；"
        f"仅候选 {len(result['only_candidate'])} 次。", "",
        "| 版本 | 计划 | 完成 | 失败 | 中断 | 未结束 | 有效评分 | 待评分 | 无效评分/材料 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for side in ["baseline", "candidate"]:
        counts = result[side]["counts"]
        lines.append(
            f"| {side} | {counts['scheduled']} | {counts['completed']} | {counts['failed']} | "
            f"{counts['interrupted']} | {counts['running'] + counts['not_started']} | "
            f"{counts['reviewed']} | {counts['pending']} | {counts['invalid_review'] + counts['invalid_artifact']} |"
        )
    lines += ["",
        "| 指标 | 有效配对 / 题目 | 排除配对 | 基线 | 候选 | 差值 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, metric in result["metrics"].items():
        lines.append(
            f"| {name} | {metric['n_pairs']} / {metric['n_cases']} | {metric['excluded_pairs']} | "
            f"{display(metric['baseline_mean'])} | {display(metric['candidate_mean'])} | "
            f"{display(metric['delta_mean'])} |"
        )
    lines += ["", "耗时和 token 通常越低越省；调用次数只是诊断指标，少调用不必然更好。",
              "质量分只代表具有有效评审记录的配对；评审者身份见 review.json，必须同时阅读失败数、待评分数和排除数。",
              "在线搜索材料会变化，本对照不固定网页和证据，不提供因果或统计显著性结论。", "",
              "## 配置变化", ""]
    if result["config_changes"]:
        lines += [f"- {key}: {value['baseline']} → {value['candidate']}"
                  for key, value in result["config_changes"].items()]
    else:
        lines.append("已保存的配置字段相同（忽略批次隔离目录）；源码仍可能不同。")
    lines += ["", "完整试验状态、源码指纹和逐对差值见 comparison.json。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="基线与候选版本配对对照；不调用模型")
    parser.add_argument("baseline_dir", type=Path)
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_COMPARISONS_DIR)
    args = parser.parse_args()
    try:
        result = compare_suites(args.baseline_dir, args.candidate_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    destination = args.output_dir.resolve() / uuid4().hex[:12]
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "comparison.json", result)
    (destination / "comparison.md").write_text(markdown(result), encoding="utf-8")
    print(f"对照：{destination / 'comparison.md'}")


if __name__ == "__main__":
    main()
