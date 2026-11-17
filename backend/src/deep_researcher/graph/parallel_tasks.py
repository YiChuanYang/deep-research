"""分阶段 fan-out/fan-in；分支只提交本任务产物，主图独占业务状态写入。"""
from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph
from langgraph.types import Send

from deep_researcher.graph import report_cycle
from deep_researcher.graph.graph_state import ResearchGraphState, decode_state, encode_state
from deep_researcher.logging_config import log_context
from deep_researcher.models import SummaryState, TodoItem

if TYPE_CHECKING:
    from deep_researcher.graph.graph_nodes import ResearchNodes

logger = logging.getLogger(__name__)
Phase = Literal["rewrite", "lookup", "collect", "retrieve", "summarize"]


class WorkerInput(TypedDict):
    """Send 的分支输入；values 是本阶段共同的已提交快照。"""

    values: ResearchGraphState
    task_id: int


class ParallelTasks:
    def __init__(self, nodes: ResearchNodes) -> None:
        self.nodes = nodes
        self._timing_lock = Lock()
        self._phase_started: dict[str, float] = {}

    def start_phase(self, values: ResearchGraphState, phase: Phase) -> None:
        with self._timing_lock:
            self._phase_started.setdefault(values["parallel_batch_id"] + phase, perf_counter())

    def finish_phase(self, values: ResearchGraphState, phase: Phase) -> None:
        with self._timing_lock:
            started = self._phase_started.pop(values["parallel_batch_id"] + phase, None)
        if started is not None:
            duration = perf_counter() - started
            logger.info("并发阶段完成 · phase=%s · 耗时=%.2fs", phase, duration,
                        extra={"event": "parallel_phase_completed", "parallel_phase": phase, "duration_s": duration})

    @staticmethod
    def key(values: ResearchGraphState, phase: Phase, task_id: int) -> str:
        return f"{values['parallel_batch_id']}:{phase}:{task_id}"

    @staticmethod
    def eligible(values: ResearchGraphState, phase: Phase) -> list[int]:
        state = decode_state(values["payload"])
        cycle = report_cycle.iteration(values) if values.get("report_iteration") is not None else None
        supplemental = {s.task_id for s in cycle.supplements} if cycle else set()
        result: list[int] = []
        for task_id in values.get("parallel_task_ids", []):
            task = state.get_task(task_id)
            if task.status in {"skipped", "completed"}:
                continue
            if phase == "lookup" and task_id not in supplemental:
                continue
            if phase in {"collect", "retrieve"} and task.retrieval.get("local_sufficient"):
                continue
            result.append(task_id)
        return result

    def dispatch(self, phase: Phase, merge_node: str) -> Callable[[ResearchGraphState], list[Send] | str]:
        def route(values: ResearchGraphState) -> list[Send] | str:
            ids = self.eligible(values, phase)
            # Send 为每个任务调用同一个节点；并发数由 graph 的 max_concurrency 限制。
            return [Send(f"parallel_{phase}", {"values": values, "task_id": i}) for i in ids] or merge_node
        return route

    def worker(self, phase: Phase) -> Callable[[WorkerInput], dict[str, Any]]:
        def run(job: WorkerInput) -> dict[str, Any]:
            # deepcopy 防止某个分支修改其他分支收到的字典。
            values = deepcopy(job["values"])
            task_id = job["task_id"]
            values["active_task_id"] = task_id
            self.start_phase(values, phase)
            worker = self.nodes.fork_worker()
            key = self.key(values, phase, task_id)

            def action(state: SummaryState, local_values: ResearchGraphState) -> dict[str, Any]:
                store = worker.rag(state.run_id).store
                cache_key = "parallel-result:" + key
                saved = store.record(cache_key)
                if saved is not None:
                    logger.info("复用分支产物 · phase=%s · task=%d", phase, task_id,
                                extra={"event": "parallel_result_reused"})
                    return {"parallel_results": {key: saved}}
                if phase == "rewrite":
                    worker.rewrite_query(state, local_values)
                elif phase == "lookup":
                    worker.lookup_corpus(state, local_values)
                elif phase == "collect":
                    worker.discover_sources(state, local_values)
                    worker.read_sources(state, local_values)
                elif phase == "retrieve":
                    worker.rag(state.run_id).select_evidence(state, task_id, page_ids=values["parallel_page_ids"])
                else:
                    worker.summarize(state, local_values)
                result: dict[str, Any] = {"task": asdict(state.get_task(task_id))}
                # 先落盘，再返回图更新。失败后的重试不需要重做已经提交的模型调用。
                store.save_record(cache_key, result)
                return {"parallel_results": {key: result}}

            try:
                update = worker.bind(f"parallel_{phase}", action)(values)
                # 绝不能把分支内的完整 payload 写回主图；仅提交 reducer 字段。
                return {"parallel_results": update["parallel_results"]}
            finally:
                worker.close()

        def execute(job: WorkerInput) -> dict[str, Any]:
            with self.nodes._task_gate:
                return run(job)
        return execute

    def prepare(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
        ids = [i for i in report_cycle.selected_ids(state, values)
               if state.get_task(i).status not in {"completed", "skipped"}]
        for task_id in ids:
            task = state.get_task(task_id)
            task.status = "running"
            logger.info("并发任务就绪 · T%02d · %s", task_id, task.title,
                        extra={"event": "task_started", "task_id": task_id})
        logger.info("并发批次开始 · 任务=%d · workers=%d · network=%d · model=%d", len(ids),
                    self.nodes.config.task_concurrency, self.nodes.config.network_concurrency,
                    self.nodes.config.model_concurrency, extra={"event": "parallel_batch_started"})
        return {"parallel_batch_id": uuid4().hex, "parallel_task_ids": ids,
                "parallel_page_ids": [], "active_task_id": None}

    def merge(self, phase: Phase) -> Callable[[SummaryState, ResearchGraphState], dict[str, Any]]:
        def commit(state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
            self.finish_phase(values, phase)
            # 合并顺序固定为任务规划顺序，不随线程结束先后而变化。
            for task_id in self.eligible(values, phase):
                result = values.get("parallel_results", {}).get(self.key(values, phase, task_id))
                if result is None:
                    raise ValueError(f"并发结果缺失：{phase}/{task_id}")
                completed = TodoItem(**result["task"])
                task = state.get_task(task_id)
                if completed.id != task.id:
                    raise ValueError("分支返回了其他任务的结果")
                fields = {
                    "rewrite": ("search_query", "status", "notices"),
                    "lookup": ("retrieval", "notices", "status"),
                    "collect": ("retrieval", "notices"),
                    "retrieve": (),
                    "summarize": ("summary", "citation_ids", "status", "notices"),
                }[phase]
                for field in fields:
                    setattr(task, field, deepcopy(getattr(completed, field)))
            if phase == "rewrite" and values.get("report_iteration") is not None:
                cycle = report_cycle.iteration(values)
                used = {report_cycle.normalized(t.effective_query) for t in state.todo_items
                        if t.id not in values["parallel_task_ids"] and t.status in {"completed", "skipped"}}
                for task_id in values["parallel_task_ids"]:
                    task = state.get_task(task_id)
                    supplement = next((s for s in cycle.supplements if s.task_id == task_id), None)
                    if supplement is not None:
                        if task.status == "skipped" or report_cycle.normalized(task.effective_query) in used:
                            task.status = "skipped"
                            task.notices = list(dict.fromkeys([*task.notices, "补充查询重复或不适合外部检索，未执行搜索"]))
                            supplement.search_completed = True
                        used.add(report_cycle.normalized(task.effective_query))
                return report_cycle.update(cycle)
            return {}
        return commit

    def index_batch(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
        # 图中只有这个节点写索引；跨任务相同正文由已有内容缓存去重。
        for task_id in self.eligible(values, "collect"):
            with log_context(task_id=task_id):
                self.nodes.index_documents(state, {**values, "active_task_id": task_id})
        latest: dict[str, str] = {}
        for page in sorted(self.nodes.rag(state.run_id).store.pages(), key=lambda p: p.retrieved_at):
            latest[page.url] = page.snapshot_id
        return {"parallel_page_ids": sorted(latest.values())}

    def register_batch(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
        self.finish_phase(values, "retrieve")
        updated = dict(values)
        for task_id in self.eligible(values, "summarize"):
            task = state.get_task(task_id)
            with log_context(task_id=task_id):
                batch = self.nodes.rag(state.run_id).register_evidence(
                    state, task_id, local_only=bool(task.retrieval.get("local_sufficient")),
                )
                delta = self.nodes._accept_rag_batch(state, {**updated, "active_task_id": task_id}, batch)
                updated.update(delta)
        return {"report_iteration": updated["report_iteration"]} if "report_iteration" in updated else {}

    def save_batch(self, state: SummaryState, values: ResearchGraphState) -> dict[str, Any]:
        for task_id in values["parallel_task_ids"]:
            if state.get_task(task_id).status not in {"skipped", "completed"}:
                with log_context(task_id=task_id):
                    self.nodes.save_note(state, {**values, "active_task_id": task_id})
        # 完整状态校验，包括摘要引用必须属于当前任务，之后才提交主图检查点。
        decode_state(encode_state(state))
        return {"active_task_id": None, "parallel_task_ids": []}

    def install(self, builder: StateGraph) -> None:
        sequential = {
            "parallel_prepare": self.prepare,
            "parallel_merge_rewrite": self.merge("rewrite"),
            "parallel_merge_lookup": self.merge("lookup"),
            "parallel_merge_collect": self.merge("collect"),
            "parallel_index": self.index_batch,
            "parallel_register": self.register_batch,
            "parallel_merge_summary": self.merge("summarize"),
            "parallel_save": self.save_batch,
        }
        for name, action in sequential.items():
            builder.add_node(name, self.nodes.bind(name, action))
        phases: list[tuple[str, Phase, str]] = [
            ("parallel_prepare", "rewrite", "parallel_merge_rewrite"),
            ("parallel_merge_rewrite", "lookup", "parallel_merge_lookup"),
            ("parallel_merge_lookup", "collect", "parallel_merge_collect"),
            ("parallel_index", "retrieve", "parallel_register"),
            ("parallel_register", "summarize", "parallel_merge_summary"),
        ]
        for source, phase, destination in phases:
            worker_name = f"parallel_{phase}"
            builder.add_node(worker_name, self.worker(phase), input_schema=WorkerInput)
            builder.add_conditional_edges(source, self.dispatch(phase, destination), [worker_name, destination])
            builder.add_edge(worker_name, destination)
        builder.add_edge("parallel_merge_collect", "parallel_index")
        builder.add_edge("parallel_merge_summary", "parallel_save")
        builder.add_edge("parallel_save", "report")
