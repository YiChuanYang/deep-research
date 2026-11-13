"""串行研究图：现有服务负责业务，LangGraph 负责调度。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.config import Configuration
from deep_researcher.graph.graph_state import (
    ResearchGraphState,
    decode_state,
    encode_state,
)
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import (
    log_context,
    log_exception_once,
)
from deep_researcher.models import SummaryState
from deep_researcher.selection_audit import SelectionAuditStore
from deep_researcher.prompts import (
    query_rewriter_system_prompt,
    evidence_selector_system_prompt,
    reporter_system_prompt,
    summarizer_system_prompt,
    todo_planner_system_prompt,
)
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.planner import PlanningService
from deep_researcher.services.query_rewriter import QueryRewriteService
from deep_researcher.services.reporter import ReportingService
from deep_researcher.services.search import SearchService
from deep_researcher.services.summarizer import SummarizationService

logger = logging.getLogger(__name__)

# Callable[[参数类型...], 返回类型] 表示可调用对象的签名。
Action = Callable[[SummaryState,ResearchGraphState],dict[str,Any]]

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
        self.selection_audits: SelectionAuditStore | None = None

    def llm(self) -> MyLLM:
        """首次需要模型时再创建客户端。"""
        if self._llm is None:
            self._llm = MyLLM(config=self.config)

        return self._llm

    def query_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_query_rewriter",
            llm=self.llm(),
            config=self.config,
            sys_prompt=query_rewriter_system_prompt,
            enable_tool_call=False,
        )

    def evidence_selector_agent(self) -> SimpleAgent:
        return SimpleAgent(
            name="Agent_for_evidence_selector", llm=self.llm(), config=self.config,
            sys_prompt=evidence_selector_system_prompt, enable_tool_call=False,
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
    def bind(
        self,
        name: str,
        action: Action,
    ) -> Callable[[ResearchGraphState], dict[str, Any]]:
        """ 接收graph运行阶段名称和action,在内部通过闭包，将节点函数包装成node"""

        # 闭包：返回的 node 保留外层 name、action 和 self。
        def node(
            graph_state: ResearchGraphState,
        ) -> dict[str, Any]:
            '''从graph_state中读取state,组装日志上下文，调用节点函数实际执行，返回graph状态的更新字段'''
            if graph_state["version"] != 1:
                raise ValueError("图状态版本不兼容")

            state = decode_state(
                graph_state["payload"]
            )

            task_nodes = {
                "rewrite_query",
                "search",
                "summarize",
                "save_note",
            }

            task_id = (
                graph_state["active_task_id"]
                if name in task_nodes
                else None
            )

            stages = {
                "plan": "plan",
                "select_task": "tasks",
                "rewrite_query": "tasks",
                "search": "tasks",
                "summarize": "tasks",
                "save_note": "tasks",
                "report": "report",
                "save_report": "save_report",
            }

            with log_context(
                run_id=state.run_id,
                execution_id=self.execution_id,
                task_id=task_id,
                stage=name,
                research_stage=stages[name],
                checkpoint=(
                    name if name in task_nodes else None
                ),
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

                try:
                    updates = action(
                        state,
                        graph_state,
                    )
                except Exception as exc:
                    writer({"type": "node.failed", "data": event_data})
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

                logger.debug(
                    "节点返回 · %s",
                    name,
                    extra={
                        "event": "graph_node_returned",
                    },
                )

                # returned 表示函数已返回；持久化是否完成仍以检查点为准。
                writer({"type": "node.returned", "data": event_data})

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

        return {}

    def select_task(
            self,
            state:SummaryState,
            graph_state:ResearchGraphState,
    )->dict[str,Any]:
        limit = graph_state["max_tasks"]
        tasks = (
            state.todo_items 
            if limit is None 
            else state.todo_items[:limit]
        )
        for task in tasks:
            if task.status not in {
                "completed",
                "skipped",
            }:
                task.status="running"

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
        task.search_query = QueryRewriteService(
            self.query_agent, self.config,
        ).rewrite_task(state, task_id)
        # bind 会把结果写进 payload；sync 检查点完成后才进入搜索。
        # 搜索失败后从 search 恢复，复用这里已经确定的查询。
        return {}

    def search(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)

        batch = SearchService(
            self.config, selector_agent_factory=self.evidence_selector_agent,
        ).search_task(
            state,
            task_id,
        )
        task = state.get_task(task_id)
        #更新状态字段
        if batch.selection_audit:
            batch.selection_audit["execution_id"] = self.execution_id
            if self.selection_audits is not None:
                try:
                    self.selection_audits.save(state.run_id, task_id, batch.selection_audit)
                except Exception as exc:
                    logger.warning("筛选审计保存失败 · %s", type(exc).__name__)
                    batch.notices.append("筛选审计保存失败；已保留正式证据片段")
            get_stream_writer()({
                "type": "evidence.selected",
                "data": {"task_id": task_id, "count": len(batch.evidence_ids)},
            })
        state.sources = batch.sources
        state.evidence = batch.evidence
        task.evidence_ids = list(batch.evidence_ids)
        task.notices = list(batch.notices)
        if not task.evidence_ids:
            task.status = "skipped"
            task.notices.append(
                "搜索没有可用证据"
            )

        return {}
    def summarize(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)

        result = SummarizationService(
            self.summary_agent,
            config=self.config,
        ).summarize_task(
            state,
            task_id,
        )

        task = state.get_task(task_id)
        #更新状态字段
        task.summary = result.text
        task.citation_ids = list(result.citation_ids)

        if not task.citation_ids:
            task.status = "skipped"
            task.notices.append(
                "无有效研究信息，未纳入报告"
            )
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
        task.note_id = (
            saved.note_id
            if saved is not None
            else None
        )
        task.note_path = (
            saved.note_path
            if saved is not None
            else None
        )
        task.status = "completed"

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
        writer = get_stream_writer()
        # 每次重新生成有独立编号，前端据此清空旧草稿。
        generation_id = uuid4().hex
        pending: list[str] = []
        pending_chars = 0
        last_flush = perf_counter()

        def emit(event_type: str, **data: Any) -> None:
            writer({
                "type": event_type,
                "data": {"generation_id": generation_id, **data},
            })

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

        emit("report.started")
        try:
            result = ReportingService(self.reporter_agent).generate_report(
                state,
                on_text=on_text,
                on_validating=on_validating,
            )
        finally:
            flush()

        emit("report.generated", chars=len(result.text))
        # generated 仅表示报告服务返回；正式可交付仍以 report_ready 为准。
        report = result.text

        unfinished = [
            task
            for task in state.todo_items
            if task.status != "completed"
        ]

        if unfinished:
            labels = {
                "pending": "本轮未执行",
                "running": "尚未完成",
                "skipped": "无有效资料",
            }

            report += "\n\n## 研究范围说明\n\n"

            report += "\n".join(
                f"- 任务 {task.id}「{task.title}」："
                f"{labels[task.status]}。"
                for task in unfinished
            )

        state.structured_report = report
        state.report_citation_ids = list(
            result.citation_ids
        )

        return {}

    def save_report(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        saved = NoteService(self.config).save_report_note(
            state
        )
        state.report_note_id = (
            saved.note_id
            if saved is not None
            else None
        )
        state.report_note_path = (
            saved.note_path
            if saved is not None
            else None
        )

        return {"finished": True}

    def close(self) -> None:
        if self._llm is not None:
            self._llm.close()
            self._llm = None

def build_graph(nodes:ResearchNodes,checkpointer:BaseCheckpointSaver)->CompiledStateGraph:
    """定义节点和条件边，编译为可执行图"""
    #定义节点
    builder = StateGraph(ResearchGraphState)
    actions:dict[str,Action]={
        "plan":nodes.plan,
        "select_task":nodes.select_task,
        "rewrite_query":nodes.rewrite_query,
        "search":nodes.search,
        "summarize":nodes.summarize,
        "save_note":nodes.save_note,
        "report":nodes.report,
        "save_report":nodes.save_report
    }
    for name,action in actions.items():
        builder.add_node(
            name,
            nodes.bind(name,action),

        )

    def after_selected(graph_state:ResearchGraphState)->str:
        """select_task 后的路由函数"""
        return (
            "rewrite_query"
            if graph_state["active_task_id"] is not None
            else "report")

    def after_searched(graph_state:ResearchGraphState)->str:
        """search函数后的路由"""
        summary_state = decode_state(graph_state["payload"])
        task = summary_state.get_task(nodes.task_id(graph_state))
        return (
            "select_task" 
            if task.status=="skipped"
            else "summarize"
        )

    def after_summarized(graph_state:ResearchGraphState)->str:
        """总结后的路由函数"""
        summary_state = decode_state(graph_state["payload"])
        task = summary_state.get_task(nodes.task_id(graph_state))
        return (
            "select_task" 
            if task.status=="skipped"
            else "save_note"
        )

    builder.add_edge(START,"plan")
    builder.add_edge(
        "plan",
        "select_task"
    )
    builder.add_conditional_edges(
        "select_task",
        after_selected,
        {
            "rewrite_query":"rewrite_query",
            "report":"report",
        }
    )
    builder.add_edge("rewrite_query", "search")
    builder.add_conditional_edges(
        "search",
        after_searched,
        {
            "select_task":"select_task",
            "summarize":"summarize",
        }
    )
    builder.add_conditional_edges(
        "summarize",
        after_summarized,
        {
            "save_note":"save_note",
            "select_task":"select_task"
        }
    )
    builder.add_edge("save_note","select_task")
    builder.add_edge("report","save_report")
    builder.add_edge("save_report",END)

    return builder.compile(checkpointer=checkpointer)
