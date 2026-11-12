# 查询改写：职责、状态和验证

## 运行流程

`plan → select_task → rewrite_query → search → summarize → save_note`

`rewrite_query` 仅针对本轮选中的任务执行。它把研究主题、任务标题、任务目标和规划查询一起交给一个禁用工具调用的 SimpleAgent，要求输出一条短查询。强调保留领域、对象、版本和排除范围，并选择任务所需的材料类型。规划提示词和任务拆分逻辑在这一版保持原样，方便把实验差异归因到查询改写。

每个执行任务最多增加一次逻辑模型调用，`max_tokens=256`；已有 SDK 网络重试配置继续生效。仍然只进行一次搜索，不自动增加关键词分支或补查轮次。模型给出空文本、多行、过长文本、JSON、Markdown、思考标签或发生普通异常时，记录回退事件并使用原查询。格式检查不能证明语义正确，相关性仍需要评测；用户中断继续向上传播。

## 三个查询名称

| 名称 | 含义 | 是否持久化 |
|---|---|---|
| `TodoItem.query` | 规划器生成的原查询 | 是，保留不覆盖 |
| `TodoItem.search_query` | 改写节点最终确定的搜索查询，回退时也是原查询 | 是，由节点更新后保存检查点 |
| `TodoItem.effective_query` | 优先取 search_query，否则取 query | 否，是 property 派生值 |

QueryRewriteService 返回字符串，不修改 state；ResearchNodes.rewrite_query 更新 search_query。只增加一个实际状态字段，没有新增任务状态或第二套任务模型。

SearchService 使用 effective_query，并将实际请求字符串显式传给 EvidenceService.collect。Evidence.query 因而记录材料实际由哪个查询取得；去重也使用该值。摘要提示词使用实际查询，笔记同时保留规划查询和实际查询。

## 恢复与旧研究

改写结果作为独立节点保存，检查点采用现有 sync 持久化方式。搜索失败后，图从 search 恢复，不会再次改写；改写失败后的回退结果也被保存。若进程恰好在模型返回后、改写检查点提交前终止，则恢复仍可能再次执行该节点，这不是外部模型调用的 exactly-once 保证。

旧任务缺少 search_query 时默认为 None。旧配置快照缺少 enable_query_rewrite 时恢复为 False；新研究默认 True，且保存到配置快照。已停在 search 的旧研究仍从原节点继续，不强制插入或重跑以前的工作。读取旧报告不改写历史内容。新检查点包含新字段，不保证旧代码能反向读取。

## 配置、日志与前端

在 backend/.env 中使用 `ENABLE_QUERY_REWRITE=true` 或 `false`。服务进程缓存配置，修改后重启后端。此开关针对新研究；恢复已创建研究使用原配置快照。

控制台通过 `🔧 services.query` 展示原查询和最终查询；JSON 日志中的事件为 query_rewrite_started、query_rewrite_completed、query_rewrite_fallback 和 query_rewrite_disabled。模型调用本身沿用现有遥测，会计入评测的 model_requests 等指标。

现有节点 SSE 自动包含 rewrite_query 的开始/返回消息；前端只补上“改写搜索词”的中文名称。没有新增 API 入参，也没有更改前端任务提交方式。

## 验证

以下命令在 backend 目录运行：

```powershell
# 固定模型输出，仅演示状态和数据流，无外部请求。
uv run python demo/demo_query_rewriting.py

# 使用当前配置实际改写一次；会产生模型调用，但不执行搜索。
uv run python demo/demo_query_rewriting.py --live

# 使用脚本模型和临时 SQLite 验证降级、真实搜索输入、引用溯源与失败恢复。
uv run python -m pytest tests/test_query_rewriting.py tests/test_citation_normalization.py tests/test_streaming.py tests/test_evals.py -q
```

同一份代码可切换开关做固定题集对照。以下命令会真实调用模型与搜索；应保留两次输出的批次路径，并按原评分流程评分：

```powershell
$env:ENABLE_QUERY_REWRITE = 'false'
uv run python -m evals.run_suite --variant query-off --case-id DR-001 DR-005 --max-tasks 5

$env:ENABLE_QUERY_REWRITE = 'true'
uv run python -m evals.run_suite --variant query-on --case-id DR-001 DR-005 --max-tasks 5

# 结束本次临时覆盖，让之后的新进程恢复从 .env/默认配置加载。
Remove-Item Env:ENABLE_QUERY_REWRITE
```

应比较：查询是否保留领域、返回材料是否直接对应任务、要点覆盖与证据支持、成功率、模型调用数和耗时。实时搜索结果存在波动；单次运行和离线测试通过不能证明研究质量已经提高。

完整题集的 key_points 仍只用于评分，不传给改写模型。此版本不引入 RAG、资料相关性裁判、正文提取或多轮补查。
