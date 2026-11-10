from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


RunStatus = Literal[
    "pending",
    "running",
    "failed",
    "completed",
]

@dataclass(frozen=True)
class RunInfo:
    """一次研究的启动信息及运行状态。"""

    run_id: str
    topic: str
    config: dict[str, Any]
    max_tasks: int | None
    status: RunStatus
    last_error: str | None
    updated_at:str=""


class GraphRunRepository:
    """图信息数据库"""
    def __init__(self,path:Path)->None:
        path.parent.mkdir(exist_ok=True,parents=True)
        self._db = sqlite3.connect(
            database=path,timeout=10
        )
        self._db.row_factory = sqlite3.Row

        with self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS graph_runs (
                    run_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL   
                )"""
            )
    @staticmethod
    def _now()->str:
        return datetime.now(timezone.utc).isoformat()

    def create(
            self,
            run_id:str,
            topic:str,
            config:dict[str,Any],
            max_tasks:int|None
    )->None:
        """在创建新数据时调用，只构造数据表中data和run_id字段"""
        data = json.dumps(
            {
                "version":1,
                "topic":topic,
                "config":config,
                "max_tasks":max_tasks
            },
            ensure_ascii=False
        )
        with self._db:
            self._db.execute(
                """
                INSERT INTO graph_runs
                VALUES (?,?,"pending",NULL,?)
""",
                (run_id,data,self._now()),
            )

    def load(self,run_id:str)->RunInfo:
        """从数据库中读取数据并以上述类返回"""
        row = self._db.execute(
            "SELECT * FROM graph_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()

        if row is None:
            raise KeyError(f"研究不存在：{run_id}")
        return  self._to_info(row)
    
    @staticmethod
    def _to_info(row:sqlite3.Row)->RunInfo:
        data = json.loads(row["data"])

        if data["version"] != 1:
            raise ValueError("运行元数据版本不兼容")

        return RunInfo(
            run_id=row["run_id"],
            topic=data["topic"],
            config=data["config"],
            max_tasks=data["max_tasks"],
            status=row["status"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    def list_runs(self,limit:int,offset:int)->list[RunInfo]:
        rows = self._db.execute(
            """SELECT * FROM graph_runs 
            ORDER BY updated_at DESC,run_id DESC
            LIMIT ? OFFSET ?""",
            (limit,offset),
        ).fetchall()
        return [self._to_info(row) for row in rows]
    

    

    def update(self,run_id:str,status:RunStatus,last_error:str|None=None)->None:
        """负责更新status和lats_error字段"""
        with self._db:
            cursor = self._db.execute(
                """UPDATE graph_runs 
                   SET status=?,last_error=?,updated_at=?
                   WHERE run_id=?
                
                """,
                (status,last_error,self._now(),run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"研究不存在：{run_id}"
                )

    def close(self) -> None:
        self._db.close()
