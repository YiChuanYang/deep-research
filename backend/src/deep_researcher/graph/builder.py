"""LangGraph 的节点注册与路由；这里不生成内容，也不持久化业务结果。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from deep_researcher.graph import report_cycle
from deep_researcher.graph.graph_state import ResearchGraphState, decode_state
from deep_researcher.graph.parallel_tasks import ParallelTasks

if TYPE_CHECKING:
    from deep_researcher.graph.graph_nodes import Action, ResearchNodes


def build_graph(
    nodes: ResearchNodes, checkpointer: BaseCheckpointSaver
) -> CompiledStateGraph:
    """定义节点和条件边，编译为可执行图"""
    # 定义节点
    builder = StateGraph(ResearchGraphState)
    actions: dict[str, Action] = {
        "lookup_corpus": nodes.lookup_corpus,
        "discover_sources": nodes.discover_sources,
        "read_sources": nodes.read_sources,
        "index_documents": nodes.index_documents,
        "retrieve_evidence": nodes.retrieve_evidence,
        "plan": nodes.plan,
        "select_task": nodes.select_task,
        "rewrite_query": nodes.rewrite_query,
        "search": nodes.search,
        "summarize": nodes.summarize,
        "save_note": nodes.save_note,
        "report": nodes.report,
        "reflect_report": nodes.reflect_report,
        "prepare_report_revision": nodes.prepare_report_revision,
        "finalize_report": nodes.finalize_report,
        "block_report": nodes.block_report,
        "save_report": nodes.save_report,
    }
    for name, action in actions.items():
        builder.add_node(
            name,
            nodes.bind(name, action),
        )

    ParallelTasks(nodes).install(builder)

    def task_entry() -> str:
        return (
            "parallel_prepare"
            if nodes.config.enable_parallel_tasks and nodes.config.enable_web_rag
            else "select_task"
        )

    def after_selected(graph_state: ResearchGraphState) -> str:
        """select_task 后的路由函数"""
        return (
            "rewrite_query" if graph_state["active_task_id"] is not None else "report"
        )

    def after_searched(graph_state: ResearchGraphState) -> str:
        """search函数后的路由"""
        summary_state = decode_state(graph_state["payload"])
        task = summary_state.get_task(nodes.task_id(graph_state))
        return "select_task" if task.status == "skipped" else "summarize"

    def after_rewritten(graph_state: ResearchGraphState) -> str:
        task = decode_state(graph_state["payload"]).get_task(nodes.task_id(graph_state))
        if task.status == "skipped":
            return "select_task"
        if not nodes.config.enable_web_rag:
            return "search"
        cycle = graph_state.get("report_iteration")
        if cycle and any(s["task_id"] == task.id for s in cycle["supplements"]):
            return "lookup_corpus"
        return "discover_sources"

    def after_summarized(graph_state: ResearchGraphState) -> str:
        """总结后的路由函数"""
        summary_state = decode_state(graph_state["payload"])
        task = summary_state.get_task(nodes.task_id(graph_state))
        return "select_task" if task.status == "skipped" else "save_note"

    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan", lambda _: task_entry(), ["parallel_prepare", "select_task"]
    )
    builder.add_conditional_edges(
        "select_task",
        after_selected,
        {
            "rewrite_query": "rewrite_query",
            "report": "report",
        },
    )
    builder.add_conditional_edges(
        "rewrite_query",
        after_rewritten,
        ["select_task", "search", "discover_sources", "lookup_corpus"],
    )
    builder.add_edge("discover_sources", "read_sources")
    builder.add_edge("read_sources", "index_documents")
    builder.add_edge("index_documents", "retrieve_evidence")
    builder.add_conditional_edges(
        "retrieve_evidence", after_searched, ["select_task", "summarize"]
    )

    def after_local(values: ResearchGraphState) -> str:
        task = decode_state(values["payload"]).get_task(nodes.task_id(values))
        return (
            "summarize"
            if task.retrieval.get("local_sufficient")
            else "discover_sources"
        )

    builder.add_conditional_edges(
        "lookup_corpus", after_local, ["summarize", "discover_sources"]
    )
    builder.add_conditional_edges(
        "search",
        after_searched,
        {
            "select_task": "select_task",
            "summarize": "summarize",
        },
    )
    builder.add_conditional_edges(
        "summarize",
        after_summarized,
        {"save_note": "save_note", "select_task": "select_task"},
    )
    builder.add_edge("save_note", "select_task")

    def after_report(values: ResearchGraphState) -> str:
        if not nodes.config.enable_report_reflection:
            return "save_report"
        return {
            "review": "reflect_report",
            "finalize": "finalize_report",
            "blocked": "block_report",
        }[report_cycle.iteration(values).next_action]

    def after_review(values: ResearchGraphState) -> str:
        return {
            "prepare": "prepare_report_revision",
            "finalize": "finalize_report",
            "blocked": "block_report",
        }[report_cycle.iteration(values).next_action]

    def after_prepared(values: ResearchGraphState) -> str:
        return {
            "tasks": task_entry(),
            "report": "report",
            "finalize": "finalize_report",
            "blocked": "block_report",
        }[report_cycle.iteration(values).next_action]

    builder.add_conditional_edges(
        "report",
        after_report,
        ["save_report", "reflect_report", "finalize_report", "block_report"],
    )
    builder.add_conditional_edges(
        "reflect_report",
        after_review,
        ["prepare_report_revision", "finalize_report", "block_report"],
    )
    builder.add_conditional_edges(
        "prepare_report_revision",
        after_prepared,
        [
            "parallel_prepare",
            "select_task",
            "report",
            "finalize_report",
            "block_report",
        ],
    )
    builder.add_edge("finalize_report", "save_report")
    builder.add_edge("save_report", END)

    return builder.compile(checkpointer=checkpointer)
