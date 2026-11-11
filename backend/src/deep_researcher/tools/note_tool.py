"""按研究编号读取、保存完整笔记。"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4
import re
from pydantic import BaseModel, ConfigDict, Field

from deep_researcher.config import Configuration
from deep_researcher.tools.base import Tool, ToolParameter

logger = logging.getLogger(__name__)


class NoteKey(BaseModel):
    """同一次研究中的一个任务，或该研究的最终报告。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    task_id: int | None = Field(default=None, gt=0)

    @property
    def note_id(self) -> str:
        suffix = (
            f"task_{self.task_id}"
            if self.task_id is not None
            else "report"
        )
        return f"note_{self.run_id}_{suffix}"

    @property
    def note_type(self) -> str:
        return (
            "task_state"
            if self.task_id is not None
            else "conclusion"
        )


class Note(NoteKey):
    """笔记正文；文件路径仅用于返回，不写入 JSON。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    note_path: str = Field(default="", exclude=True)


class NoteTool(Tool):
    """统一提供 read/save；save 始终保存完整正文。"""

    def __init__(self, config: Configuration) -> None:
        super().__init__(
            name="note",
            description="读取或保存完整研究笔记",
        )

        self._workspace = (
            Path(config.notes_workspace)
            .expanduser()
            .resolve()
        )
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def read(
        self,
        *,
        run_id: str,
        task_id: int | None = None,
    ) -> Note | None:
        """没有文件返回 None；损坏或归属错误继续抛异常。"""
        key = NoteKey(run_id=run_id, task_id=task_id)

        with self._lock:
            note = self._read(key)

        logger.debug(
            "笔记读取 · %s", "命中" if note is not None else "尚未创建",
            extra={
                "event": "note_read",
                "run_id": run_id,
                "task_id": task_id,
                "note_id": key.note_id,
                "note_path": str(self._path(key)),
                "found": note is not None,
            },
        )
        return note

    def save(
        self,
        *,
        run_id: str,
        task_id: int | None = None,
        title: str,
        content: str,
    ) -> Note:
        """固定编号、完整替换，重复保存不会创建第二份笔记。"""
        note = Note(
            run_id=run_id,
            task_id=task_id,
            title=title,
            content=content,
        )

        with self._lock:
            # 已有文件必须合法，避免静默覆盖损坏的数据。
            previous = self._read(note)
            self._write(note)

        logger.debug(
            "笔记文件已%s", "创建" if previous is None else "替换",
            extra={
                "event": "note_written",
                "run_id": run_id,
                "task_id": task_id,
                "note_id": note.note_id,
                "note_path": str(self._path(note)),
                "note_created": previous is None,
                "content_chars": len(note.content),
            },
        )

        return note.model_copy(
            update={"note_path": str(self._path(note))}
        )

    def _path(self, key: NoteKey) -> Path:
        return self._workspace / f"{key.note_id}.json"

    def _read(self, key: NoteKey) -> Note | None:
        path = self._path(key)

        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

        note = Note.model_validate_json(text)

        if (
            note.run_id != key.run_id
            or note.task_id != key.task_id
        ):
            raise ValueError(
                "笔记文件与当前研究或任务不匹配"
            )

        return note.model_copy(
            update={"note_path": str(path)}
        )

    def _write(self, note: Note) -> None:
        """先写临时文件，再原子替换正式文件。"""
        path = self._path(note)
        temporary = path.with_name(
            f".{path.name}.{uuid4().hex}.tmp"
        )

        try:
            temporary.write_text(
                note.model_dump_json(indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """Tool 协议适配入口；服务层直接调用 read/save。"""
        action = parameters.get("action")
        key = NoteKey(
            run_id=parameters.get("run_id"),
            task_id=parameters.get("task_id"),
        )

        if action == "read":
            note = self.read(**key.model_dump())

            if note is None:
                raise FileNotFoundError(
                    f"笔记不存在：{key.note_id}"
                )

        elif action == "save":
            note = self.save(
                **key.model_dump(),
                title=parameters.get("title"),
                content=parameters.get("content"),
            )

        else:
            raise ValueError("action 必须是 read/save")

        return {
            **note.model_dump(),
            "action": action,
            "note_id": note.note_id,
            "note_type": note.note_type,
            "note_path": note.note_path,
        }

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="read 读取，save 保存完整正文",
            ),
            ToolParameter(
                name="run_id",
                type="string",
                description="研究编号，32 位小写十六进制 UUID",
            ),
            ToolParameter(
                name="task_id",
                type="integer",
                description="任务编号；报告笔记不传",
                required=False,
            ),
            ToolParameter(
                name="title",
                type="string",
                description="save 时必填：笔记标题",
                required=False,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="save 时必填：完整正文",
                required=False,
            ),
        ]

    @staticmethod
    def delete_run_notes(
          workspace: Path,
          run_id: str,
      ) -> int:
          """删除指定研究的笔记及遗留临时文件，返回删除文件数。"""
          if re.fullmatch(
              r"[0-9a-f]{32}",
              run_id,
          ) is None:
              raise ValueError("研究编号格式不正确")

          if not workspace.is_absolute():
              raise ValueError(
                  "删除笔记需要历史配置中的绝对目录"
              )

          root = workspace.resolve()

          if not root.exists():
              return 0

          if not root.is_dir():
              raise ValueError(
                  "笔记工作目录不是文件夹"
              )

          stem = (
              rf"note_{run_id}_"
              rf"(?:report|task_[1-9]\d*)\.json"
          )

          pattern = re.compile(
              rf"(?:{stem}|\.{stem}\.[0-9a-f]{{32}}\.tmp)"
          )

          targets = [
              path
              for path in root.iterdir()
              if pattern.fullmatch(path.name)
          ]

          # 先验证全部候选文件，再开始删除。
          for path in targets:
              if (
                  path.is_symlink()
                  or path.resolve().parent != root
                  or not path.is_file()
              ):
                  raise ValueError(
                      f"笔记路径异常，停止删除：{path.name}"
                  )

          for path in targets:
              path.unlink(missing_ok=True)

          return len(targets)
