"""保存研究过程事件，并为 SSE 提供按编号补发能力。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResearchEvent:
    """
    一条研究事件。

    event_id 是全局递增编号，用于断线重连后补发。
    run_id 用于区分不同研究。
    execution_id 用于区分首次执行和恢复执行。
    """

    event_id: int
    run_id: str
    execution_id: str
    type: str
    created_at: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """
        将 dataclass 转成普通字典。

        asdict() 会递归展开 dataclass，
        返回值可以直接交给 json.dumps()。
        """
        return asdict(self)


class EventStore:
    """研究事件的 SQLite 存储。"""

    def __init__(self, path: Path) -> None:
        # 这里只保存路径，不立即创建数据库。
        # 这样构造对象不会产生副作用。
        self._path = path

    def initialize(self) -> None:
        """
        创建事件表。

        返回值是 None。
        数据库结构变化通过文件副作用完成。
        """
        self._path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with closing(
            sqlite3.connect(
                self._path,
                timeout=10,
            )
        ) as db:
            # WAL 允许读取者和写入者更好地并行工作。
            db.execute("PRAGMA journal_mode=WAL")

            with db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS research_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        execution_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        data TEXT NOT NULL
                    )
                    """
                )

                db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_events_run_id
                    ON research_events(run_id, event_id)
                    """
                )

    def append(
        self,
        *,
        run_id: str,
        execution_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """
        写入一条事件。

        每次创建独立 SQLite 连接，避免跨线程复用连接。
        with db 负责提交或回滚事务。
        closing(db) 负责关闭连接。
        """
        with closing(
            sqlite3.connect(
                self._path,
                timeout=10,
            )
        ) as db:
            with db:
                db.execute(
                    """
                    INSERT INTO research_events (
                        run_id,
                        execution_id,
                        type,
                        created_at,
                        data
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        execution_id,
                        event_type,
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                        json.dumps(
                            data,
                            ensure_ascii=False,
                        ),
                    ),
                )

    def read_after(
        self,
        *,
        run_id: str,
        after_event_id: int,
        limit: int = 200,
    ) -> list[ResearchEvent]:
        """
        读取指定编号之后的事件。

        返回值是 list[ResearchEvent]。
        如果数据库不存在或没有事件，返回空列表。
        """
        if not self._path.is_file():
            return []

        # mode=ro 表示只读打开。
        # 只读模式不会因为路径不存在而创建数据库。
        uri = (
            f"{self._path.resolve().as_uri()}"
            "?mode=ro"
        )

        with closing(
            sqlite3.connect(
                uri,
                uri=True,
                timeout=10,
            )
        ) as db:
            db.row_factory = sqlite3.Row

            table_exists = db.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'research_events'
                """
            ).fetchone()

            if table_exists is None:
                return []

            rows = db.execute(
                """
                SELECT
                    event_id,
                    run_id,
                    execution_id,
                    type,
                    created_at,
                    data
                FROM research_events
                WHERE run_id = ?
                  AND event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (
                    run_id,
                    after_event_id,
                    limit,
                ),
            ).fetchall()

        return [
            ResearchEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                execution_id=row["execution_id"],
                type=row["type"],
                created_at=row["created_at"],
                data=json.loads(row["data"]),
            )
            for row in rows
        ]

    def delete_run(self, run_id: str) -> None:
        """删除指定研究的全部事件。"""
        if not self._path.is_file():
            return

        with closing(
            sqlite3.connect(
                self._path,
                timeout=10,
            )
        ) as db:
            with db:
                table_exists = db.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name = 'research_events'
                    """
                ).fetchone()

                if table_exists is None:
                    return

                db.execute(
                    """
                    DELETE FROM research_events
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )

    @staticmethod
    def sse_message(event: ResearchEvent) -> str:
        """
        将事件编码成 SSE 文本。

        SSE 的一条消息由多行组成，空行表示消息结束：
            id: ...
            event: ...
            data: ...

        返回值是浏览器 EventSource 可以直接读取的字符串。
        """
        payload = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

        # 所有事件统一使用 research.event。
        # 真正的业务事件类型放在 JSON 的 type 字段中。
        return (
            f"id: {event.event_id}\n"
            "event: research.event\n"
            f"data: {payload}\n\n"
        )
