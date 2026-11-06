"""规划服务，原生工具调用"""
from __future__ import annotations

import json
import logging
from typing import Any, List

from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.prompts import todo_planner_instructions, get_current_date
from deep_researcher.tools.registry import ToolRegistry

from deep_researcher.logging_config import log_operation

logger = logging.getLogger(__name__)

def _generate_research_tasks(tasks: List[dict[str, Any]]) -> str:
    """虚拟工具，使模型以参数列表的形式返回研究任务"""
    return f"已接受{len(tasks)}个调研任务"

class PlanningService:
    """研究任务规划服务"""
    def __init__(self,agent:ToolAwareAgent)->None:
        self._agent = agent
        self._init_planner_tool()


    def _init_planner_tool(self)->None:
        """注册虚拟工具，动态挂载到agent上"""
        registry = self._agent.tool_registry
        if registry is None:
            registry = ToolRegistry()

        ToolAwareAgent.attach_registry(self._agent, registry)
        registry.register_function(
            name="generate_research_tasks",
            description="生成结构化的调研子任务列表，必须调用此工具输出结果。",
            func=_generate_research_tasks,
            parameters={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "调研子任务列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "任务标题"},
                                "intent": {"type": "string", "description": "任务意图与调研目标"},
                                "query": {"type": "string", "description": "推荐的搜索关键词"},
                            },
                            "required": ["title", "intent", "query"]
                        }
                    }
                },
                "required": ["tasks"]
            }
        )
    @log_operation("研究规划")
    def plan_todo_list(self,state:SummaryState)->List[TodoItem]:
        """通过监听器捕捉结构化参数，构造todoitem"""
        #拼接运行时提示词
        prompt = todo_planner_instructions.format(
            current_date = get_current_date(),
            research_topic = state.research_topic
        )
        parsed_tasks: list[dict[str, Any]] = []

        def capture_tasks(event: dict[str, Any]) -> None:
            """只接收成功的规划工具结果，其他工具事件交给原监听器。"""
            nonlocal parsed_tasks
            if (
                event.get("tool_name") != "generate_research_tasks"
                or not event.get("success")
            ):
                return
            try:
                args = json.loads(event["raw_parameters"])
                if not isinstance(args, dict):
                    raise TypeError("规划工具参数必须是字典")
                tasks = args.get("tasks", [])
                if not isinstance(tasks, list) or not all(
                    isinstance(task, dict) for task in tasks
                ):
                    raise TypeError("tasks 必须是由字典组成的列表")
                parsed_tasks = tasks
                logger.debug(
                    "已捕获规划参数 · call_id=%s tasks=%d",
                    event.get("tool_call_id"), len(parsed_tasks),
                )
            except Exception as exc:
                logger.debug(
                    "⚠️ 规划任务解析失败：error_type=%s",
                    type(exc).__name__,
                )

        #临时挂载监听器
        original_listener = self._agent.tool_call_listener

        def capture_and_forward(event: dict[str, Any]) -> None:
            capture_tasks(event)
            if original_listener is not None:
                original_listener(event)

        self._agent.tool_call_listener = capture_and_forward
        try:
            logger.info("正在规划研究任务")
            self._agent.run(
                input_text=prompt,
                tool_choice="required"
            )
        except Exception as exc:
            logger.debug(
                "❌ 研究计划生成失败：error_type=%s",
                type(exc).__name__,
            )
            raise

        finally:
            self._agent.tool_call_listener = original_listener

        # 组装 TodoItem
        todo_items: List[TodoItem] = []
        for idx, item in enumerate(parsed_tasks, start=1):
            todo_items.append(
                TodoItem(
                    id=idx,
                    title=item.get("title", f"任务{idx}"),
                    intent=item.get("intent", "聚焦主题关键问题"),
                    query=item.get("query", state.research_topic),
                )
            )

        # 极端情况兜底
        used_fallback = not todo_items
        if used_fallback:
            logger.warning(
                "⚠️ 未获取到规划任务，使用兜底任务：topic_chars=%d",
                len(state.research_topic or ""),
            )
            todo_items.append(self.create_fallback_task(state))

        state.todo_items = todo_items
        logger.info(
            "规划完成 · %d 个任务", len(todo_items),
            extra={"event": "plan_completed", "task_count": len(todo_items)},
        )
        return todo_items

    @staticmethod
    def create_fallback_task(state: SummaryState) -> TodoItem:
        return TodoItem(
            id=1,
            title="基础背景梳理",
            intent="收集主题的核心背景与最新动态",
            query=f"{state.research_topic} 最新进展" if state.research_topic else "基础背景梳理",
        )
