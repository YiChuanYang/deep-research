"""验证日志配置、上下文、异常链和真实服务之间的协作。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import RateLimitError

from deep_researcher.agents.simple_agent import SimpleAgent
from deep_researcher.config import Configuration, SearchAPI
from deep_researcher.llm.llm import MyLLM
from deep_researcher.logging_config import (
    ConsoleFormatter,
    log_context,
    log_exception_once,
    log_operation,
    run_cli,
    setup_logging,
)
from deep_researcher.models import SummaryState, TodoItem
from deep_researcher.repository import ResearchRepository
from deep_researcher.services.note_service import NoteService
from deep_researcher.services.reporter import ReportingService

Logs = tuple[io.StringIO, Path]

LOG = logging.getLogger("deep_researcher.services.search")


@pytest.fixture
def logs(tmp_path: Path) -> tuple[io.StringIO, Path]:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    names = ("deep_researcher", "httpx", "httpcore", "openai", "urllib3", "ddgs")
    old_levels = {name: logging.getLogger(name).level for name in names}
    stream = io.StringIO()
    path = setup_logging(stream=stream, log_dir=tmp_path, color=False)
    assert path is not None
    try:
        yield stream, path
    finally:
        for handler in root.handlers[:]:
            if handler not in old_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(old_level)
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_levels_fields_and_single_console_icon(logs: Logs) -> None:
    stream, path = logs
    with log_context(run_id="R12345678", task_id=2, stage="搜索"):
        LOG.info("🔎 检索完成 · %d 个来源", 5)
        LOG.debug("工具参数数量=3", extra={"event": "search_detail"})
    output = stream.getvalue()
    assert output.count("🔎") == 1
    assert "services.search" in output and "R12345678" in output and "T02" in output
    assert "工具参数数量" not in output and "\x1b[" not in output
    saved = records(path)
    assert len(saved) == 2
    assert saved[1]["run_id"] == "R12345678" and saved[1]["task_id"] == 2
    assert saved[1]["stage"] == "搜索" and saved[1]["event"] == "search_detail"
    assert saved[0]["module"] == "deep_researcher.services.search"


def test_setup_is_idempotent_and_retains_host_handler(logs: Logs) -> None:
    stream, path = logs
    host = logging.NullHandler()
    logging.getLogger().addHandler(host)
    try:
        setup_logging(stream=stream, log_dir=path.parent, color=False)
        setup_logging(stream=stream, log_dir=path.parent, color=False)
        assert host in logging.getLogger().handlers
        LOG.info("只出现一次")
        assert stream.getvalue().count("只出现一次") == 1
        assert len(records(path)) == 1
    finally:
        logging.getLogger().removeHandler(host)


def test_nested_context_restores_after_exception(logs: Logs) -> None:
    _, path = logs
    with log_context(run_id="RA", task_id=1):
        try:
            with log_context(task_id=2):
                LOG.info("inner")
                raise ValueError("test")
        except ValueError:
            pass
        LOG.info("outer")
    LOG.info("outside")
    saved = records(path)
    assert [item["task_id"] for item in saved] == [2, 1, None]
    assert saved[-1]["run_id"] is None


def test_async_contexts_do_not_mix(logs: Logs) -> None:
    _, path = logs

    async def worker(run_id: str, task_id: int) -> None:
        with log_context(run_id=run_id, task_id=task_id):
            await asyncio.sleep(0)
            LOG.info("worker")

    async def run() -> None:
        await asyncio.gather(worker("RA", 1), worker("RB", 2))

    asyncio.run(run())
    assert {(row["run_id"], row["task_id"]) for row in records(path) if row["message"] == "worker"} == {
        ("RA", 1),
        ("RB", 2),
    }


def test_exception_logged_once_with_original_cause(logs: Logs) -> None:
    stream, path = logs

    @log_operation("报告生成")
    def inner() -> None:
        try:
            raise ValueError("原始错误")
        except ValueError as exc:
            raise RuntimeError("上层说明") from exc

    @log_operation("研究流程", new_run=True)
    def outer() -> None:
        inner()

    with pytest.raises(RuntimeError):
        outer()
    errors = [row for row in records(path) if row["level"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["error"]["type"] == "ValueError"
    assert "RuntimeError: 上层说明" in errors[0]["error"]["traceback"]
    assert errors[0]["stage"] == "报告生成"
    assert "ValueError: 原始错误" in stream.getvalue()
    assert "Traceback" not in stream.getvalue()


def test_error_429_survives_real_llm_agent_and_reporter(logs: Logs) -> None:
    stream, path = logs
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://offline.invalid")
    )
    error = RateLimitError(
        "账户达到速率限制",
        response=response,
        body={"error": {"code": "1302", "message": "账户达到速率限制"}},
    )

    def create(**kwargs: Any) -> None:
        raise error

    llm = MyLLM.__new__(MyLLM)
    llm.llm_provider = "offline"
    llm.model = "offline-model"
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    state = SummaryState(
        todo_items=[
            TodoItem(
                id=1,
                title="任务",
                query="检索",
                intent="目标",
                summary="有效摘要",
                status="completed",
            )
        ]
    )
    reporter = ReportingService(lambda: SimpleAgent(name="reporter", llm=llm))
    with pytest.raises(RuntimeError) as caught:
        reporter.generate_report(state)
    assert caught.value.__cause__ is error
    errors = [row for row in records(path) if row["level"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["error"]["status_code"] == 429
    assert errors[0]["error"]["type"] == "RateLimitError"
    assert "模型响应为空" not in stream.getvalue()
    assert state.structured_report is None


def test_color_and_encoding_fallback() -> None:
    record = logging.makeLogRecord(
        {
            "name": "deep_researcher.services.note_service",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "📝 已保存",
            "run_id": "R1",
            "task_id": 1,
        }
    )
    colored = ConsoleFormatter(color=True).format(record)
    assert "\x1b[" in colored and "📒 services.note" in colored
    fallback = ConsoleFormatter(emoji=False, encoding="ascii").format(record)
    fallback.encode("ascii")
    assert "services.note" in fallback


def test_file_rotation_is_bounded(logs: Logs) -> None:
    stream, path = logs
    setup_logging(
        stream=stream,
        log_dir=path.parent,
        color=False,
        max_bytes=900,
        backup_count=2,
    )
    for index in range(20):
        LOG.debug("轮转检查 %d %s", index, "x" * 100)
    paths = list(path.parent.glob("research.jsonl*"))
    assert len(paths) == 3
    assert all(records(item) for item in paths)


def test_cli_exits_once_without_second_traceback(
    logs: Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream, path = logs
    monkeypatch.setattr("deep_researcher.logging_config.setup_logging", lambda: path)

    @log_operation("演示")
    def main() -> None:
        raise RuntimeError("模拟失败")

    with pytest.raises(SystemExit) as caught:
        run_cli(main)
    assert caught.value.code == 1
    assert len([row for row in records(path) if row["level"] == "ERROR"]) == 1
    assert "Traceback" not in stream.getvalue()


def test_workflow_propagates_ids_without_repeating_success_logs(
    logs: Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deep_researcher import deep_researcher as workflow_module
    from tests.test_note_workflow import ScriptedLLM, answer, tool_response

    stream, path = logs
    llm = ScriptedLLM(
        [
            tool_response(
                {
                    "tasks": [
                        {"title": "评测", "intent": "比较模型", "query": "benchmark"}
                    ]
                },
                "generate_research_tasks",
            ),
            answer("规划完成"),
            answer("基于证据的摘要"),
            answer("# 研究报告"),
            tool_response(
                {
                    "tasks": [
                        {"title": "评测", "intent": "比较模型", "query": "benchmark"}
                    ]
                },
                "generate_research_tasks",
            ),
            answer("规划完成"),
            answer("另一次摘要"),
            answer("# 第二份报告"),
        ]
    )
    llm.model = "offline"
    llm.client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(workflow_module, "MyLLM", lambda: llm)
    payload = {
        "results": [{"title": "来源", "url": "https://example.com", "content": "证据"}],
        "backend": "duckduckgo",
        "answer": None,
        "notices": [],
    }

    def dispatch(
        *args: Any, **kwargs: Any
    ) -> tuple[dict[str, Any], list[str], None, str]:
        LOG.info("检索完成 · 1 个来源")
        return payload, [], None, "duckduckgo"

    monkeypatch.setattr("deep_researcher.services.search.dispatch_search", dispatch)
    repository = ResearchRepository(":memory:")
    workflow = workflow_module.DeepResearcher(
        Configuration(
            enable_notes=True,
            notes_workspace=str(path.parent / "notes"),
            search_api=SearchAPI.DUCKDUCKGO,
        ),
        repository=repository,
    )
    try:
        result = workflow.run("主题一")
        workflow.run("主题二")
    finally:
        workflow.close()
        repository.close()
    assert result.report_markdown == "# 研究报告"
    rows = records(path)
    starts = [row for row in rows if row.get("event") == "research_started"]
    assert len(starts) == 2 and starts[0]["run_id"] != starts[1]["run_id"]
    assert starts[0]["run_id"] == result.run_id
    assert starts[0]["execution_id"] != starts[1]["execution_id"]
    summaries = [row for row in rows if row.get("event") == "summary_completed"]
    assert [row["run_id"] for row in summaries] == [row["run_id"] for row in starts]
    assert all(row["task_id"] == 1 for row in summaries)
    models = [row for row in rows if row["component"] == "agents.simple"]
    assert any(
        row["task_id"] == 1 and row["agent"] == "Agent_for_summarizer" for row in models
    )
    assert all(row["research_stage"] == "tasks" and row["checkpoint"] == "summarize" for row in summaries)
    assert stream.getvalue().count("摘要已生成") == 2
    assert "Agent 已初始化" not in stream.getvalue()
    assert "笔记操作完成" not in stream.getvalue()
    assert "%d" not in stream.getvalue()


def test_full_id_is_kept_in_json_and_shortened_only_on_console(logs: Logs) -> None:
    stream, path = logs
    run_id = "abc12345" + "0" * 24
    with log_context(run_id=run_id, stage="研究调度"):
        logging.getLogger("deep_researcher.repository").info("取得执行权")
    assert "RABC12345" in stream.getvalue()
    assert run_id not in stream.getvalue()
    assert "storage.repository" in stream.getvalue()
    assert "研究调度" in stream.getvalue()
    assert records(path)[0]["run_id"] == run_id


def test_note_service_uses_state_identity_and_records_create_or_replace(logs: Logs) -> None:
    _, path = logs
    state = SummaryState(
        run_id="a" * 32,
        todo_items=[TodoItem(id=1, title="任务", intent="目标", query="检索", summary="完整正文")],
    )
    notes = NoteService(Configuration(enable_notes=True, notes_workspace=str(path.parent / "notes")))
    with log_context(run_id="entry", task_id=99):
        assert notes.load_task_note(state, 1) == ""
        notes.save_task_note(state, 1)
        notes.save_task_note(state, 1)
        assert "完整正文" in notes.load_task_note(state, 1)
        LOG.info("回到入口")
    rows = records(path)
    writes = [row for row in rows if row.get("event") == "note_written"]
    assert [row["note_created"] for row in writes] == [True, False]
    assert all(row["run_id"] == state.run_id and row["task_id"] == 1 for row in rows[:-1])
    assert all(row["checkpoint"] == "save_note" for row in writes)
    assert rows[-1]["run_id"] == "entry" and rows[-1]["task_id"] == 99
    assert "完整正文" not in path.read_text(encoding="utf-8")


def test_secondary_failure_gets_its_own_error_id(logs: Logs) -> None:
    _, path = logs
    try:
        raise RuntimeError("模型失败")
    except RuntimeError as original:
        original_id = log_exception_once(LOG, "业务失败", error=original)
        try:
            raise OSError("磁盘不可写")
        except OSError as secondary:
            secondary_id = log_exception_once(
                LOG, "保存故障状态失败", error=secondary,
                extra={"related_error_id": original_id},
            )
        assert log_exception_once(LOG, "重复业务失败", error=original) == original_id
    errors = [row for row in records(path) if row["level"] == "ERROR"]
    assert len(errors) == 2
    assert original_id != secondary_id
    assert errors[1]["error"]["type"] == "OSError"
    assert errors[1]["related_error_id"] == errors[0]["error_id"]


@pytest.mark.parametrize("fail_checkpoint_save", [False, True])
def test_failed_workflow_and_resume_keep_log_identity(
    logs: Logs, monkeypatch: pytest.MonkeyPatch, fail_checkpoint_save: bool,
) -> None:
    from deep_researcher import deep_researcher as workflow_module
    from tests.test_note_workflow import ScriptedLLM, answer, tool_response

    _, path = logs
    llm = ScriptedLLM([
        tool_response({"tasks": [{"title": "任务", "intent": "目标", "query": "检索"}]}, "generate_research_tasks"),
        answer("规划完成"), RuntimeError("模拟模型失败"), answer("恢复后的摘要"), answer("# 恢复后的报告"),
    ])
    llm.client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(workflow_module, "MyLLM", lambda: llm)
    search_calls: list[int] = []

    def dispatch(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[str], None, str]:
        search_calls.append(1)
        return {"results": [{"title": "来源", "url": "https://example.com", "content": "证据"}]}, [], None, "duckduckgo"

    monkeypatch.setattr("deep_researcher.services.search.dispatch_search", dispatch)
    repository = ResearchRepository(":memory:")
    real_update = repository.update
    if fail_checkpoint_save:
        def update(record: Any, status: str = "running") -> None:
            if status == "failed":
                raise sqlite3.OperationalError("模拟数据库不可写")
            real_update(record, status=status)
        monkeypatch.setattr(repository, "update", update)

    workflow = workflow_module.DeepResearcher(
        Configuration(enable_notes=True, notes_workspace=str(path.parent / "notes"), search_api=SearchAPI.DUCKDUCKGO),
        repository=repository,
    )
    try:
        with pytest.raises(RuntimeError, match="模型请求失败"):
            workflow.run("测试恢复")
        run_id = workflow.last_run_id
        assert run_id is not None
        first_rows = records(path)
        failed = next(row for row in first_rows if row.get("event") == "research_failed")
        errors = [row for row in first_rows if row["level"] == "ERROR"]
        assert failed["failure_saved"] is not fail_checkpoint_save
        assert len(errors) == (2 if fail_checkpoint_save else 1)
        assert all(row["run_id"] == run_id and row["execution_id"] == failed["execution_id"] for row in errors)
        assert errors[0]["task_id"] == 1 and errors[0]["checkpoint"] == "summarize"
        assert errors[0]["research_stage"] == "tasks"
        if fail_checkpoint_save:
            assert errors[1]["error"]["type"] == "OperationalError"
            assert errors[1]["related_error_id"] == errors[0]["error_id"]
            return

        result = workflow.resume(run_id)
        assert result.report_markdown == "# 恢复后的报告"
        assert len(search_calls) == 1
        rows = records(path)
        completed = next(row for row in rows if row.get("event") == "research_completed")
        assert completed["run_id"] == failed["run_id"]
        assert completed["execution_id"] != failed["execution_id"]
        resumed = [row for row in rows if row["execution_id"] == completed["execution_id"]]
        assert next(row for row in resumed if row.get("event") == "task_started")["checkpoint"] == "summarize"
        assert not any(row.get("event") == "plan_started" for row in resumed)
        commits = [row for row in resumed if row.get("event") == "checkpoint_saved"]
        assert commits[-1]["status"] == "completed" and commits[-1]["research_stage"] == "done"
        assert commits[-1]["task_checkpoints"] == [{"task_id": 1, "checkpoint": "done", "status": "completed"}]
        assert len([row for row in rows if row["level"] == "ERROR"]) == 1
    finally:
        workflow.close()
        repository.close()
