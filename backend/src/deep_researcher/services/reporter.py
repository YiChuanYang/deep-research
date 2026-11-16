"""整合带引用的摘要，校验后由程序补充参考条目。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import closing
from time import perf_counter

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.logging_config import log_operation
from deep_researcher.models import CitedText, SummaryState
from deep_researcher.prompts import report_writer_instructions
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.services.report_format import report_paragraphs

logger = logging.getLogger(__name__)


class ReportingService:
    """只允许引用已完成摘要实际使用的证据。"""

    def __init__(
        self,
        agent_factory: Callable[[], SimpleAgent],
    ) -> None:
        self._agent_factory = agent_factory

    @log_operation(
        "报告生成",
        state_parameter="state",
    )
    def generate_report(
        self,
        state: SummaryState,
        *,
        on_text: Callable[[str], None] | None = None,
        on_validating: Callable[[], None] | None = None,
        include_references: bool = True,
        previous_report: str | None = None,
        feedback: str | None = None,
        supplement_task_ids: list[int] | None = None,
    ) -> CitedText:
        started_at = perf_counter()

        tasks = [
            task
            for task in state.todo_items
            if task.status == "completed" and task.summary
        ]

        if not tasks:
            logger.warning(
                "没有有效任务摘要，返回资料不足说明"
            )

            return CitedText(
                text=(
                    "# 研究结果\n\n"
                    "本轮未获得足以形成结论的资料。"
                )
            )
        evidence_service = EvidenceService()
        allowed: list[str] = []

        # 再次校验摘要，确保进入报告的材料关系完整。
        for task in tasks:
            ids = evidence_service.validate(
                state,
                task.summary or "",
                task.evidence_ids,
            )
            allowed.extend(task.evidence_ids if task.retrieval else ids)

        allowed = list(dict.fromkeys(allowed))

        task_summaries = "\n\n".join(
            f"## 任务 {task.id}：{task.title}\n"
            f"任务要求：{task.intent}\n{task.summary}\n资料限制：{'；'.join(task.notices) or '无'}"
            for task in tasks
        )

        prompt = report_writer_instructions.format(
            research_topic=(
                state.research_topic or "未命名主题"
            ),
            task_summaries=task_summaries,
            evidence_context=evidence_service.context(
                state,
                allowed,
            ),
        )
        prompt += (
            "\n逐项回应原始研究要求，围绕问题组织结论，不按任务摘要机械拼接。"
            "涉及工程设计时写清步骤顺序、事务或状态边界、失败窗口、重试副作用和验证办法。"
            "摘要可能遗漏重要内容，应再次核对本次提供的证据；保留条件、否定和版本。"
            "用户给定的假设是分析前提，不得用无关真实产品替换，也不要给题设数字挂外部引用。"
            "材料不足的要求明确写出未知，不能用相关主题的背景介绍代替回答。"
        )
        if previous_report:
            supplements = [state.get_task(task_id) for task_id in supplement_task_ids or []]
            # 本轮材料单独标明，避免它淹没在旧摘要中；不要求为凑新增引用而采用。
            changes = [
                {
                    "task_id": task.id,
                    "question": task.title,
                    "status": task.status,
                    "summary": task.summary if task.status == "completed" else None,
                    "evidence_ids": task.citation_ids if task.status == "completed" else [],
                    "notices": task.notices,
                }
                for task in supplements
            ]
            prompt += (
                "\n下面是待修订的报告草稿，不是已证实的事实。结合全部任务材料与反馈输出完整的新正文。"
                "保留仍被证据支持的有效内容；无法支持的结论应删除或缩小范围。不得把补搜失败写成证据支持。\n"
                "P 编号与反馈的 paragraph_id 一一对应，只用于定位，不要写进新正文。\n"
                + json.dumps(report_paragraphs(previous_report), ensure_ascii=False)
                + "\n需要处理的反馈：\n" + (feedback or "无")
                + "\n本轮补充研究结果：\n" + json.dumps(changes, ensure_ascii=False)
                + "\n逐项核对反馈和原文，误报不必照做。用补充证据回答真正未解决的问题；"
                "已有材料足够时直接修正。保留仍有依据的内容，不能仅更改空行或照抄原稿。"
                "若补充任务无有效结果或新材料仍不足，明确写出具体未知，不编造结论。"
                "新引用只在支持新内容时使用，不能用堆引用冒充修订。只输出完整报告正文。"
            )

        logger.info(
            "正在汇总报告 · 任务=%s · 可引用证据=%d",
            "；".join(
                f"{task.id}. {task.title}"
                for task in tasks
            ),
            len(allowed),
            extra={
                "event": "report_started",
                "task_count": len(tasks),
                "task_titles": [task.title for task in tasks],
                "allowed_evidence_ids": allowed,
            },
        )

        agent = self._agent_factory()
        agent.enable_tool_call = False

        try:
            if on_text is None:
                # 兼容原来的 demo_reporter 和非流式调用。
                response = agent.run(
                    input_text=prompt
                )
            else:
                # 使用列表收集分片，
                # 最后再拼成完整正文进行引用校验。
                parts: list[str] = []

                # closing 确保回调抛错时也关闭生成器，释放模型 HTTP 连接。
                with closing(agent.stream(input_text=prompt)) as chunks:
                    for text in chunks:
                        parts.append(text)
                        # 回调通知外层，不改变正文，也不直接写业务状态。
                        on_text(text)

                response = "".join(parts)

        finally:
            # 无论模型成功、失败还是被关闭，
            # 都清理 Agent 的本轮历史。
            agent.clear_history()

        if (
            not isinstance(response, str)
            or not response.strip()
        ):
            raise ValueError("报告正文为空")

        if on_validating is not None:
            on_validating()

        # 等流式分片拼成完整正文后再处理：一个引用可能跨越多个分片。
        # 草稿保留模型原文，最终结果接口交付规范化并校验通过的正式报告。
        prepared = evidence_service.prepare_text(
            state,
            response.strip(),
            allowed,
            document="report",
        )
        body = prepared.text
        citation_ids = prepared.citation_ids

        references = evidence_service.references(
            state,
            citation_ids,
        )

        report = (
            f"{body}\n\n"
            f"## 引用证据\n\n"
            f"{references}"
        ) if include_references else body

        duration = perf_counter() - started_at

        logger.info(
            "报告已生成 · 引用=%s · 耗时=%.2fs",
            "、".join(citation_ids) or "无",
            duration,
            extra={
                "event": "report_completed",
                "citation_ids": citation_ids,
                "report_chars": len(report),
                "duration_s": round(duration, 3),
            },
        )

        return CitedText(
            text=report,
            citation_ids=tuple(citation_ids),
        )
