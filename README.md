# Deep Researcher
uv run uvicorn deep_researcher.api.app:app --host 127.0.0.1 --port 8000 --workers 1
基于 LangGraph 的网页研究应用。正式研究入口为 `GraphDeepResearcher`，负责创建研究、执行图流程、读取结果和恢复执行。

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

在已安装依赖、配置好 `backend/.env` 的环境中，从 `backend` 目录运行：

```powershell
uv run python demo/demo_graphresearcher.py
```

## Web 与评测入口

- Web：`deep_researcher.api.app:app` → `ResearchManager` → `GraphDeepResearcher`，启动方式见 [前端说明](frontend/README.md)。
- 评测：评测执行器同样调用 `GraphDeepResearcher`，使用方式见 [评测说明](backend/evals/README.md)。
- 存储：研究元数据由 `graph/graph_repository.py` 中的 `GraphRunRepository` 管理，图检查点由 LangGraph 管理；默认数据目录为 `backend/data/langgraph/`。
