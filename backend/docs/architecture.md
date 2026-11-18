# 当前架构与状态流转

本文描述当前可运行代码，作为日常修改的入口。专题文档解释具体算法和历史实验；实际接口以当前代码为准。

## 一条调用链

```text
Vue 页面 → api/app.py / routes.py → api/manager.py: ResearchManager
                                   ↓
                    graph/graph_researcher.py: GraphDeepResearcher
                                   ↓
                    graph/builder.py: build_graph
                                   ↓
                    graph/graph_nodes.py: ResearchNodes
                                   ↓
                    services / rag → agents / llm / tools
```

Python 调用和全链路评测直接使用 `GraphDeepResearcher`。节点独立评测可以直接调用对应服务，不必启动完整图。

## 代码放在哪里

| 模块 | 负责什么 | 不应承担什么 |
|---|---|---|
| `api/app.py`、`routes.py`、`schemas.py` | 请求校验、HTTP 接口、把快照转换成响应 | 生成摘要、决定反思下一轮 |
| `api/manager.py` | 后台队列、容量限制、运行/恢复/删除调度 | 再实现一套研究流程 |
| `graph/graph_researcher.py` | 创建研究、加载配置和检查点、执行与恢复、管理资源和运行元数据 | 写提示词、实现检索算法 |
| `graph/builder.py` | 注册节点、连接边、选择串行/并行/反思分支 | 调用模型、修改任务正文 |
| `graph/graph_nodes.py` | 串接业务动作、提交状态、统一日志与事件 | 直接实现 BM25、文件读写细节 |
| `graph/task_state.py` | 任务状态转换、证据与摘要接收、并行结果字段合并 | 模型请求、磁盘读写、另建状态副本 |
| `graph/parallel_tasks.py` | `Send` 分发、阶段合并、共享并发限制 | 分支覆盖整份业务状态 |
| `graph/report_cycle.py` | 报告反思的路由决策、预算、补充任务和草稿提交规则 | 发起模型请求 |
| `services/` | 完成一次规划、检索、摘要、报告、反思或笔记保存动作，返回结果 | 推进整个研究的运行状态 |
| `rag/` | 网页采集、资料库存储、分块、混合检索和重排 | 决定某条研究已完成 |
| `agents/`、`llm/`、`tools/` | 通用模型调用、工具协议和外部能力 | 感知图的下一节点 |
| `events.py`、`logging_config.py` | 展示事件、日志和排障信息 | 作为断点恢复依据 |

`ResearchNodes` 仍保留资源工厂和 `bind()`：它们共享同次执行的日志、限流器及客户端生命周期。没有为追求小文件再引入一套容器或抽象基类。

## 四类信息各自有一个归属

| 信息 | 唯一负责的数据结构/存储 | 谁提交 |
|---|---|---|
| 任务、证据、摘要、最终报告 | `models.py: SummaryState`，序列化到图的 `payload` | 节点；任务规则经 `task_state.py` 执行 |
| 图控制信息 | `graph_state.py: ResearchGraphState` 的 `active_task_id`、`finished`、并行字段等 | 节点返回更新，LangGraph 保存检查点 |
| 报告草稿、审核意见、修订轮次、补搜预算 | `ReportIteration`，保存在图的 `report_iteration` 字段 | 报告相关节点和 `report_cycle.py` |
| 研究列表、配置快照、运行状态、最后错误 | `graph_repository.py: RunInfo` / `runs.sqlite3` | `GraphDeepResearcher`，API 调度层处理排队失败等情况 |

`SummaryState` **没有 `finished` 字段**。任务 `completed`、图 `finished`、研究元数据 `completed` 不是同一层的概念。读取结果时通过 `RunSnapshot.report_ready` 核对图是否结束、是否仍有待执行节点以及是否存在报告。

API 的 `execution=idle/queued/executing` 表示当前进程的调度情况；它不是新的业务完成状态。SSE 中的报告增量是展示草稿，不能代替经过校验的最终结果。

`CitedText`、`EvidenceBatch` 是服务的临时返回值；HTTP 响应模型和 `SummaryStateOutput` 是读取视图，不要求分别更新。不能简单删除这些类型，否则会把业务状态、接口协议和临时结果混在一起。

## 任务如何改变状态

```text
pending → running → completed
              └──→ skipped
```

1. `select_task` 或 `parallel_prepare` 调用 `start_task()`。恢复未完成任务时可以继续处于 `running`。
2. `rewrite_query` 只确定实际查询 `search_query`；规划时的 `query` 保留原文。确实不适合执行的补充查询通过 `skip_task()` 跳过。
3. 检索服务返回 `EvidenceBatch`，节点调用 `accept_evidence()`。无可用证据就跳过并保存原因。
4. 摘要服务返回 `CitedText`，节点调用 `accept_summary()`。有效摘要写入 `summary/citation_ids`，任务仍然是 `running`；没有引用的摘要不进入正式报告。
5. `save_note` 先调用 `NoteService.save_task_note()`。保存成功后，`complete_task()` 写入笔记关联并将任务置为 `completed`。关闭笔记功能时不写文件，仍可完成有效任务。

笔记保存失败时不会把任务标记为完成。异常由入口记录为研究级失败，任务保留未完成状态，恢复时从检查点继续。任务没有另加一个 `failed` 状态，避免与运行元数据重复维护。

这些函数只修改本次节点解码出来的业务对象。节点通过 `bind()` 返回编码后的 `payload`，由 LangGraph 持久化；在内存里赋值不等于检查点已经保存成功。

