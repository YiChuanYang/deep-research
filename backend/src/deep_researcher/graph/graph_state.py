"""图保存普通数据，业务服务继续使用原有 dataclass。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any, TypedDict

from deep_researcher.models import (
    Evidence,
    Source,
    SummaryState,
    SummaryStateOutput,
    TodoItem,
)

class ResearchGraphState(TypedDict):
    """langgraph顶层状态"""
    #图版本
    version:int
    payload :dict[str,Any]
    max_tasks:int|None
    active_task_id:int|None
    finished:bool

def encode_state(state:SummaryState)->dict[str,Any]:
    """asdict只能由dataclass注册过的数据类使用，递归展开成为字典"""
    return asdict(state)

def decode_state(payload:dict[str,Any])->SummaryState:
    """还原业务对象"""
    raw=deepcopy(payload)
    raw["todo_items"]= [
        TodoItem(**item) for item in raw["todo_items"]
    ]
   

    
    raw["sources"] = {
        key: Source(**value)
        for key, value in raw["sources"].items()
    }

    raw["evidence"] = {
        key: Evidence(**value)
        for key, value in raw["evidence"].items()
    }
    state = SummaryState(**raw)

    task_ids = {
        task.id for task in state.todo_items
    }

    for key, source in state.sources.items():
        if source.source_id != key:
            raise ValueError("来源编号不一致")

    for key, item in state.evidence.items():
        if (
            item.evidence_id != key
            or item.source_id not in state.sources
            or item.task_id not in task_ids
        ):
            raise ValueError("证据关联不完整")

    for task in state.todo_items:
        if task.search_query is not None and (
            not isinstance(task.search_query, str) or not task.search_query.strip()
        ):
            raise ValueError("已保存的搜索查询必须是非空字符串")
        if any(
            key not in state.evidence
            or state.evidence[key].task_id != task.id
            for key in task.evidence_ids
        ):
            raise ValueError("任务证据关联不完整")

        if not set(task.citation_ids).issubset(
            task.evidence_ids
        ):
            raise ValueError("任务引用超出证据范围")

    if not set(state.report_citation_ids).issubset(
        state.evidence
    ):
        raise ValueError("报告引用不存在")

    return state

def to_output(
    payload: dict[str, Any],
) -> SummaryStateOutput:
    """还原为现有对外返回类型。"""
    state = decode_state(payload)

    return SummaryStateOutput(
        run_id=state.run_id,
        report_markdown=state.structured_report,
        todo_items=state.todo_items,
        sources=state.sources,
        evidence=state.evidence,
        report_citation_ids=state.report_citation_ids,
    )


