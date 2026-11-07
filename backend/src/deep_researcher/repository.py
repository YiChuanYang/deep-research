from pathlib import Path
from typing import  Literal,Optional
import logging
from deep_researcher.models import ResearchRecord,SummaryState,TodoItem
import sqlite3
from datetime import datetime,timezone
from dataclasses import asdict
import json
import logging 
logger = logging.getLogger(__name__)
from uuid import uuid4

#Path(__file__)是当前执行的这个py文件的路径
DEFAULT_DB = (
    Path(__file__).resolve().parents[2]
    /"data"/"research.sqlite3"
)
class RunBusyError(RuntimeError):
    """研究已被其它执行者占用"""

class RunOwnerShipError(RuntimeError):
    """执行权失效"""



class ResearchRepository:
    """研究存储层"""
    def __init__(self,database:str|Path=DEFAULT_DB)->None:
        "内存存储，适合测试"
        if str(database)==":memory:":
            target = ":memory:"
        else:
            path = Path(database).expanduser().resolve()
            path.parent.mkdir(exist_ok=True,parents=True)
            target=str(path)

        self._db = sqlite3.connect(target,timeout=10)
        self._db.row_factory = sqlite3.Row

        #建表
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                        CHECK(
                            status IN (
                                'pending',
                                'running',
                                'failed',
                                'completed'
                            )
                        ),
                    record TEXT NOT NULL,
                    owner_token TEXT,
                    update_at TEXT NOT NULL
                )
            """)


    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )

    @staticmethod
    def _encode(record:ResearchRecord)->str:
        """将ResearchRecord转换成文本,方便进入数据库中的record字段
        """
        return json.dumps({
            "schema_version":1,
            "state":asdict(record.state),
            "config":record.config,
            "max_tasks":record.max_tasks,
            "stage":record.stage,
            "last_error":record.last_error
        },
        ensure_ascii=False,)
    @staticmethod
    def _decode(row:sqlite3.Row)->ResearchRecord:
        """将数据库搜索结果返回到record类"""
        data = json.loads(row["record"])
        if int(data.get("schema_version"))!=1:
            raise ValueError(
                "不支持该研究快照版本，需要先执行数据迁移"
            )

        if data["stage"] not in (
            "plan", "tasks", "report", "save_report", "done"
        ):
            raise ValueError("研究阶段无效")
        #从字典中获取state,此时state还是字典类型
        raw_state = data.get("state")
        raw_state["todo_items"] = [
            TodoItem(**item) for item in raw_state["todo_items"]
        ]
        #json对象会变成字符串，需要恢复state中search_contexts的类型为int的键
        raw_state["search_contexts"]={
            int(key):value 
            for key,value in raw_state["search_contexts"].items()
        }

        for task in raw_state["todo_items"]:
            if task.checkpoint not in (
                "search", "summarize", "save_note", "done"
            ):
                raise ValueError(
                    f"任务 {task.id} 的检查点无效"
                )

        state = SummaryState(**raw_state)
        if state.run_id != row["run_id"]:
            raise ValueError("快照编号与数据库记录不一致")
        return ResearchRecord(
            state=state,
            config=data["config"],
            max_tasks=data["max_tasks"],
            stage=data["stage"],
            status=row["status"],
            last_error=str(data["last_error"]),

        )

    def create(self,record:ResearchRecord)->None:
        """创建研究；重复 run_id 抛错，避免覆盖已有记录。"""
        if not record.state.run_id or record.status != "pending":
            raise ValueError(
                "新研究必须有 run_id，且状态必须为 pending"
            )

        with self._db:
            self._db.execute(
                """
                INSERT INTO research_runs
                VALUES (?,?,?,NULL,?)
""",        (record.state.run_id,
             record.status,
             self._encode(record),
             self._now()
     ),
            )
        logger.info(
            "研究记录已创建 · stage=%s",
            record.stage,
        )

    def load(self,run_id:str)->ResearchRecord:
        """读取"""
        row = self._db.execute(
            "SELECT * FROM research_runs WHERE run_id = ?",
            (run_id,)
        ).fetchone()

        if not row:
            raise KeyError(f"{run_id}研究不存在")
        return self._decode(row)


    
    def claim(
            self,
            run_id:str,
            *,
            recover_interrupted:bool=False)->ResearchRecord:
        """取得原子执行权，在此生成owner_token令牌,只负责更新状态和令牌
        Args:
            recover_interrupted:在取值为True时会使程序生成新令牌
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record =self.load(run_id)

            if record.status == "completed":
                return record
            if (
                record.status =="running" 
                and 
                not recover_interrupted
            ):
                raise RunBusyError("当前并非从断点恢复且该研究正在被占用")

            #生成令牌
            record.owner_token = uuid4().hex
            self._db.execute("""
                UPDATE research_runs 
                SET status='running',
                    owner_token=?,
                    update_at=?
                WHERE run_id=?
""",(record.owner_token,self._now(),run_id),)

        record.status = 'running'
        return record

    def update(self,
               record:ResearchRecord,
               status:Literal["pending","running","failed","completed"]="running")->None:
        """基于已有执行权更新"""
        if not record.owner_token:
            raise RunOwnerShipError("无执行权，请先claim")

        record_text = self._encode(record)
        #进入更新
        with self._db:
            cursor = self._db.execute("""
                UPDATE research_runs
                SET status=?,
                    record=?,
                    update_at=?
                WHERE run_id=?
                AND owner_token=?
                AND status="running"
            """,
            (status,
             record_text,
             self._now(),
             record.state.run_id,
             record.owner_token,),
            )
            if cursor.rowcount != 1:
                raise RunOwnerShipError(
                    "执行权已失效或研究已经结束"
                )
        record.status = status

        logger.debug(
            "检查点已保存 · stage=%s status=%s",
            record.stage,
            status,
            extra={"event": "checkpoint_saved"},
        )

    def close(self) -> None:
        """释放连接。"""
        self._db.close()





    
