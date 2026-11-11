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
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS graph_runs (
                    run_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL   
                )"""
            )
            columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(graph_runs)").fetchall()
            }
            if "deleted_at" not in columns:
                self._db.execute("ALTER TABLE graph_runs"
                "                 ADD COLUMN deleted_at TEXT")


    @staticmethod
    def _now()->str:
        return datetime.now(timezone.utc).isoformat()

    def create(
          self,
          run_id: str,
          topic: str,
          config: dict[str, Any],
          max_tasks: int | None,
      ) -> None:
          data = json.dumps(
              {
                  "version": 1,
                  "topic": topic,
                  "config": config,
                  "max_tasks": max_tasks,
              },
              ensure_ascii=False,
          )

          with self._db:
              self._db.execute(
                  """
                  INSERT INTO graph_runs (
                      run_id,
                      data,
                      status,
                      last_error,
                      updated_at,
                      deleted_at
                  )
                  VALUES (?, ?, 'pending', NULL, ?, NULL)
                  """,
                  (run_id, data, self._now()),
              )

    def load(
          self,
          run_id: str,
          *,
          include_deleted: bool = False,
      ) -> RunInfo:
          row = self._db.execute(
              """
              SELECT *
              FROM graph_runs
              WHERE run_id = ?
                AND (? OR deleted_at IS NULL)
              """,
              (run_id, include_deleted),
          ).fetchone()

          if row is None:
              raise KeyError(f"研究不存在：{run_id}")

          return self._to_info(row)
    
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
            WHERE deleted_at IS NULL
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
                   AND deleted_at IS NULL
                
                """,
                (status,last_error,self._now(),run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"研究不存在：{run_id}"
                )

    def mark_deleted(self, run_id: str) -> None:
          """先隐藏研究并禁止恢复，保留清理所需的配置。"""
          with self._db:
              cursor = self._db.execute(
                  """
                  UPDATE graph_runs
                  SET deleted_at = COALESCE(deleted_at, ?)
                  WHERE run_id = ?
                  """,
                  (self._now(), run_id),
              )

              if cursor.rowcount != 1:
                  raise KeyError(
                      f"研究不存在：{run_id}"
                  )

    def purge_deleted(self, run_id: str) -> None:
          """关联数据清理成功后，最后删除元数据。"""
          with self._db:
              cursor = self._db.execute(
                  """
                  DELETE FROM graph_runs
                  WHERE run_id = ?
                    AND deleted_at IS NOT NULL
                  """,
                  (run_id,),
              )

              if cursor.rowcount != 1:
                  raise KeyError(
                      f"没有待清理的研究：{run_id}"
                  )

    def close(self) -> None:
        self._db.close()
