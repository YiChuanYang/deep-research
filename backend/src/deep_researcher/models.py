from dataclasses import dataclass ,field #内部流转数据，使用dataclass 不用pydantic 减少开销
from typing import List, Optional
from typing_extensions import Annotated
import operator
@dataclass(kw_only=True) #kw_only=True表示只能使用关键字参数来初始化对象
class TodoItem:
    """单个待办任务"""
    id: int
    title: str
    intent: str
    query: str
    status: str='pending'
    summary: Optional[str] = None
    sources_summary: Optional[str] = None
    #列表、字典和集合这些可变对象不能直接设置默认值
    notices: list[str] = field(default_factory=list)
    note_id: Optional[str] = None
    note_path: Optional[str] = None
    stream_token: Optional[str] = None

@dataclass(kw_only=True)
class SummaryState:
    research_topic: Optional[str] = field(default=None)  # Report topic
    search_query: Optional[str] = field(default=None)  # Deprecated placeholder
    # State owns task results; services address tasks by ID.
    web_research_results: Annotated[list, operator.add] = field(default_factory=list)
    sources_gathered: Annotated[list, operator.add] = field(default_factory=list)
    search_contexts: dict[int, str] = field(default_factory=dict)
    research_loop_count: int = field(default=0)  # Research loop count
    running_summary: Optional[str] = field(default=None)  # Legacy summary field
    todo_items: Annotated[list, operator.add] = field(default_factory=list)
    structured_report: Optional[str] = field(default=None)
    report_note_id: Optional[str] = field(default=None)
    report_note_path: Optional[str] = field(default=None)

    def get_task(self, task_id: int) -> TodoItem:
        for task in self.todo_items:
            if task.id == task_id:
                return task
        raise KeyError(f"任务不存在：{task_id}")
    def __repr__(self):
        lines = ["SummaryState("]
        for key, value in self.__dict__.items():
            if key.startswith("_"):
                continue
            # 超长字符串截断，防止控制台刷屏
            if isinstance(value, str) and len(value) > 150:
                val_str = repr(value[:150] + "……")
            else:
                val_str = repr(value)
            lines.append(f"  {key}={val_str},")
        lines.append(")")
        return "\n".join(lines)



@dataclass(kw_only=True)
class SummaryStateInput:
    research_topic: Optional[str] = field(default=None)  # Report topic


@dataclass(kw_only=True)
class SummaryStateOutput:
    running_summary: Optional[str] = field(default=None)  # Backward-compatible文本
    report_markdown: Optional[str] = field(default=None)
    todo_items: List[TodoItem] = field(default_factory=list)