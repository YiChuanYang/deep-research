from deep_researcher.services.planner import PlanningService
from deep_researcher.rag.embeddings import EmbeddingClient
from deep_researcher.config import Configuration
import numpy as np
from scipy.optimize import linear_sum_assignment

cfg = Configuration.from_env()
client = EmbeddingClient(cfg)


def caculate_dimension_cover_rate(expected_points:list[str],actual_titles:list[str],threshold:float=0.7)->float:
    """
    计算预期维度覆盖率
    算法逻辑：对每一条预期要点，计算其与所有实际要点的语义余弦相似度，取最高值；
    若最高相似度 ≥ 阈值，则视为该预期要点被命中；最终覆盖率 = 命中要点数 / 预期要点总数。

    Args:
        expected_points: 预期要点文本列表
        actual_titles: 实际要点文本列表
        threshold: 相似度命中阈值，取值范围 0~1，默认 0.7

    Returns:
        float: 预期维度覆盖率，取值范围 0~1
        """

    if not expected_points or not actual_titles:
        return 0.0
    #转换成向量
    expected_vectors = client.embed(expected_points,timeout=60.0)
    actual_vectors = client.embed(actual_titles,timeout=60.0)
    #换成numpy
    expected_matrix = np.array(expected_vectors, dtype=np.float32)
    actual_matrix = np.array(actual_vectors, dtype=np.float32)

    # 归一化后向量点积 = 余弦相似度，同时避免零向量除零错误
    exp_norm = np.linalg.norm(expected_matrix, axis=1, keepdims=True)
    exp_norm[exp_norm == 0] = 1e-8
    expected_normalized = expected_matrix / exp_norm

    act_norm = np.linalg.norm(actual_matrix, axis=1, keepdims=True)
    act_norm[act_norm == 0] = 1e-8
    actual_normalized = actual_matrix / act_norm

    # 相似度矩阵形状：(预期要点数, 实际要点数)
    similarity_matrix = np.dot(expected_normalized, actual_normalized.T)

    # 每个预期要点对应的最大相似度
    max_similarities = np.max(similarity_matrix, axis=1)
    # 统计命中数量
    hit_count = np.sum(max_similarities >= threshold)
    # 计算最终覆盖率
    cover_rate = float(hit_count) / len(expected_points)

    return cover_rate


