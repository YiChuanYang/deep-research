"""离线展示分栏日志，不调用模型、搜索或笔记工具."""

from __future__ import annotations

import logging
from uuid import uuid4

from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
    run_cli,
)


def main() -> None:
    """输出演示数据，展示正常进度、DEBUG 细节和错误根因."""
    research = logging.getLogger("deep_researcher.graph.graph_researcher")
    search = logging.getLogger("deep_researcher.services.search")
    summary = logging.getLogger("deep_researcher.services.summarizer")
    notes = logging.getLogger("deep_researcher.services.note_service")
    reporter = logging.getLogger("deep_researcher.services.reporter")
    repository = logging.getLogger("deep_researcher.graph.graph_repository")
    run_id = uuid4().hex
    with log_context(run_id=run_id, execution_id=uuid4().hex, stage="新建研究", research_stage="plan"):
        research.info("开始演示 · 以下为模拟数据", extra={"event": "research_started"})
        with log_context(stage="研究规划"):
            logging.getLogger("deep_researcher.services.planner").info("规划完成 · 1 个任务")
        with log_context(task_id=1, stage="执行任务", research_stage="tasks", checkpoint="search"):
            logging.getLogger("deep_researcher.graph.graph_nodes").info(
                "开始 · 模型能力评测 · checkpoint=search"
            )
            with log_context(stage="任务搜索"):
                search.info("检索完成 · 5 个来源 · 耗时=1.80s")
                search.debug("模拟底层细节 · 默认只写入 JSON 文件")
            with log_context(stage="任务总结", checkpoint="summarize"):
                summary.info("摘要已生成 · 986 字 · 耗时=6.20s")
            with log_context(stage="保存任务笔记", checkpoint="save_note"):
                repository.debug("检查点已保存 · stage=tasks status=running", extra={"event": "checkpoint_saved"})
                notes.info("任务笔记已保存 · 1080 字")
        with log_context(stage="报告生成", research_stage="report", checkpoint=None):
            reporter.info("正在汇总报告 · 1 个任务")
            try:
                raise RuntimeError("模拟 HTTP 429 · code=1302 · 账户达到速率限制")
            except RuntimeError as exc:
                log_exception_once(reporter, "报告生成失败（演示）", error=exc)
            research.warning("研究已中止 · stage=report · run_id=%s", run_id)

    with log_context(run_id=run_id, execution_id=uuid4().hex, stage="恢复研究", research_stage="report"):
        research.info("准备恢复研究（演示）", extra={"event": "research_resume_requested"})
        repository.info("执行权已取得 · stage=report · 原状态=failed")
        with log_context(stage="报告生成"):
            reporter.info("报告已生成 · 1280 字 · 耗时=5.00s")
        research.info("演示结束 · 未发送外部请求")


if __name__ == "__main__":
    run_cli(main)
