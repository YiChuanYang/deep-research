"""独立保存候选原文、选择理由和证据映射，不膨胀图检查点。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class SelectionAuditStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def save(self, run_id: str, task_id: int, audit: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每次操作独立连接；closing 关闭连接，with db 提交或回滚事务。
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS selection_audits (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, task_id INTEGER NOT NULL, payload TEXT NOT NULL
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS ix_selection_run ON selection_audits(run_id)")
                db.execute("INSERT INTO selection_audits(run_id, task_id, payload) VALUES (?, ?, ?)",
                           (run_id, task_id, json.dumps(audit, ensure_ascii=False, allow_nan=False)))

    def read_run(self, run_id: str) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='selection_audits'").fetchone() is None:
                return []
            rows = db.execute(
                "SELECT audit_id, task_id, payload FROM selection_audits WHERE run_id=? ORDER BY audit_id",
                (run_id,),
            ).fetchall()
        return [{"audit_id": row[0], "task_id": row[1], "audit": json.loads(row[2])} for row in rows]

    def delete_run(self, run_id: str) -> None:
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='selection_audits'").fetchone() is None:
                return
            with db:
                db.execute("DELETE FROM selection_audits WHERE run_id=?", (run_id,))