def calculate_task_semantic_match(
    gold_tasks: list[str],
    gen_tasks: list[str],
) -> dict:
    """
    计算任务语义匹配度（进阶版：匈牙利算法一对一最优匹配）
    算法逻辑：
        1. 分别对黄金标准任务、生成任务做向量化；
        2. 归一化后通过矩阵点积计算两两余弦相似度，得到相似度矩阵；
        3. 将相似度转为代价矩阵，用匈牙利算法求解二分图最优一对一匹配；
        4. 基于匹配结果计算平均匹配度、匹配数、未匹配数等指标。

    Args:
        gold_tasks: 黄金标准任务标题列表
        gen_tasks: 实际生成的任务标题列表

    Returns:
        dict: 匹配结果集合，包含：
            - avg_similarity: 平均语义匹配度（0~1），核心指标
            - match_count: 成功匹配的任务对数
            - unmatched_gold: 未匹配到的黄金任务数
            - unmatched_gen: 未匹配到的生成任务数
            - match_pairs: 匹配详情列表，便于排查 bad case
    """
    # ========== 边界处理：任一列表为空直接返回 ==========
    if not gold_tasks or not gen_tasks:
        return {
            "avg_similarity": 0.0,
            "match_count": 0,
            "unmatched_gold": len(gold_tasks),
            "unmatched_gen": len(gen_tasks),
            "match_pairs": []
        }

    # ========== 1. 批量向量化（和你覆盖率函数完全一致） ==========
    gold_vectors = client.embed(gold_tasks, timeout=60.0)
    gen_vectors = client.embed(gen_tasks, timeout=60.0)

    # 转 NumPy 浮点矩阵
    gold_matrix = np.array(gold_vectors, dtype=np.float32)
    gen_matrix = np.array(gen_vectors, dtype=np.float32)

    # ========== 2. 向量归一化 + 批量计算余弦相似度 ==========
    # 黄金任务向量归一化（处理零向量，避免除零）
    gold_norm = np.linalg.norm(gold_matrix, axis=1, keepdims=True)
    gold_norm[gold_norm == 0] = 1e-8
    gold_normalized = gold_matrix / gold_norm

    # 生成任务向量归一化
    gen_norm = np.linalg.norm(gen_matrix, axis=1, keepdims=True)
    gen_norm[gen_norm == 0] = 1e-8
    gen_normalized = gen_matrix / gen_norm

    # 相似度矩阵形状：(黄金任务数, 生成任务数)
    # 第 i 行第 j 列 = 第 i 个黄金任务 与 第 j 个生成任务 的余弦相似度
    similarity_matrix = np.dot(gold_normalized, gen_normalized.T)

    # ========== 3. 匈牙利算法：求解一对一最优匹配 ==========
    # 匈牙利算法求「最小代价」，我们要「最大相似度」，所以代价 = 1 - 相似度
    cost_matrix = 1 - similarity_matrix

    # row_ind: 匹配上的黄金任务索引数组
    # col_ind: 对应匹配到的生成任务索引数组，两者一一对应
    row_ind, col_ind = linear_sum_assignment(cost_matrix)

    # ========== 4. 统计匹配结果 ==========
    match_count = len(row_ind)
    # 所有匹配对的相似度
    matched_sims = similarity_matrix[row_ind, col_ind]
    # 平均匹配度
    avg_similarity = float(np.mean(matched_sims)) if match_count > 0 else 0.0

    # 未匹配数量统计
    unmatched_gold = len(gold_tasks) - match_count
    unmatched_gen = len(gen_tasks) - match_count

    # 匹配详情（方便排查 bad case）
    match_pairs = [
        {
            "gold_task": gold_tasks[i],
            "gen_task": gen_tasks[j],
            "similarity": round(float(sim), 4)
        }
        for i, j, sim in zip(row_ind, col_ind, matched_sims)
    ]

    return {
        "avg_similarity": round(avg_similarity, 4),
        "match_count": match_count,
        "unmatched_gold": unmatched_gold,
        "unmatched_gen": unmatched_gen,
        "match_pairs": match_pairs
    }


def calculate_task_redundancy_rate(gen_tasks: list[str]) -> float:
    """
    计算生成任务的语义冗余率
    算法逻辑：
        1. 对所有生成任务批量向量化；
        2. 归一化后计算两两余弦相似度，得到自相似度矩阵；
        3. 取出所有「不重复两两配对」的相似度（排除自己和自己比对的对角线）；
        4. 取所有配对相似度的平均值，作为整体冗余率。
        数值越高，说明任务之间内容重叠越严重。

    Args:
        gen_tasks: 生成的任务标题列表

    Returns:
        float: 任务语义冗余率，取值范围 0~1，越高冗余越严重
    """
    # ========== 边界处理：任务数小于2，不存在两两配对，冗余率为0 ==========
    if len(gen_tasks) < 2:
        return 0.0

    # ========== 1. 批量向量化（和覆盖率/匹配度函数完全同口径） ==========
    task_vectors = client.embed(gen_tasks, timeout=60.0)
    task_matrix = np.array(task_vectors, dtype=np.float32)

    # ========== 2. 向量归一化 ==========
    norm = np.linalg.norm(task_matrix, axis=1, keepdims=True)
    norm[norm == 0] = 1e-8  # 零向量保护，避免除零
    task_normalized = task_matrix / norm

    # ========== 3. 计算自相似度矩阵 ==========
    # 矩阵形状：(任务数, 任务数)，matrix[i][j] = 任务i 和 任务j 的相似度
    similarity_matrix = np.dot(task_normalized, task_normalized.T)

    # ========== 4. 取上三角（不包含对角线），即所有不重复两两配对 ==========
    # k=1 表示从对角线往上偏移1位，排除自己和自己比的1.0
    n = len(gen_tasks)
    triu_indices = np.triu_indices(n, k=1)
    pairwise_sims = similarity_matrix[triu_indices]

    # ========== 5. 平均值即为冗余率 ==========
    redundancy_rate = float(np.mean(pairwise_sims))

    return round(redundancy_rate, 4)

