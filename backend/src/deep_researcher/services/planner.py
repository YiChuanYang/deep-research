"""规划服务，原生工具调用"""
from __future__ import annotations

import json
import logging
from typing import List,Dict

from deep_researcher.agents.tool_aware_agent import ToolAwareAgent
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.config import Configuration
from deep_researcher.prompts import todo_planner_instructions, get_current_date
from deep_researcher.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

def _generate_research_tasks(tasks:List[Dict])->str:
    """虚拟工具，使模型以参数列表的形式返回研究任务"""
    return f"已接受{len(tasks)}个调研任务"

class PlanningService:
    """研究任务规划服务"""
    def __init__(self,agent:ToolAwareAgent)->None:
        self._agent = agent
        self._init_planner_tool()


    def _init_planner_tool(self)->None:
        """注册虚拟工具，动态挂载到agent上"""
        registry = ToolRegistry()
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
        #挂载到agent
        ToolAwareAgent.attach_registry(self._agent,registry)

    def plan_todo_list(self,state:SummaryState)->List[TodoItem]:
        """通过监听器捕捉结构化参数，构造todoitem"""
        #拼接运行时提示词
        prompt = todo_planner_instructions.format(
            current_date = get_current_date(),
            research_topic = state.research_topic
        )
        parsed_tasks:List[Dict]=[]

        def capture_tasks(event:Dict):
            """临时监听器，临时替换agent自带的监听器"""
            #声明变量
            nonlocal parsed_tasks
            try:
                 args = json.loads(event["raw_parameters"])
                 parsed_tasks = args.get("tasks",[])
            except Exception :
                pass

        #临时挂载监听器
        original_listener = self._agent.tool_call_listener
        self._agent.tool_call_listener = capture_tasks

        try:
            self._agent.run(
                input_text=prompt,
                tool_choice="required"
            )

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
        if not todo_items:
            logger.warning("规划工具调用解析失败，使用兜底任务")
            todo_items.append(self.create_fallback_task(state))

        state.todo_items = todo_items
        titles = [task.title for task in todo_items]
        logger.info("规划器生成 %d 个任务：%s", len(todo_items), titles)
        return todo_items

    @staticmethod
    def create_fallback_task(state: SummaryState) -> TodoItem:
        return TodoItem(
            id=1,
            title="基础背景梳理",
            intent="收集主题的核心背景与最新动态",
            query=f"{state.research_topic} 最新进展" if state.research_topic else "基础背景梳理",
        )






