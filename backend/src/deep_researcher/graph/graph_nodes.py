"""串行研究图：现有服务负责业务，LangGraph 负责调度。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
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
from deep_researcher.prompts import (
    reporter_system_prompt,
    summarizer_system_prompt,
    todo_planner_system_prompt,
)
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.planner import PlanningService
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

    def llm(self) -> MyLLM:
        """首次需要模型时再创建客户端。"""
        if self._llm is None:
            self._llm = MyLLM(config=self.config)

        return self._llm

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

                try:
                    updates = action(
                        state,
                        graph_state,
                    )
                except Exception as exc:
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

    def search(
        self,
        state: SummaryState,
        graph_state: ResearchGraphState,
    ) -> dict[str, Any]:
        task_id = self.task_id(graph_state)

        batch = SearchService(self.config).search_task(
            state,
            task_id,
        )
        task = state.get_task(task_id)
        #更新状态字段
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
        result = ReportingService(
            self.reporter_agent
        ).generate_report(state)
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
            "search" 
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
            "search":"search",
            "report":"report",
        }
    )
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







        




                      
                 