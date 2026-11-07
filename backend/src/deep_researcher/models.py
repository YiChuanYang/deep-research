from dataclasses import dataclass ,field #内部流转数据，使用dataclass 不用pydantic 减少开销
from typing import List, Optional,Literal,Any



from typing_extensions import Annotated
import operator
@dataclass(kw_only=True) #kw_only=True表示只能使用关键字参数来初始化对象
class TodoItem:
    """单个待办任务"""
    id: int
    title: str
    intent: str
    query: str
    status: Literal[
    "pending",
    "running",
    "failed",
    "completed",
    "skipped",
] = "pending"

    checkpoint: Literal[
    "search",
    "summarize",
    "save_note",
    "done",
] = "search"
    last_error: str | None = None
    summary: Optional[str] = None
    sources_summary: Optional[str] = None
    #列表、字典和集合这些可变对象不能直接设置默认值
    notices: list[str] = field(default_factory=list)
    note_id: Optional[str] = None
    note_path: Optional[str] = None
    stream_token: Optional[str] = None
    #证据
    # 本轮搜索提供给这个任务的证据。
    evidence_ids: list[str] = field(default_factory=list)
    # 最终任务摘要实际使用的证据。
    citation_ids: list[str] = field(default_factory=list)

#证据引用类
@dataclass(kw_only=True, frozen=True)
class Source:
    """一个来源页面；编号在同一研究内唯一。"""

    source_id: str
    url: str
    title: str


@dataclass(kw_only=True, frozen=True)
class Evidence:
    """实际提供给模型的材料片段，不等同于已验证事实。"""

    evidence_id: str
    source_id: str
    task_id: int
    text: str
    retrieved_at: str
    query: str
    backend: str
    kind: Literal["search_snippet", "page_excerpt"]
    truncated: bool = False

@dataclass(kw_only=True)
class SummaryState:
    run_id :str =""
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
    #证据
    sources: dict[str, Source] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    report_citation_ids: list[str] = field(default_factory=list)

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


#数据库类
@dataclass
class ResearchRecord:
    """一次研究的状态、执行阶段和非敏感配置"""
    state:SummaryState
    config:dict[str,Any]
    max_tasks:int|None=None
    stage:Literal["plan","tasks","report","save_report","done"]="plan"
    status:Literal["pending","running","failed","completed"]="pending"
    last_error:str|None=None
    #令牌,dataclass自动生成__repr__方法可以打印字段。repr=False表示打印时不输出该字段
    owner_token:str|None=field(default=None,repr=False)



@dataclass(kw_only=True)
class SummaryStateInput:
    research_topic: Optional[str] = field(default=None)  # Report topic


@dataclass(kw_only=True)
class SummaryStateOutput:
    run_id :str =""
    running_summary: Optional[str] = field(default=None)  # Backward-compatible文本
    report_markdown: Optional[str] = field(default=None)
    todo_items: List[TodoItem] = field(default_factory=list)
    sources: dict[str, Source] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    report_citation_ids: list[str] = field(default_factory=list)

