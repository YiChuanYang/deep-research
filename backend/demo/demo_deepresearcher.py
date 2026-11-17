"""沿用原 demo 文件名，通过 GraphDeepResearcher 执行完整研究。"""

from deep_researcher.config import get_config
from deep_researcher.graph.graph_researcher import GraphDeepResearcher
from deep_researcher.logging_config import run_cli


def main() -> None:
    """执行研究，并在成功或失败后输出用于恢复的研究编号。"""
    researcher = GraphDeepResearcher(config=get_config())

    try:
        result = researcher.run(
            topic="当前大语言模型的能力排名。",
            max_tasks=5,
        )
        print(result.report_markdown)
    finally:
        print("研究编号：", researcher.last_run_id)


if __name__ == "__main__":
    run_cli(main)
