"""提供结构化返回值和原子文件写入的笔记工具."""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Literal, TypedDict, cast

from deep_researcher.config import Configuration, get_config
from deep_researcher.tools.base import Tool, ToolParameter

logger = logging.getLogger(__name__)

NoteAction = Literal["create", "read", "update"]


class NoteRecord(TypedDict):
    """笔记文件中保存的字段."""

    note_id: str
    task_id: int | None
    title: str
    note_type: str
    tags: list[str]
    content: str


class NoteResult(NoteRecord):
    """返回给 Agent、监听器或服务层的笔记结果."""

    action: NoteAction
    note_path: str


class NoteTool(Tool):
    """创建、读取和更新笔记，支持追加记录与替换完整正文."""

    def __init__(
        self,
        name: str = "note",
        description: str = "笔记工具，支持创建、读取和修改笔记",
        config: Configuration | None = None,
    ) -> None:
        """初始化配置、存储目录和同一实例的读写锁."""
        super().__init__(name, description)
        self._config = config or get_config()
        self._workspace = Path(self._config.notes_workspace).resolve()
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        logger.debug(
            "📝 笔记工具已初始化：workspace=%s",
            self._workspace,
        )

    def run(self, parameters: dict[str, Any]) -> NoteResult:
        """校验参数、执行读写，返回包含任务关联的完整笔记."""
        if not isinstance(parameters, dict):
            raise TypeError("parameters 必须是字典")

        action = parameters.get("action")
        if action not in ("create", "read", "update"):
            raise ValueError("action 必须是 create/read/update")

        logger.debug(
            "📝 笔记操作开始：action=%s note_id=%s task_id=%s",
            action,
            parameters.get("note_id"),
            parameters.get("task_id"),
        )
        try:
            # 同一个工具实例上的读取、修改和保存保持连续。
            with self._lock:
                if action == "create":
                    data = self._create(parameters)
                elif action == "read":
                    data = self._read(parameters.get("note_id"))
                else:
                    data = self._update(parameters)

                result: NoteResult = {
                    **data,
                    "action": cast(NoteAction, action),
                    "note_path": str(self._note_path(data["note_id"])),
                }
        except FileNotFoundError:
            logger.debug(
                "⚠️ 笔记文件不存在：action=%s note_id=%s",
                action,
                parameters.get("note_id"),
            )
            raise
        except Exception:
            logger.debug(
                "❌ 笔记操作失败：action=%s note_id=%s",
                action,
                parameters.get("note_id"),
            )
            raise

        logger.debug(
            "✅ 笔记操作完成：action=%s note_id=%s task_id=%s "
            "mode=%s content_chars=%d path=%s",
            action,
            result["note_id"],
            result["task_id"],
            parameters.get("mode", "append") if action == "update" else "-",
            len(result["content"]),
            result["note_path"],
        )
        return result

    @staticmethod
    def _require_text(value: object, field: str) -> str:
        """校验必填文本，拒绝 None、空字符串及非文本值."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _validate_task_id(value: object) -> int | None:
        """允许报告笔记不关联任务，任务编号必须是正整数."""
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise ValueError("task_id 必须是正整数或 None")
        return value

    @staticmethod
    def _validate_tags(value: object) -> list[str]:
        """复制标签列表，避免调用方后续修改影响本次结果."""
        if not isinstance(value, list) or not all(
            isinstance(tag, str) for tag in value
        ):
            raise TypeError("tags 必须是字符串列表")
        return list(value)

    def _note_path(self, note_id: object) -> Path:
        """根据有效笔记编号定位文件，拒绝路径形式的编号."""
        if (
            not isinstance(note_id, str)
            or re.fullmatch(r"note_[0-9a-f]{12}", note_id) is None
        ):
            raise ValueError("note_id 格式不正确")
        return self._workspace / f"{note_id}.json"

    def _create(self, parameters: dict[str, Any]) -> NoteRecord:
        """首次创建时写入实际正文，不接受空的占位笔记."""
        data: NoteRecord = {
            "note_id": f"note_{uuid.uuid4().hex[:12]}",
            "task_id": self._validate_task_id(parameters.get("task_id")),
            "title": self._require_text(parameters.get("title", "未命名笔记"), "title"),
            "note_type": self._require_text(
                parameters.get("note_type", "task_state"), "note_type"
            ),
            "tags": self._validate_tags(parameters.get("tags", ["deep_research"])),
            "content": self._require_text(parameters.get("content"), "content"),
        }
        self._save(data)
        return data

    def _read(self, note_id: object) -> NoteRecord:
        """读取并验证现有记录，文件缺失和损坏均向上抛出."""
        path = self._note_path(note_id)
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("note_id") != note_id:
            raise ValueError("笔记文件内容与 note_id 不匹配")
        if not isinstance(raw.get("content"), str):
            raise ValueError("笔记 content 必须是字符串")

        # 允许读取旧版的空正文，之后可由服务层提交真实摘要。
        return NoteRecord(
            note_id=raw["note_id"],
            task_id=self._validate_task_id(raw.get("task_id")),
            title=self._require_text(raw.get("title"), "title"),
            note_type=self._require_text(raw.get("note_type"), "note_type"),
            tags=self._validate_tags(raw.get("tags", [])),
            content=raw["content"],
        )

    def _update(self, parameters: dict[str, Any]) -> NoteRecord:
        """使用 append 追加工作记录，replace 保存最新完整摘要."""
        data = self._read(parameters.get("note_id"))
        if "task_id" in parameters:
            task_id = self._validate_task_id(parameters["task_id"])
            if task_id != data["task_id"]:
                raise ValueError("笔记不属于当前任务")
        if "note_type" in parameters and parameters["note_type"] != data["note_type"]:
            raise ValueError("不能通过更新改变笔记类型")

        content = self._require_text(parameters.get("content"), "content")
        mode = parameters.get("mode", "append")
        if mode not in ("append", "replace"):
            raise ValueError("mode 必须是 append/replace")

        old_content = data["content"].strip()
        if mode == "replace" or not old_content:
            data["content"] = content
        else:
            data["content"] = f"{old_content}\n\n---\n{content}"

        if "title" in parameters:
            data["title"] = self._require_text(parameters["title"], "title")
        if "tags" in parameters:
            data["tags"] = self._validate_tags(parameters["tags"])

        self._save(data)
        return data

    def _save(self, data: NoteRecord) -> None:
        """在同一目录写临时文件，完整写入后原子替换正式文件."""
        path = self._note_path(data["note_id"])
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_parameters(self) -> list[ToolParameter]:
        """声明模型可用参数，按 action 的必填校验在运行时执行."""
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作：create 创建、read 读取、update 更新",
                required=True,
            ),
            ToolParameter(
                name="note_id",
                type="string",
                description="笔记唯一 ID，read/update 时必填",
                required=False,
            ),
            ToolParameter(
                name="task_id",
                type="integer",
                description="关联的任务编号，创建任务笔记时填写",
                required=False,
            ),
            ToolParameter(
                name="title",
                type="string",
                description="笔记标题",
                required=False,
            ),
            ToolParameter(
                name="note_type",
                type="string",
                description="笔记类型：task_state 任务笔记或 conclusion 报告笔记",
                required=False,
            ),
            ToolParameter(
                name="tags",
                type="array",
                description="标签列表，任务笔记包含 deep_research 和 task_编号",
                required=False,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="实际笔记正文，create/update 时必填；不能填写操作说明",
                required=False,
            ),
            ToolParameter(
                name="mode",
                type="string",
                description="仅 update 使用：append 追加记录，replace 替换完整正文",
                required=False,
                default="append",
            ),
        ]
