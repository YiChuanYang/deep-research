"""统一配置控制台分栏日志、运行上下文和轮转 JSON 日志."""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any, ParamSpec, TextIO, TypeVar
from uuid import uuid4

_P = ParamSpec("_P")
_R = TypeVar("_R")
_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("research_log_context", default={})
_SETUP_LOCK = RLock()
_HANDLER_TAG = "_deep_research_handler"
_ERROR_TAG = "_deep_research_error_id"
_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "logs"
_BUILTIN_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_CONTEXT_FIELDS = (
    "run_id", "execution_id", "task_id", "task_position", "task_count",
    "stage", "research_stage", "checkpoint", "agent",
)
_COMPONENTS: dict[str, tuple[str, str]] = {
    "rag.pipeline": ("🌐", "rag.web"),
    "rag.retrieval": ("🧲", "rag.retrieve"),
    "rag.store": ("🗄️", "rag.store"),
    "rag.embeddings": ("⚡", "rag.embed"),
    "rag.timing": ("⏱️", "rag.timing"),
    "deep_researcher": ("🚀", "research"),
    "deep_researcher.task": ("📋", "research.task"),
    "services.planner": ("🧭", "services.planner"),
    "services.search": ("🔎", "services.search"),
    "services.query_rewriter": ("🔧", "services.query"),
    "services.summarizer": ("🧠", "services.summarizer"),
    "services.reporter": ("📝", "services.reporter"),
    "services.report_reflection": ("🪞", "services.review"),
    "graph.report_cycle": ("🔄", "graph.report_cycle"),
    "services.note_service": ("📒", "services.note"),
    "services.tool_events": ("🪝", "listeners.tools"),
    "agents.simple_agent": ("🤖", "agents.simple"),
    "agents.tool_aware_agent": ("🤖", "agents.tool_aware"),
    "llm.llm": ("🔌", "llm"),
    "tools.registry": ("🧰", "tools.registry"),
    "tools.search_tool": ("🧰", "tools.search"),
    "tools.note_tool": ("🧰", "tools.note"),
    "repository": ("🗄️", "storage.repository"),
    "graph_researcher": ("🚀", "research.graph"),
    "graph_nodes": ("🧭", "graph.nodes"),
    "graph.graph_researcher": ("🚀", "research.graph"),
    "graph.graph_nodes": ("🧭", "graph.nodes"),
    "services.evidence_service": ("🧾", "services.evidence"),
    "api.app": ("🌐", "api.app"),
    "api.manager": ("🧵", "api.manager"),
}
_PREFIXES = (
    "✅",
    "❌",
    "⚠️",
    "⚠",
    "🚀",
    "📋",
    "🧭",
    "🔎",
    "🔍",
    "🧠",
    "📝",
    "📒",
    "📖",
    "🤖",
    "🔌",
    "🧰",
    "🪝",
    "🗄️",
    "🧾",
    "👂",
    "🔧",
    "🧹",
    "📤",
    "🔄",
    "🌐",
    "🧩",
    "📂",
    "🗂️",
    "💾",
    "📄",
)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def new_run_id() -> str:
    """生成一轮研究的日志编号."""
    return "R" + uuid4().hex[:8].upper()


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """临时绑定上下文，退出或异常时恢复，隔离不同线程和异步任务."""
    token = _CONTEXT.set({**_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        # 只沿显式原因去重；处理原异常时发生的另一异常应单独记录。
        # formatException 仍会保留包含隐式 context 的完整堆栈。
        current = current.__cause__
    return chain


def log_exception_once(
    logger: logging.Logger,
    message: str,
    *args: object,
    error: BaseException,
    level: int = logging.ERROR,
    extra: dict[str, Any] | None = None,
) -> str:
    """同一异常向上传递时只记录一次完整堆栈，保留原始原因."""
    chain = _exception_chain(error)
    for item in chain:
        existing_id = getattr(item, _ERROR_TAG, None)
        if existing_id:
            return str(existing_id)
    error_id = "E" + uuid4().hex[:8].upper()
    for item in chain:
        setattr(item, _ERROR_TAG, error_id)
    logger.log(
        level,
        message,
        *args,
        exc_info=(type(error), error, error.__traceback__),
        extra={**(extra or {}), "error_id": error_id},
    )
    return error_id


def log_operation(
    stage: str,
    *,
    task_parameter: str | None = None,
    state_parameter: str | None = None,
    checkpoint: str | None = None,
    new_run: bool = False,
    report_errors: bool = True,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """为同步业务入口绑定阶段和任务，统一记录该入口的异常."""

    def decorate(func: Callable[_P, _R]) -> Callable[_P, _R]:
        signature = inspect.signature(func)
        operation_logger = logging.getLogger(func.__module__)

        @wraps(func)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            fields: dict[str, Any] = {"stage": stage}
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            if new_run or not _CONTEXT.get().get("run_id"):
                fields["run_id"] = new_run_id()
            if new_run:
                fields["task_id"] = None
            if task_parameter is not None:
                fields["task_id"] = bound.arguments[task_parameter]
            if state_parameter is not None:
                state = bound.arguments[state_parameter]
                # 独立运行服务 demo 时也使用实际研究编号，不使用入口临时编号。
                state_run_id = getattr(state, "run_id", None)
                if state_run_id:
                    fields["run_id"] = state_run_id
                if task_parameter is None:
                    fields["task_id"] = None
            if checkpoint is not None:
                fields["checkpoint"] = checkpoint
            if args and hasattr(args[0], "name"):
                fields["agent"] = args[0].name
            with log_context(**fields):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if report_errors:
                        log_exception_once(
                            operation_logger, "%s失败", stage, error=exc,
                            extra={"event": "operation_failed"},
                        )
                    raise

        return wrapped

    return decorate


def _clean_message(message: str) -> str:
    text = _ANSI.sub("", message).strip()
    while True:
        prefix = next((item for item in _PREFIXES if text.startswith(item)), None)
        if prefix is None:
            break
        text = text[len(prefix) :].lstrip()
    return re.sub(r"^\[DeepResearch(?:er)?\]\s*", "", text)


def _component(name: str) -> tuple[str, str]:
    short = name.removeprefix("deep_researcher.")
    if short in _COMPONENTS:
        return _COMPONENTS[short]
    if name == "__main__" or name.startswith("demo"):
        return "🖥️", "app"
    return "📦", short


def _display_run_id(value: object) -> str:
    """控制台显示短编号；JSON 日志仍保留完整编号."""
    text = str(value or "—")
    if re.fullmatch(r"[0-9a-f]{32}", text):
        return "R" + text[:8].upper()
    return text


def _pad_display(text: str, width: int) -> str:
    """按终端显示宽度补空格，避免中文阶段名称使分栏错位."""
    used = sum(
        0 if unicodedata.combining(char) or char in "\ufe0e\ufe0f"
        else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )
    if text.endswith("\ufe0f"):
        used = max(2, used)
    return text + " " * max(0, width - used)


class ContextFilter(logging.Filter):
    """将上下文写入记录，控制台和文件得到一致的追踪字段."""

    def filter(self, record: logging.LogRecord) -> bool:
        """填入未显式提供的字段，不覆盖调用者通过 extra 传入的值."""
        for key in _CONTEXT_FIELDS:
            if not hasattr(record, key):
                setattr(record, key, _CONTEXT.get().get(key))
        return True


class ConsoleFormatter(logging.Formatter):
    """方案 A：时间、等级、模块、研究编号、任务编号、操作与正文分栏."""

    def __init__(
        self,
        *,
        color: bool = False,
        emoji: bool = True,
        tracebacks: bool = False,
        encoding: str = "utf-8",
    ) -> None:
        """配置颜色、表情和异常详情，不改变原始日志记录."""
        super().__init__(datefmt="%H:%M:%S")
        self.color = color
        self.emoji = emoji
        self.tracebacks = tracebacks
        self.encoding = encoding

    def format(self, record: logging.LogRecord) -> str:
        """生成紧凑单行正文；错误根因和可选堆栈另起一行."""
        icon, component = _component(record.name)
        stamp = self.formatTime(record, self.datefmt)
        level = "WARN" if record.levelno == logging.WARNING else record.levelname
        run_id = _display_run_id(getattr(record, "run_id", None))
        task_id = getattr(record, "task_id", None)
        task = f"T{task_id:02d}" if type(task_id) is int else str(task_id or "—")
        stage = str(getattr(record, "stage", None) or "—")
        # 控制台使用短中文；JSON 日志仍保留原节点名称，便于程序统计。
        stage = {
            "reflect_report": "报告反思",
            "prepare_report_revision": "安排改进",
            "finalize_report": "报告定稿",
            "block_report": "停止交付",
        }.get(stage, stage)
        message = " ↵ ".join(_clean_message(record.getMessage()).splitlines())
        display_icon = icon if self.emoji else "-"
        label = f"{_pad_display(display_icon, 2)} {component:<21}"
        if self.color:
            code = (
                "31"
                if record.levelno >= logging.ERROR
                else "33"
                if record.levelno >= logging.WARNING
                else "90"
                if record.levelno <= logging.DEBUG
                else "36"
            )
            stamp = f"\x1b[90m{stamp}\x1b[0m"
            level = f"\x1b[{code}m{level:<5}\x1b[0m"
            label = f"\x1b[36m{label}\x1b[0m"
            message = f"\x1b[{code}m{message}\x1b[0m"
        else:
            level = f"{level:<5}"
        result = (
            f"{stamp} {level} {label} {run_id:<9} {task:<4} "
            f"{_pad_display(stage, 12)} │ {message}"
        )
        if record.exc_info and record.exc_info[1] is not None:
            root = _exception_chain(record.exc_info[1])[-1]
            reason = " ".join(_ANSI.sub("", str(root)).split())[:500]
            error_id = getattr(record, "error_id", "—")
            result += f"\n    └─ {type(root).__name__}: {reason} · error_id={error_id}"
            if self.tracebacks:
                result += "\n" + self.formatException(record.exc_info)
        return result.encode(self.encoding, errors="backslashreplace").decode(
            self.encoding
        )


class JsonFormatter(logging.Formatter):
    """每行保存一个 JSON 对象，保留完整模块、上下文和异常链."""

    def format(self, record: logging.LogRecord) -> str:
        """序列化记录，日志文件不包含控制台颜色转义符."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "module": record.name,
            "component": _component(record.name)[1],
            "message": _clean_message(record.getMessage()),
            "source": {"file": record.pathname, "line": record.lineno},
        }
        for key in _CONTEXT_FIELDS:
            payload[key] = getattr(record, key, None)
        reserved = _BUILTIN_FIELDS | set(payload) | {"exc_text"}
        for key, value in record.__dict__.items():
            if key not in reserved and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            root = _exception_chain(record.exc_info[1])[-1]
            payload["error"] = {
                "type": type(root).__name__,
                "message": str(root),
                "status_code": getattr(root, "status_code", None),
                "traceback": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


def _level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    level = getattr(logging, value.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"无效日志等级：{value}")
    return level


def _supports_color(stream: TextIO) -> bool:
    if os.getenv("NO_COLOR") is not None or not stream.isatty():
        return False
    if os.name != "nt":
        return True
    # Windows 控制台开启 VT 颜色；重定向输出保持纯文本。
    import ctypes
    import msvcrt

    try:
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        mode = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 0x0004)
        )
    except (OSError, ValueError, AttributeError):
        return False


def setup_logging(
    console_level: str | int | None = None,
    *,
    log_dir: str | Path | None = _DEFAULT_DIR,
    file_level: str | int = logging.DEBUG,
    color: bool | None = None,
    stream: TextIO | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> Path | None:
    """配置应用日志；重复调用替换自有 Handler，保留外部框架的 Handler.

    默认控制台 INFO、文件 DEBUG。DEEP_RESEARCH_LOG_LEVEL 和
    DEEP_RESEARCH_LOG_DIR 可覆盖默认值；log_dir=None 关闭文件输出。
    """
    console_number = _level(
        console_level
        if console_level is not None
        else os.getenv("DEEP_RESEARCH_LOG_LEVEL", "INFO")
    )
    file_number = _level(file_level)
    active_stream = stream if stream is not None else sys.stderr
    encoding = active_stream.encoding or "utf-8"
    try:
        "🚀🧭🔎🧠📝📒🤖🔌🧰🪝🗄️".encode(encoding)
        emoji = True
    except UnicodeEncodeError:
        emoji = False

    console = logging.StreamHandler(active_stream)
    console.setLevel(console_number)
    console.addFilter(ContextFilter())
    console.setFormatter(
        ConsoleFormatter(
            color=_supports_color(active_stream) if color is None else color,
            emoji=emoji,
            tracebacks=console_number <= logging.DEBUG,
            encoding=encoding,
        )
    )
    handlers: list[logging.Handler] = [console]
    log_path: Path | None = None
    if log_dir is not None:
        directory = Path(
            os.getenv("DEEP_RESEARCH_LOG_DIR", str(log_dir))
            if log_dir == _DEFAULT_DIR
            else log_dir
        ).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "research.jsonl"
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(file_number)
        file_handler.addFilter(ContextFilter())
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)

    with _SETUP_LOCK:
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if getattr(handler, _HANDLER_TAG, False):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(min(console_number, file_number) if log_path else console_number)
        for handler in handlers:
            setattr(handler, _HANDLER_TAG, True)
            root.addHandler(handler)
        logging.getLogger("deep_researcher").setLevel(logging.DEBUG)
        for name in ("httpx", "httpcore", "openai", "urllib3", "ddgs"):
            logging.getLogger(name).setLevel(logging.WARNING)
    return log_path


def run_cli(main: Callable[[], None]) -> None:
    """运行 demo 入口，统一配置并避免未处理异常再次打印重复堆栈."""
    setup_logging()
    with log_context(run_id=new_run_id(), stage="入口"):
        try:
            main()
        except Exception as exc:
            log_exception_once(
                logging.getLogger(main.__module__),
                "程序退出 · 执行失败",
                error=exc,
            )
            raise SystemExit(1) from None
