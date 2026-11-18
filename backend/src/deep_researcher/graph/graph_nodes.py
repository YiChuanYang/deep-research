"""研究图：业务节点与持久化调度；保留旧串行入口以恢复历史研究。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from copy import copy
from threading import BoundedSemaphore
from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.config import get_stream_writer

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration
from deep_researcher.graph import report_cycle

# 保留已有 Python 调用者的导入位置，路由实现只存在于 builder.py。
from deep_researcher.graph.builder import build_graph as build_graph
from deep_researcher.graph.graph_state import (
    ResearchGraphState,
    decode_state,
    encode_state,
)
from deep_researcher.graph.task_state import (
    accept_evidence,
    accept_summary,
    complete_task,
    skip_task,
    start_task,
)
from deep_researcher.llm.llm import MyLLM
from deep_researcher.llm.structured import ModelOutputError
from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
)
from deep_researcher.models import EvidenceBatch, SummaryState
from deep_researcher.prompts import (
    query_rewriter_system_prompt,
    reporter_system_prompt,
    summarizer_system_prompt,
    todo_planner_system_prompt,
)
from deep_researcher.rag.pipeline import WebRagPipeline
from deep_researcher.report_iteration import ReportIteration
from deep_researcher.services.evidence_service import EvidenceService
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.planner import PlanningService
from deep_researcher.services.query_rewriter import QueryRewriteService
from deep_researcher.services.report_format import report_suffix
from deep_researcher.services.report_reflection import ReportReflectionService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.research_constraints import validate_research_question
from deep_researcher.services.search import SearchService
from deep_researcher.services.summarizer import SummarizationService

logger = logging.getLogger(__name__)

# Callable[[参数类型...], 返回类型] 表示可调用对象的签名。
Action = Callable[[SummaryState, ResearchGraphState], dict[str, Any]]


class ResearchNodes:
    """每次 run/resume 创建独立的节点执行对象。"""

    def __init__(
        self,
        config: Configuration,
        execution_id: str,
    ) -> None:
        self.config = config
        self.execution_id = execution_id
        self._llm: MyLLM | None = None
        self._rag: dict[str, WebRagPipeline] = {}
        self._network_gate = BoundedSemaphore(config.network_concurrency)
        self._model_gate = BoundedSemaphore(config.model_concurrency)
        self._task_gate = BoundedSemaphore(config.task_concurrency)

    def fork_worker(self) -> ResearchNodes:
        """浅复制保留子类工厂；重置可变客户端/检索缓存，限流器继续共享。"""
        worker = copy(self)
        worker._llm = None
        worker._rag = {}
        return worker

    def rag(self, run_id: str) -> WebRagPipeline:
        if run_id not in self._rag:
            self._rag[run_id] = WebRagPipeline(self.config, run_id, self.llm)
        return self._rag[run_id]

    def llm(self) -> MyLLM:
        """首次需要模型时再创建客户端。"""
        if self._llm is None:
            self._llm = MyLLM(config=self.config, request_gate=self._model_gate)

        return self._llm

    def query_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_query_rewriter",
            llm=self.llm(),
            config=self.config,
            sys_prompt=query_rewriter_system_prompt,
            enable_tool_call=False,
        )

    def summary_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_summarizer",
            llm=self.llm(),
            config=self.config,
            sys_prompt=summarizer_system_prompt,
            enable_tool_call=False,
        )

    def reporter_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_reporter",
            llm=self.llm(),
            config=self.config,
            sys_prompt=reporter_system_prompt,
            enable_tool_call=False,
        )

    def report_reflection_service(self) -> ReportReflectionService:
        return ReportReflectionService(self.llm(), self.config)

    def bind(
        self,
        name: str,
        action: Action,
    ) -> Callable[[ResearchGraphState], dict[str, Any]]:
        """接收graph运行阶段名称和action,在内部通过闭包，将节点函数包装成node"""

        # 闭包：返回的 node 保留外层 name、action 和 self。
        def node(
            graph_state: ResearchGraphState,
        ) -> dict[str, Any]:
            """从graph_state中读取state,组装日志上下文，调用节点函数实际执行，返回graph状态的更新字段"""
            if graph_state["version"] != 1:
                raise ValueError("图状态版本不兼容")

            state = decode_state(graph_state["payload"])

            task_nodes = {
                "lookup_corpus",
                "discover_sources",
                "read_sources",
                "index_documents",
                "retrieve_evidence",
                "rewrite_query",
                "search",
                "summarize",
                "save_note",
                "parallel_rewrite",
                "parallel_lookup",
                "parallel_collect",
                "parallel_retrieve",
                "parallel_summarize",
            }

            task_id = graph_state["active_task_id"] if name in task_nodes else None

            stages = {
                "lookup_corpus": "tasks",
                "discover_sources": "tasks",
                "read_sources": "tasks",
                "index_documents": "tasks",
                "retrieve_evidence": "tasks",
                "plan": "plan",
                "select_task": "tasks",
                "rewrite_query": "tasks",
                "search": "tasks",
                "summarize": "tasks",
                "save_note": "tasks",
                "report": "report",
                "reflect_report": "report",
                "prepare_report_revision": "report",
                "finalize_report": "report",
                "block_report": "report",
                "save_report": "save_report",
            }

            with log_context(
                run_id=state.run_id,
                execution_id=self.execution_id,
                task_id=task_id,
                stage=name,
                research_stage=stages.get(name, "tasks"),
                checkpoint=(name if name in task_nodes else None),
                agent=None,
            ):
                logger.debug(
                    "节点开始 · %s",
                    name,
                    extra={
                        "event": "graph_node_started",
                    },
                )

                # writer 是 LangGraph 运行上下文中的过程消息发送函数。
                # 它不会写入 SummaryState，也不会自动保存到检查点。
                writer = get_stream_writer()
                event_data = {"node": name, "task_id": task_id}
                writer({"type": "node.started", "data": event_data})

                started_at = perf_counter()
                try:
                    updates = action(
                        state,
                        graph_state,
                    )
                except Exception as exc:
                    duration_s = round(perf_counter() - started_at, 6)
                    writer(
                        {
                            "type": "node.failed",
                            "data": {**event_data, "duration_s": duration_s},
                        }
                    )
                    logger.info(
                        "节点失败 · %s · 耗时=%.3fs",
                        name,
                        duration_s,
                        extra={
                            "event": "graph_node_completed",
                            "node": name,
                            "node_status": "failed",
                            "duration_s": duration_s,
                        },
                    )
                    log_exception_once(
                        logger,
                        "节点失败 · %s",
                        name,
                        error=exc,
                        extra={
                            "event": "graph_node_failed",
                        },
                    )
                    raise

                duration_s = round(perf_counter() - started_at, 6)
                logger.debug(
                    "节点返回 · %s · 耗时=%.3fs",
                    name,
                    duration_s,
                    extra={
                        "event": "graph_node_completed",
                        "node": name,
                        "node_status": "completed",
                        "duration_s": duration_s,
                    },
                )

                # returned 表示函数已返回；持久化是否完成仍以检查点为准。
                writer(
                    {
                        "type": "node.returned",
                        "data": {**event_data, "duration_s": duration_s},
                    }
                )

                # ** 解包合并字典。
                # 返回更新后，LangGraph 才接收节点的新状态。
                return {
                    "payload": encode_state(state),
                    **updates,
                }

        return node

    def plan(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        """plan任务节点，更新state中的todo_items字段"""
        agent = ToolAwareAgent(
            name="Agent_for_planner",
            llm=self.llm(),
            config=self.config,
            sys_prompt=todo_planner_system_prompt,
        )

        try:
            state.todo_items = PlanningService(agent).plan_todo_list(state)
        finally:
            agent.clear_history()

        if self.config.enable_report_reflection:
            return report_cycle.update(
                ReportIteration(
                    initial_task_ids=report_cycle.selected_ids(state, graph_state)
                )
            )
        return {}

    def select_task(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        tasks = [
            state.get_task(task_id)
            for task_id in report_cycle.selected_ids(state, graph_state)
        ]
        for task in tasks:
            if task.status not in {
                "completed",
                "skipped",
            }:
                start_task(task)

                logger.info(
                    "任务开始 · %s",
                    task.title,
                    extra={
                        "task_id": task.id,
                        "event": "task_started",
                    },
                )

                return {
                    "active_task_id": task.id,
                }

        return {"active_task_id": None}

    @staticmethod
    def task_id(
        graph_state: ResearchGraphState,
    ) -> int:
        task_id = graph_state["active_task_id"]

        if task_id is None:
            raise ValueError("当前没有选中的任务")

        return task_id

    def rewrite_query(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)
        task = state.get_task(task_id)
        cycle = (
            report_cycle.iteration(graph_state)
            if graph_state.get("report_iteration") is not None
            else None
        )
        supplement = (
            next((item for item in cycle.supplements if item.task_id == task_id), None)
            if cycle
            else None
        )
        if supplement is not None:
            try:
                validate_research_question(supplement.question, supplement.query)
            except ModelOutputError:
                skip_task(task, "补充问题是对报告的自评，未发送外部搜索")
                supplement.search_completed = True
                logger.warning("跳过补充任务 · 问题无法通过外部搜索回答")
                return report_cycle.update(cycle)
        task.search_query = QueryRewriteService(
            self.query_agent,
            self.config,
        ).rewrite_task(state, task_id)
        if supplement is not None:
            try:
                validate_research_question(supplement.question, task.effective_query)
            except ModelOutputError:
                # 原始补充查询已校验，只回退改写结果，不把它变成搜索自评问题。
                task.search_query = supplement.query
                task.notices.append("改写带入报告自评用语，沿用原补充查询")
                logger.warning("补充查询改写未采用 · 沿用已校验的原查询")
        if graph_state.get("report_iteration") is not None:
            cycle = report_cycle.iteration(graph_state)
            supplement = next(
                (item for item in cycle.supplements if item.task_id == task_id), None
            )
            used = {
                report_cycle.normalized(item.effective_query)
                for item in state.todo_items
                if item.id != task_id and item.status in {"completed", "skipped"}
            }
            if (
                supplement is not None
                and report_cycle.normalized(task.effective_query) in used
            ):
                skip_task(task, "补充查询与已执行查询重复，未重复发送搜索")
                supplement.search_completed = True
                return report_cycle.update(cycle)
        # bind 会把结果写进 payload；sync 检查点完成后才进入搜索。
        # 搜索失败后从 search 恢复，复用这里已经确定的查询。
        return {}

    def search(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)
        task = state.get_task(task_id)
        if graph_state.get("report_iteration") is not None:
            cycle = report_cycle.iteration(graph_state)
            supplement = next(
                (item for item in cycle.supplements if item.task_id == task_id), None
            )
            if supplement is not None:
                try:
                    # 恢复可能直接从 search 开始，不能只依赖 rewrite_query 的检查。
                    validate_research_question(
                        supplement.question, task.effective_query
                    )
                except ModelOutputError:
                    skip_task(task, "补充问题或查询是报告自评，未发送外部搜索")
                    supplement.search_completed = True
                    logger.warning("补充搜索未执行 · 问题无法通过外部资料回答")
                    return report_cycle.update(cycle)

        batch = SearchService(self.config).search_task(
            state,
            task_id,
        )
        task = state.get_task(task_id)
        accept_evidence(
            state,
            task_id,
            batch,
            missing_notice="搜索没有可用证据",
            keep_notices=False,
        )

        if graph_state.get("report_iteration") is not None:
            cycle = report_cycle.iteration(graph_state)
            supplement = next(
                (item for item in cycle.supplements if item.task_id == task_id), None
            )
            if supplement is not None:
                previous = decode_state(graph_state["payload"])
                known = {
                    report_cycle.evidence_fingerprint(item)
                    for item in previous.evidence.values()
                }
                for key in task.evidence_ids:
                    fingerprint = report_cycle.evidence_fingerprint(state.evidence[key])
                    if fingerprint not in known:
                        supplement.novel_evidence_ids.append(key)
                        known.add(fingerprint)
                supplement.search_completed = True
                return report_cycle.update(cycle)
        return {}

    def discover_sources(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        task = state.get_task(self.task_id(graph_state))
        with self._network_gate:
            result = self.rag(state.run_id).discover(state, task.id)
        task.retrieval.update(stage="discovered", discovered=len(result["candidates"]))
        return {}

    def read_sources(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        task = state.get_task(self.task_id(graph_state))
        with self._network_gate:
            result = self.rag(state.run_id).read(task.id)
        task.retrieval.update(
            {k: v for k, v in result.items() if k not in {"page_ids", "notices"}}
        )
        task.retrieval["stage"] = "read"
        task.notices = result["notices"]
        return {}

    def index_documents(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        task = state.get_task(self.task_id(graph_state))
        result = self.rag(state.run_id).index(task.id)
        task.retrieval.update({k: v for k, v in result.items() if k != "notices"})
        task.retrieval["stage"] = "indexed"
        task.notices = result["notices"]
        return {}

    def lookup_corpus(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        """补充任务先查本研究资料；只有明确可回答才跳过联网。"""
        task = state.get_task(self.task_id(graph_state))
        if task.retrieval.get("local_checked"):
            return {}
        pipeline = self.rag(state.run_id)
        batch = pipeline.retrieve(state, task.id, local_only=True)
        sufficient = pipeline.local_sufficient(state, task.id, batch)
        task.retrieval.update(local_checked=True, local_sufficient=sufficient)
        if sufficient:
            task.notices = list(
                dict.fromkeys(
                    [
                        *task.notices,
                        *batch.notices,
                        "本次补充复用已有网页资料，未发起搜索",
                    ]
                )
            )
            return self._accept_rag_batch(state, graph_state, batch)
        logger.info(
            "已有资料不足 · 继续联网补充", extra={"event": "web_local_insufficient"}
        )
        return {}

    def retrieve_evidence(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        batch = self.rag(state.run_id).retrieve(state, self.task_id(graph_state))
        return self._accept_rag_batch(state, graph_state, batch)

    def _accept_rag_batch(
        self, state: SummaryState, graph_state: ResearchGraphState, batch: EvidenceBatch
    ) -> dict[str, Any]:
        task = state.get_task(self.task_id(graph_state))
        known = {
            report_cycle.evidence_fingerprint(item) for item in state.evidence.values()
        }
        accept_evidence(
            state, task.id, batch, missing_notice="没有选出能回答当前任务的证据"
        )
        task.retrieval.update(stage="retrieved", selected=len(task.evidence_ids))
        if graph_state.get("report_iteration") is not None:
            cycle = report_cycle.iteration(graph_state)
            supplement = next(
                (s for s in cycle.supplements if s.task_id == task.id), None
            )
            if supplement is not None:
                supplement.search_completed = True
                for key in task.evidence_ids:
                    fingerprint = report_cycle.evidence_fingerprint(state.evidence[key])
                    if fingerprint not in known:
                        supplement.novel_evidence_ids.append(key)
                        known.add(fingerprint)
                return report_cycle.update(cycle)
        return {}

    def summarize(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)
        cycle = (
            report_cycle.iteration(graph_state)
            if graph_state.get("report_iteration") is not None
            else None
        )
        supplement = (
            next((item for item in cycle.supplements if item.task_id == task_id), None)
            if cycle
            else None
        )

        result = SummarizationService(
            self.summary_agent,
            config=self.config,
        ).summarize_task(
            state,
            task_id,
            research_question=supplement.question if supplement else None,
        )

        task = state.get_task(task_id)
        accept_summary(task, result)
        return {}

    def save_note(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)

        saved = NoteService(self.config).save_task_note(
            state,
            task_id,
        )

        task = state.get_task(task_id)
        complete_task(task, saved)

        logger.info(
            "任务完成",
            extra={"event": "task_completed"},
        )

        return {}

    def report(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        cycle = (
            report_cycle.iteration(graph_state)
            if self.config.enable_report_reflection
            else None
        )
        previous = cycle.drafts[-1] if cycle and cycle.drafts else None
        writer = get_stream_writer()
        # 每次重新生成有独立编号，前端据此清空旧草稿。
        generation_id = uuid4().hex
        pending: list[str] = []
        pending_chars = 0
        last_flush = perf_counter()

        def emit(event_type: str, **data: Any) -> None:
            writer(
                {
                    "type": event_type,
                    "data": {"generation_id": generation_id, **data},
                }
            )

        def flush() -> None:
            nonlocal pending_chars, last_flush
            # nonlocal 允许修改外层函数的变量，而不是创建同名局部变量。
            if pending:
                emit("report.delta", text="".join(pending))
                pending.clear()
                pending_chars = 0
                last_flush = perf_counter()

        def on_text(text: str) -> None:
            nonlocal pending_chars
            pending.append(text)
            pending_chars += len(text)
            # 合并小分片，避免每个 token 都进行一次 SQLite 事务。
            # 时间阈值在收到分片时检查；结束/失败时会强制发出余下内容。
            if pending_chars >= 512 or perf_counter() - last_flush >= 0.08:
                flush()

        def on_validating() -> None:
            flush()
            emit("report.validating")

        emit("report.started", revision=cycle.revision if cycle else 0)
        try:
            result = ReportingService(self.reporter_agent).generate_report(
                state,
                on_text=on_text,
                on_validating=on_validating,
                include_references=cycle is None,
                previous_report=previous.text if previous else None,
                feedback=json.dumps(previous.review.model_dump(), ensure_ascii=False)
                if previous and previous.review
                else None,
                supplement_task_ids=(
                    [
                        item.task_id
                        for item in cycle.supplements
                        if item.revision == cycle.revision
                    ]
                    if cycle
                    else None
                ),
            )
        finally:
            flush()

        emit("report.generated", chars=len(result.text))
        # generated 仅表示报告服务返回；正式可交付仍以 report_ready 为准。
        report = result.text

        if cycle is not None:
            report_cycle.commit_draft(cycle, result.text, list(result.citation_ids))
            return report_cycle.update(cycle)
        report += report_suffix(state)

        state.structured_report = report
        state.report_citation_ids = list(result.citation_ids)

        return {}

    def reflect_report(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        cycle = report_cycle.iteration(graph_state)
        draft = cycle.drafts[-1]
        if draft.revision != cycle.revision or draft.review is not None:
            raise ValueError("待评审的报告版本不一致")
        outcome = self.report_reflection_service().review(
            state, draft, allow_repair=cycle.protocol_repairs_used == 0
        )
        cycle.protocol_repairs_used += outcome.repairs_used
        draft.review, draft.review_error = outcome.review, outcome.error
        draft.review_input_chars = outcome.input_chars
        report_cycle.decide_after_review(cycle, self.config)
        get_stream_writer()(
            {
                "type": "report.reviewed",
                "data": {
                    "revision": cycle.revision,
                    "status": cycle.status,
                    "issue_count": len(draft.review.issues) if draft.review else None,
                },
            }
        )
        return report_cycle.update(cycle)

    def prepare_report_revision(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        cycle = report_cycle.iteration(graph_state)
        report_cycle.prepare_round(state, cycle, self.config)
        return report_cycle.update(cycle)

    def finalize_report(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        cycle = report_cycle.iteration(graph_state)
        if (
            cycle.status not in {"accepted", "limited", "unreviewed"}
            or cycle.next_action != "finalize"
        ):
            raise ValueError("报告尚未达到交付条件")
        draft = cycle.drafts[-1]
        if draft.review and draft.review.blocking:
            raise ValueError("仍有未解决的实质问题，不能交付报告")
        if cycle.status == "accepted" and (draft.review is None or draft.review.issues):
            raise ValueError("缺少与当前报告绑定的通过评审")
        if cycle.status == "unreviewed" and any(
            item.review and item.review.blocking for item in cycle.drafts
        ):
            raise ValueError("未复核状态不能绕过已记录的实质问题")
        allowed = [
            key
            for task in state.todo_items
            if task.status == "completed"
            for key in (task.evidence_ids if task.retrieval else task.citation_ids)
        ]
        if draft.citation_ids:
            actual = EvidenceService().validate(state, draft.text, allowed)
            if set(actual) != set(draft.citation_ids):
                raise ValueError("报告引用记录与正文不一致")
        elif draft.text != "# 研究结果\n\n本轮未获得足以形成结论的资料。":
            raise ValueError("无引用报告不属于资料不足说明")
        state.report_limitations = [
            {
                "accepted": "模型复核未提出需要继续处理的问题；这不代表外部事实已获证实。",
                "limited": "本次为有限交付：" + cycle.stop_reason,
                "unreviewed": "报告未完成语义复核：" + cycle.stop_reason,
            }[cycle.status]
        ]
        if draft.review:
            state.report_limitations.extend(
                "待补充：" + issue.reason for issue in draft.review.issues
            )
        body = draft.text
        if draft.citation_ids:
            body += "\n\n## 引用证据\n\n" + EvidenceService().references(
                state, draft.citation_ids
            )
        state.structured_report = body + report_suffix(state)
        state.report_citation_ids = list(draft.citation_ids)
        logger.info(
            "报告进入交付 · 第 %d 版 · %s",
            cycle.revision + 1,
            cycle.status,
            extra={"event": "report_adopted", "review_status": cycle.status},
        )
        return {}

    def block_report(
        self, state: SummaryState, graph_state: ResearchGraphState
    ) -> dict[str, Any]:
        # 单独节点失败：上一节点保存的草稿、评审和预算仍然可查。
        cycle = report_cycle.iteration(graph_state)
        raise RuntimeError(
            "报告仍有未解决的实质问题，已保留草稿，不能发布：" + cycle.stop_reason
        )

    def save_report(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        saved = NoteService(self.config).save_report_note(state)
        state.report_note_id = saved.note_id if saved is not None else None
        state.report_note_path = saved.note_path if saved is not None else None

        return {"finished": True}

    def close(self) -> None:
        if self._llm is not None:
            self._llm.close()
            self._llm = None
