from deep_researcher.tools.base import Tool,ToolParameter

from typing import Optional,List,Dict,Any

import logging
from deep_researcher.config import Configuration, get_config
logger = logging.getLogger(__name__)
from pathlib import Path
import uuid
import json





class NoteTool(Tool):
    def __init__(
            self,
            name="note",
            description="笔记工具，支持创建、读取和修改笔记",
            config:Optional[Configuration]=None
    )->None:
        super().__init__(name,description)
        self._config = config or get_config()
        self._workspace = Path(self._config.notes_workspace)
        self._workspace.mkdir(parents=True,exist_ok=True)

        logger.info(f"✅[NoteTool]初始化成功，笔记存储于{self._workspace}")
    def run(self, parameters: dict[str, Any]) -> str:
        """统一入口，根据 action 分发到对应动作"""
        if not isinstance(parameters, dict):
            raise ValueError("parameters 必须是字典")

        action = parameters.get("action")
        if action not in ("create", "read", "update"):
            raise ValueError(f"不支持的 action：{action}，可选值：create / read / update")

        if action == "create":
            return self._create(parameters)
        elif action == "read":
            return self._read(parameters)
        else:
            return self._update(parameters)

        
    def _create(self,params:Dict[str,Any])->str:
        """创建笔记"""
        task_id = params.get("task_id","unknown_id")
        title = str(params.get("title","未命名笔记")).strip()
        note_type = str(params.get("note_type", "task_state")).strip()
        tags = params.get("tags", ["deep_research"])
        content = str(params.get("content", "")).strip()

        note_id = f"note_{uuid.uuid4().hex[:12]}"
        note_data = {
            "note_id": note_id,
            "task_id": task_id,
            "title": title,
            "note_type": note_type,
            "tags": tags,
            "content": content,
        }
        file_path = self._workspace/f"{note_id}.json"

        try:
            file_path.write_text(
                json.dumps(note_data,ensure_ascii=False,indent=2),
                encoding="utf-8",
            )
            logger.info(f"📝[NoteTool] 笔记创建成功,note_id={note_id},path={file_path}")
            return f"笔记创建成功,note_id={note_id}"
        except Exception as e:
            logger.exception("📝 笔记创建失败 | error=%s", str(e))
            raise RuntimeError(f"笔记创建失败：{e}") from e

    def _read(self,params:Dict[str,Any])->str:
        """读取笔记"""
        note_id = str(params.get("note_id")).strip()
        if not note_id:
            logger.error("❌[NoteTool]read失败，note_id为空")
            raise ValueError("note_id不能为空")
        file_path = self._workspace/f"{note_id}.json"

        if not file_path.exists():
            logger.error(f"❌[NoteTool]read失败，{file_path}不存在")
            raise FileNotFoundError(f"笔记不存在：{file_path}")

        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            logger.info(f"📝 笔记读取成功 | note_id={note_id},file_path={file_path}")
            return json.dumps(data,ensure_ascii=False,indent=2)
        except Exception as e:
            logger.exception("📝 笔记读取失败 | note_id=%s error=%s", note_id, str(e))
            raise
    def _update(self, params: dict[str, Any]) -> str:
        """增量更新笔记内容（追加模式）"""
        note_id = str(params.get("note_id", "")).strip()
        if not note_id:
            raise ValueError("note_id 不能为空")

        file_path = self._workspace / f"{note_id}.json"
        if not file_path.exists():
            raise FileNotFoundError(f"笔记不存在：{note_id}")

        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            new_content = str(params.get("content","")).strip()

            if new_content:
                if data["content"]:
                    data["content"] += f"\n\n---\n{new_content}"
                else:
                    data["content"] = new_content
            # 可选更新标题和标签
            if params.get("title"):
                data["title"] = params["title"]
            if params.get("tags"):
                data["tags"] = params["tags"]

            file_path.write_text(
                json.dumps(data,ensure_ascii=False,indent=2),
                encoding="utf-8"
            )
            logger.info("📝 笔记更新成功 | note_id=%s", note_id)
            return "笔记更新成功"

        except Exception as e:
            logger.exception("📝 笔记更新失败 | note_id=%s error=%s", note_id, str(e))
            raise

        # ===================== 参数定义 =====================
    def get_parameters(self) -> List[ToolParameter]:
        """OpenAI 格式的工具参数 schema"""
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作类型，可选：create(创建)、read(读取)、update(增量更新)",
                required=True,
            ),
            ToolParameter(
                name="note_id",
                type="string",
                description="笔记唯一ID，read 和 update 时必填",
                required=False,
            ),
            ToolParameter(
                name="task_id",
                type="integer",
                description="关联的任务编号",
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
                description="笔记类型，如 task_state / conclusion",
                required=False,
            ),
            ToolParameter(
                name="tags",
                type="array",
                description="标签列表，用于检索，必须包含 deep_research 和 task_{id}",
                required=False,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="笔记正文内容",
                required=False,
            ),
        ]