### 证据目录为什么可以赋值

`EvidenceService.collect()` 和 RAG 证据登记返回的 `EvidenceBatch.sources/evidence` 是“已有全局目录 + 本次新材料”的完整目录；`batch.evidence_ids` 才是本次任务选中的列表。

`accept_evidence()` 在赋值前检查旧目录的编号没有丢失，并检查本任务证据的任务归属和来源关联。如果误把仅含本任务的增量传进来，会直接报错，避免静默覆盖其他任务的证据。这里没有再维护一份总目录。

## 串行与并行的差别

串行 Web RAG 的主路径：

```text
plan → select_task → rewrite_query → discover_sources → read_sources
                                                             ↓
select_task ← save_note ← summarize ← retrieve_evidence ← index_documents
```

反思产生的补充任务先走 `lookup_corpus`，本地材料足够则直接摘要，否则继续联网采集。没有可用证据或引用的任务跳过后回到任务选择，不会继续保存笔记。

同时开启 `enable_parallel_tasks` 和 `enable_web_rag` 时，任务按阶段并行：改写 → 本地查找 → 联网采集 → 索引 → 检索 → 登记证据 → 摘要 → 保存笔记。各阶段汇合后再进入下一阶段。

- 工作分支拿到快照副本，只返回以“批次/阶段/任务”作为键的产物。
- reducer 合并 `parallel_results`，相同结果重复提交允许，冲突结果拒绝。
- 主图按任务顺序调用 `merge_task_result()`，只接受当前阶段允许改的字段。例如改写阶段不能覆盖任务标题、证据和笔记字段。
- 证据编号统一登记，索引和笔记保存沿用现有主图边界，避免多个分支分别分配冲突编号。
- 分支产物在资料库缓存后才返回，因此恢复可以复用已完成的调用。

详细节点顺序、限流和语法见 [并发说明](parallel-tasks.md)。本次没有修改节点名称、检查点字段或分支缓存格式。

## 报告与反思

任务完成后进入 `report`。关闭反思时直接保存报告；开启时把草稿放入 `ReportIteration`，进入 `reflect_report`。

图根据反思和预算进入 `prepare_report_revision`、`finalize_report` 或 `block_report`。需要补充资料时，创建补充任务并回到任务处理入口；已有证据足够时可以直接重写报告。最终交付内容由报告节点提交到 `SummaryState.structured_report/report_citation_ids/report_limitations`，随后 `save_report` 保存笔记并标记图完成。

因此反思循环仍由 LangGraph 的条件边控制，服务只产出审核或正文结果。详情见 [报告迭代说明](report_iteration.md)。

## 数据为什么分开放

- `data/langgraph/`：运行元数据、图检查点和展示事件。恢复以图检查点为准。
- 配置的 `web_rag_workspace`：每条研究的 `corpus.sqlite3`，存网页正文、块、向量及阶段缓存；不把全文和向量塞进 `payload`。
- 配置的 `notes_workspace`：可读的任务和报告笔记；不依赖笔记反向恢复整份状态。
- `evals/`：评测代码和固定输入；批次输出另存。导入规划指标函数不会自动创建模型客户端。

本次没有清理已有研究记录、资料库、笔记、模型缓存、评测题集或历史结果。

## 本次删除与保留

已删除当前生产代码、评测和本地 demo 均未使用的内容：

- `services/note_guidance.py`：旧笔记工具提示词。
- `services/tool_events.py`：旧笔记监听器；主流程现在显式保存笔记。
- `NoteService.load_task_note()`：旧笔记回填入口；当前通过检查点恢复。
- `utils.py` 中旧来源格式化/去重函数和 `get_config_value()`；保留仍使用的 `strip_thinking_tokens()`。
- 未直接使用的 `hello-agents`、`loguru`、`requests` 依赖声明；新增直接使用的 `pydantic` 声明。间接依赖仍由锁文件管理。
- 重复的开发依赖定义和失效 demo 文档入口；补上打包所需的后端 README。

保留仍有明确用途的代码：

- 普通搜索和串行图：配置开关仍能启用，历史研究也可能从这些节点恢复。
- `ToolAwareAgent` 通用监听器：规划工具仍使用，不能随旧笔记监听器一起删。
- `NoteTool.read()`：保留文件读取能力，与失效的状态回填入口不同。
- `graph_nodes.build_graph` 的转导出：原来的 Python 导入位置仍可用，唯一实现位于 `builder.py`。
- 前端组件与状态 composable：仍被页面使用，没有可确认失效的模块。

## 验证与旧测试边界

旧测试此前已决定另行重做，本次没有迁移它们。`test_note_workflow.py`、`test_logging_config.py` 仍引用已经退出的工作流/仓储；`test_utils.py` 仍引用本次删除的旧格式化函数。因此裸跑 `pytest` 仍会遇到这些旧导入错误，不能把它当成当前回归集通过。

本次使用当前图、引用、检索、并发、反思和事件测试，并补充 `test_task_state.py` 检查目录不丢失、非法引用不提交、笔记保存失败不完成、分支合并不越权。执行：

```powershell
uv run pytest --ignore=tests/test_note_workflow.py --ignore=tests/test_logging_config.py --ignore=tests/test_utils.py
```

这些检查使用模拟模型或搜索结果，不证明线上模型质量提升。本次整理保持研究算法、评测指标公式与固定数据集不变。
