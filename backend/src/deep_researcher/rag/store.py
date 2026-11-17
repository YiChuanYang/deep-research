"""每份研究独立的资料库；图检查点只保存统计和证据关联。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class PageSnapshot:
    snapshot_id: str
    url: str
    title: str
    text: str
    retrieved_at: str
    kind: str
    content_hash: str
    truncated: bool = False


@dataclass(frozen=True, kw_only=True)
class TextChunk:
    chunk_id: str
    snapshot_id: str
    text: str
    section: str
    start: int
    end: int


class WebRagStore:
    """固定键写入是幂等的；事务提交后才能让图节点返回。"""

    def __init__(self, workspace: str | Path, run_id: str) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise ValueError("网页资料库需要完整的研究编号")
        self.path = Path(workspace).expanduser().resolve() / run_id / "corpus.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, url TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, page_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chunk_page ON chunks(page_id);
                CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def record(self, key: str) -> Any:
        with closing(self._connect()) as db:
            row = db.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_record(self, key: str, value: Any) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?, ?)",
                       (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def save_page(self, page: PageSnapshot) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO pages VALUES (?, ?, ?)",
                       (page.snapshot_id, page.url, json.dumps(asdict(page), ensure_ascii=False)))

    def pages(self, ids: list[str] | None = None) -> list[PageSnapshot]:
        return [PageSnapshot(**json.loads(data)) for data in self._read_rows("pages", ids)]

    def save_chunks(self, chunks: list[TextChunk]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR IGNORE INTO chunks VALUES (?, ?, ?)", [
                (c.chunk_id, c.snapshot_id, json.dumps(asdict(c), ensure_ascii=False)) for c in chunks
            ])

    def chunks(self, page_ids: list[str] | None = None) -> list[TextChunk]:
        return [TextChunk(**json.loads(data)) for data in self._read_rows("chunks", page_ids)]

    def _read_rows(self, table: Literal["pages", "chunks"], ids: list[str] | None) -> list[str]:
        """在 SQL 中筛选，分批查询后按 rowid 还原原有稳定顺序。"""
        column = {"pages": "id", "chunks": "page_id"}[table]
        if ids == []:
            return []
        with closing(self._connect()) as db:
            if ids is None:
                return [row[0] for row in db.execute(f"SELECT data FROM {table} ORDER BY rowid")]
            unique_ids = list(dict.fromkeys(ids))
            rows: list[tuple[int, str]] = []
            # 参数数量保持低于旧 SQLite 的 999 上限；值始终通过占位符绑定。
            for offset in range(0, len(unique_ids), 400):
                group = unique_ids[offset:offset + 400]
                marks = ",".join("?" for _ in group)
                rows.extend(db.execute(
                    f"SELECT rowid, data FROM {table} WHERE {column} IN ({marks})", group,
                ).fetchall())
        return [data for _, data in sorted(rows)]

    def vectors(self, keys: list[str]) -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        unique_keys = list(dict.fromkeys(keys))
        with closing(self._connect()) as db:
            for offset in range(0, len(unique_keys), 400):
                group = unique_keys[offset:offset + 400]
                marks = ",".join("?" for _ in group)
                rows = db.execute(f"SELECT key, data FROM vectors WHERE key IN ({marks})", group)
                result.update((key, json.loads(data)) for key, data in rows)
        return result

    def save_vectors(self, values: dict[str, list[float]]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)", [
                (key, json.dumps(value, allow_nan=False)) for key, value in values.items()
            ])

    def describe(self, snapshot_id: str) -> PageSnapshot:
        pages = self.pages([snapshot_id])
        if not pages:
            raise KeyError("网页快照不存在")
        return pages[0]
