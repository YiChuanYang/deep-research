import json
from pathlib import Path

import numpy as np

from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration
from deep_researcher.llm.llm import MyLLM
from deep_researcher.models import SummaryState
from deep_researcher.prompts import todo_planner_system_prompt
from deep_researcher.services.planner import PlanningService
from evals.nodes.planner.evaluation import (
    caculate_dimension_cover_rate,
    calculate_task_redundancy_rate,
    calculate_task_semantic_match,
    close_embedding_client,
)


def call_planner(topic: str) -> list[str]:
    """
    调用项目真实的规划服务，输入研究主题，返回任务标题列表
    完全复现图节点里的规划调用逻辑
    """
    # 构造初始状态
    state = SummaryState(run_id="eval_test", research_topic=topic)

    # 构造规划 Agent（和 graph_nodes 中保持一致）
    cfg = Configuration.from_env()
    llm = MyLLM(config=cfg)
    agent = ToolAwareAgent(
        name="Eval_Planner",
        llm=llm,
        config=cfg,
        sys_prompt=todo_planner_system_prompt,
    )

    try:
        # 执行规划
        tasks = PlanningService(agent).plan_todo_list(state)
    finally:
        agent.clear_history()
        llm.close()

    # 提取任务标题
    return [task.title for task in tasks]


def evaluate_single_sample(sample: dict) -> dict:
    """
    评测单条样本，计算所有指标
    返回：该样本的完整评测结果
    """
    sample_id = sample["sample_id"]
    topic = sample["topic"]
    expected_dims = sample["expected_dimensions"]
    gold_task_titles = [t["title"] for t in sample["gold_tasks"]]

    # ----- 调用规划器生成任务 -----
    try:
        gen_tasks = call_planner(topic)
        print(f"生成{len(gen_tasks)}项子任务")
    except Exception as e:
        # 调用失败，标记错误，不中断整体评测
        print("任务生成失败")
        return {
            "sample_id": sample_id,
            "topic": topic,
            "success": False,
            "error_msg": str(e),
        }

    # ----- 计算各项指标 -----
    # 1. 维度覆盖率（核心，全面性）
    cover_rate = caculate_dimension_cover_rate(expected_dims, gen_tasks, 0.6)

    # 2. 语义匹配度（拆分逻辑合理性）
    match_result = calculate_task_semantic_match(gold_task_titles, gen_tasks)

    # 3. 任务冗余率
    redundancy_rate = calculate_task_redundancy_rate(gen_tasks)

    # 4. 任务数量偏差率（直接计算，不单独写函数）
    gen_count = len(gen_tasks)
    gold_count = len(gold_task_titles)
    count_deviation = (
        abs(gen_count - gold_count) / gold_count if gold_count > 0 else 0.0
    )

    # ----- 综合得分（权重：覆盖50% + 匹配30% + 低冗余20%） -----
    total_score = (
        cover_rate * 0.5
        + match_result["avg_similarity"] * 0.3
        + (1 - min(redundancy_rate, 1.0)) * 0.2
    )

    return {
        "sample_id": sample_id,
        "topic": topic,
        "success": True,
        "gen_task_count": gen_count,
        "gold_task_count": gold_count,
        # 核心指标
        "dimension_cover_rate": round(cover_rate, 4),
        "semantic_match_score": match_result["avg_similarity"],
        "redundancy_rate": redundancy_rate,
        "count_deviation": round(count_deviation, 4),
        "total_score": round(total_score, 4),
        # 辅助排查信息
        "unmatched_gold": match_result["unmatched_gold"],
        "unmatched_gen": match_result["unmatched_gen"],
        "match_pairs": match_result["match_pairs"],
    }


def run(dataset_path: str | Path | None = None) -> list[dict]:
    """
    运行完整评测流水线
    默认读取同目录下的 input.json
    """
    # 默认路径：当前脚本目录下的 input.json
    if dataset_path is None:
        dataset_path = Path(__file__).parent / "input.json"

    # ---------- 1. 加载评测数据集 ----------
    if not Path(dataset_path).exists():
        raise FileNotFoundError(f"评测数据集不存在：{dataset_path}")

    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    total = len(dataset)
    print(f"✅ 加载评测集完成，共 {total} 条样本")
    print("=" * 90)

    # ---------- 2. 逐条执行评测 ----------
    all_results = []
    for idx, sample in enumerate(dataset, 1):
        print(
            f"[{idx:2d}/{total}] 评测中：{sample['sample_id']} - {sample['topic'][:45]}..."
        )

        result = evaluate_single_sample(sample)
        all_results.append(result)

        # 实时打印单条结果
        if result["success"]:
            print(
                f"      ✓ 覆盖率={result['dimension_cover_rate']:.2%}  "
                f"匹配度={result['semantic_match_score']:.4f}  "
                f"冗余率={result['redundancy_rate']:.2%}  "
                f"综合分={result['total_score']:.4f}"
            )
        else:
            print(f"      ✗ 调用失败：{result['error_msg'][:60]}")

    # ---------- 3. 统计整体汇总指标 ----------
    success_list = [r for r in all_results if r["success"]]
    failed_list = [r for r in all_results if not r["success"]]

    if not success_list:
        print("\n❌ 全部样本调用失败，无有效统计结果")
        return all_results

    # 计算各项平均值
    avg_cover = np.mean([r["dimension_cover_rate"] for r in success_list])
    avg_match = np.mean([r["semantic_match_score"] for r in success_list])
    avg_redundancy = np.mean([r["redundancy_rate"] for r in success_list])
    avg_count_dev = np.mean([r["count_deviation"] for r in success_list])
    avg_total = np.mean([r["total_score"] for r in success_list])

    # 找出最差3条 bad case（按综合分升序）
    bad_cases = sorted(success_list, key=lambda x: x["total_score"])[:3]

    # ---------- 4. 打印汇总报告 ----------
    print("\n" + "=" * 90)
    print("📊 规划节点评测汇总报告")
    print(
        f"样本总数：{total}  |  成功：{len(success_list)}  |  失败：{len(failed_list)}"
    )
    print("-" * 90)
    print(f"平均维度覆盖率：   {avg_cover:.2%}      （核心：全面性）")
    print(f"平均语义匹配度：   {avg_match:.4f}        （拆分逻辑合理性）")
    print(f"平均任务冗余率：   {avg_redundancy:.2%}    （无效重复程度）")
    print(f"平均数量偏差率：   {avg_count_dev:.2%}    （粒度偏差）")
    print(f"平均综合得分：     {avg_total:.4f}")
    print("-" * 90)
    print("⚠️  Bad Case Top3（综合分最低）：")
    for i, case in enumerate(bad_cases, 1):
        print(
            f"  {i}. {case['sample_id']} - {case['topic']}\n"
            f"     覆盖率={case['dimension_cover_rate']:.2%}  "
            f"匹配度={case['semantic_match_score']:.4f}  "
            f"综合分={case['total_score']:.4f}"
        )

    if failed_list:
        print("-" * 90)
        print(f"❌ 失败样本列表（共{len(failed_list)}条）：")
        for case in failed_list:
            print(f"  - {case['sample_id']}: {case['error_msg'][:60]}")

    print("=" * 90)

    return all_results


if __name__ == "__main__":
    try:
        run()
    finally:
        close_embedding_client()
