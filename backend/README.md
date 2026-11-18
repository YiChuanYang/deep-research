# Deep Researcher 后端

项目启动说明见 [根目录 README](../README.md)。

- 应用入口：`deep_researcher.api.app:app`。
- Python 研究入口：`deep_researcher.graph.graph_researcher.GraphDeepResearcher`。
- [当前职责与状态流转](docs/architecture.md)。
- [全链路评测](evals/README.md)。
- 独立规划评测：在本目录执行 `uv run python -m evals.nodes.planner.runner`。

后端使用远程模型和网页接口，本地保存检查点、语料和笔记；不属于完全离线应用。
