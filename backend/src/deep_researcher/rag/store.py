"""每份研究独立的资料库；图检查点只保存统计和证据关联。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
        with closing(self._connect()) as db:
            rows = db.execute("SELECT data FROM pages ORDER BY rowid").fetchall()
        wanted = set(ids) if ids is not None else None
        pages = [PageSnapshot(**json.loads(row[0])) for row in rows]
        return [p for p in pages if wanted is None or p.snapshot_id in wanted]

    def save_chunks(self, chunks: list[TextChunk]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR IGNORE INTO chunks VALUES (?, ?, ?)", [
                (c.chunk_id, c.snapshot_id, json.dumps(asdict(c), ensure_ascii=False)) for c in chunks
            ])

    def chunks(self, page_ids: list[str] | None = None) -> list[TextChunk]:
        wanted = set(page_ids) if page_ids is not None else None
        with closing(self._connect()) as db:
            rows = db.execute("SELECT data FROM chunks ORDER BY rowid").fetchall()
        chunks = [TextChunk(**json.loads(row[0])) for row in rows]
        return [c for c in chunks if wanted is None or c.snapshot_id in wanted]

    def vectors(self, keys: list[str]) -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        with closing(self._connect()) as db:
            for key in keys:
                row = db.execute("SELECT data FROM vectors WHERE key=?", (key,)).fetchone()
                if row:
                    result[key] = json.loads(row[0])
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
