# Deep Researcher

基于 LangGraph 的网页研究应用：规划任务、查询改写、网页采集、混合检索与重排、带引用的摘要和报告，以及报告反思后的定向补搜。支持任务并发、检查点恢复、流式报告和证据回查。

正式研究入口为 `GraphDeepResearcher`。前端和评测都使用这一入口；远程模型与搜索服务需要联网。

## 启动

在项目根目录打开两个终端。保留并配置自己的 `backend/.env`，配置字段与默认值见 [Configuration](backend/src/deep_researcher/config.py)。

```powershell
# 终端 1：后端；uv 使用 backend/.venv，自动安装开发依赖组。
cd backend
uv sync --locked
uv run uvicorn deep_researcher.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

```powershell
# 终端 2：前端
cd frontend
npm.cmd ci
npm.cmd run dev
```

访问 http://127.0.0.1:5173 。前端通过 `/api` 请求后端，不保存 API 密钥。修改 `.env` 后重启后端。部署与前端操作说明见 [前端 README](frontend/README.md)。

## 研究入口

Python 代码统一使用：

```python
from deep_researcher.graph.graph_researcher import GraphDeepResearcher

researcher = GraphDeepResearcher()
try:
    result = researcher.run("你的研究主题", max_tasks=2)
    print(result.report_markdown)
finally:
    # 发生异常时也保留研究编号，供后续恢复。
    print("研究编号：", researcher.last_run_id)
```

恢复已有研究时，在相同数据目录下调用 `researcher.resume(run_id)`

本地 `demo/` 是个人实验目录，不作为正式入口。固定入口不依赖某个 demo 文件是否存在。

## 评测

全链路评测和固定题集见 [评测说明](backend/evals/README.md)。独立节点评测放在 `backend/evals/nodes/`，在 `backend` 目录运行，例如：

```powershell
uv run python -m evals.nodes.planner.runner
```

规划节点的输入是 `evals/nodes/planner/input.json`，指标函数在 `evaluation.py`。运行评测会实际调用模型；单纯导入指标函数不会加载嵌入模型。

## 项目结构

| 目录 | 职责 |
|---|---|
| `frontend/src/` | 页面、交互、状态轮询与 SSE 展示 |
| `backend/src/deep_researcher/api/` | HTTP 边界与后台研究调度 |
| `backend/src/deep_researcher/graph/` | 研究入口、图路由、业务状态提交与恢复 |
| `backend/src/deep_researcher/services/` | 规划、搜索、摘要、报告等业务动作 |
| `backend/src/deep_researcher/rag/` | 网页语料、分块、向量、召回与重排 |
| `backend/src/deep_researcher/agents/`、`llm/`、`tools/` | 模型调用与工具能力 |
| `backend/evals/` | 全链路与独立节点评测 |

完整的职责、节点数据流、状态归属和整理记录见 [架构说明](backend/docs/architecture.md)。图检查点与研究元数据默认保存在 `backend/data/langgraph/`；资料库和笔记路径由配置控制。
