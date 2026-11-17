"""通过正式 LangGraph 入口执行研究，并输出用于恢复的研究编号。"""

from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.logging_config import run_cli


def main() -> None:
    """运行一次研究，失败时仍保留研究编号。"""
    researcher = GraphDeepResearcher()

    try:
        result = researcher.run(
            "高光谱图像分类的研究现状",
            max_tasks=2,
        )
        print(result.report_markdown)
    finally:
        print("研究编号：", researcher.last_run_id)


if __name__ == "__main__":
    run_cli(main)
