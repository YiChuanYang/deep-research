"""整合已完成子任务并生成最终研究报告."""

from __future__ import annotations

import logging
from collections.abc import Callable

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.models import SummaryState
from deep_researcher.prompts import report_writer_instructions

logger = logging.getLogger(__name__)


class ReportingService:
	"""读取共享研究状态，生成并保存最终报告文本."""

	def __init__(self, agent_factory: Callable[[], SimpleAgent]) -> None:
		self._agent_factory = agent_factory

	def generate_report(self, state: SummaryState) -> str:
		completed_tasks = [
			task
			for task in state.todo_items
			if task.status == "completed" and task.summary
		]
		if not completed_tasks:
			raise ValueError("没有可汇总的已完成任务，无法生成报告")

		task_summaries = "\n\n".join(
			f"## 任务 {task.id}: {task.title}\n"
			f"目标：{task.intent}\n\n"
			f"{task.summary}\n\n"
			f"来源：\n{task.sources_summary or '暂无来源'}"
			for task in completed_tasks
		)
		prompt = report_writer_instructions.format(
			research_topic=state.research_topic or "未命名研究主题",
			task_summaries=task_summaries,
		)

		logger.info(
			"📝 开始生成研究报告：completed_tasks=%d",
			len(completed_tasks),
		)
		agent = self._agent_factory()
		try:
			report = agent.run(input_text=prompt).strip()
		except Exception:
			logger.exception("❌ 研究报告生成失败：tasks=%d", len(completed_tasks))
			raise
		finally:
			agent.clear_history()

		if not report:
			raise ValueError("报告 Agent 返回了空内容")

		state.structured_report = report
		state.running_summary = report
		logger.info("✅ 研究报告生成完成：chars=%d", len(report))
		return report
