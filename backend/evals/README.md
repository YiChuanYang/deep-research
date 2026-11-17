# 固定问题集评测

Web RAG 的阶段、配置、快照与降级行为见 [Web RAG 说明](../docs/web_rag.md)。

报告级反思与定向补搜默认启用，配置、预算和数据流见 [报告迭代说明](../docs/report_iteration.md)。对照 v2 时显式设置 `ENABLE_REPORT_REFLECTION=false`；新增的 `report_iterations.json` 和 `telemetry.report_iteration` 用于诊断轮次收益，不改变人工质量评分公式。

若只检查反思对同一初稿的增量，可运行 `python -m evals.report_cycle_probe <fixture或snapshot.json> ...`。它复用初始正文和材料，补搜仍联网；不是全链路 A/B，也不会自动生成质量分。示例和局限见 [边界修复与对照记录](../docs/report_iteration_fixes.md)。

查询改写版本的配置、状态与开关对照方法见 [查询改写说明](../docs/query_rewriting.md)。

这套评测用于回答：同一批研究问题上，修改后的系统是否更可靠、报告是否更完整、证据是否支持结论、耗时和调用量是否发生变化。

当前题集为 `cases.v1.json`，内容版本 `1.1.0`：24 题，**20 道 dev、4 道 holdout**。overview / comparison / engineering / boundaries 每类各有 5 道 dev 和 1 道 holdout，每题 4 个固定验收要点。文件名中的 v1 对应题集格式的主版本；题集校验仍支持每题 3～5 个要点。

本次保留 DR-001～DR-012 的题目、分组和验收要点，新增的 12 道题全部进入 dev；DR-009～DR-012 继续作为 holdout。新增题目覆盖如下：

| 类别 | 新增编号与主题 |
|---|---|
| overview：知识概述 | DR-013 数据库事务；DR-014 训练/验证/测试集与数据泄漏；DR-015 HTTP 缓存 |
| comparison：方案比较 | DR-016 SQLite 与 PostgreSQL；DR-017 PCA 与 t-SNE；DR-018 单元、集成与端到端测试 |
| engineering：工程设计 | DR-019 私有文档与网络联合检索；DR-020 消息消费与外部通知；DR-021 并发检索、重试与取消 |
| boundaries：判断边界 | DR-022 缓存性能与新鲜度；DR-023 相关性、因果性与广告证据；DR-024 软件版本与实际配置 |

新增题目的验收要点在运行前固定，不根据生成报告倒推评分标准。涉及假设案例的题目要求保留未知条件，不把虚构场景当成真实产品或实验去补造证据。

扩充会改变题集指纹，20 题的新批次不能直接与历史 8 题批次计算整体差值。比较新旧系统时，两版都应运行同一份 20 题开发集；如果只复跑历史题集，可通过 `--dataset evals/runs/<旧批次>/dataset.json` 使用当时冻结的题集。仅用 `--case-id` 选出旧编号不会恢复旧题集指纹。评分规则与计算公式不变。

这是“在线研究 + 自动结构检查 + 人工质量评分”的工作流。测试使用离线脚本模型，只验证工程链路与计算口径，不代表真实模型成绩。

## 1. 先预览，再跑一题

以下命令在项目根目录的 PowerShell 执行。后续命令均在 backend 下执行。

```powershell
cd backend
.\.venv\Scripts\python.exe -X utf8 -m evals.dataset
.\.venv\Scripts\python.exe -X utf8 -m evals.run_suite --variant baseline-v1 --dry-run
.\.venv\Scripts\python.exe -X utf8 -m evals.run_suite --variant baseline-v1 --case-id DR-001 --max-tasks 5
```

前两个命令不调用外部服务；第三个命令读取应用现有配置并实际调用模型和搜索。配置沿用 `Configuration.from_env()`，从 backend/.env 和系统环境加载。它需要应用原有的模型配置；使用 Tavily 时还需要相应搜索凭证。

`--variant` 只是人为指定的版本标签，不会自动切换代码或提示词。`--max-tasks` 是研究执行的子任务上限，不是题目数量；规划出的任务可能多于实际执行的任务。

每次命令都会输出新的绝对批次目录。不同批次隔离数据库、事件、日志和笔记，每次试验使用新 run_id；不会把恢复旧研究当作独立重复试验。普通失败记录后继续下一题，Ctrl+C 会保留当前中断和剩余未开始记录。硬终止可能留下 running，汇总不会猜测它已完成。

每个批次包含：

```text
evals/runs/<时间-随机编号>/
  manifest.json          标签、配置、源码指纹、计划执行清单
  dataset.json           冻结的完整题集
  rubric.md              本次人工评分规则
  source_snapshot/       当时的 Python 源码、评测代码和依赖声明
  data/                  独立的 SQLite 研究数据
  logs/                  运行日志
  notes/                 启用笔记时保存的批次笔记
  attempts/DR-001/trial-001/
    attempt.json         执行/导出状态、耗时、调用量
    case.json            当前题目和验收要点
    snapshot.json        研究状态快照
    report.md            完整报告
    body.md              可自动识别时，保存模型生成的正文
    evidence.json        本次真正保存的证据与来源
    checks.json          导出时的自动检查
    review.json          需要人工填写的评分表
  summary.json
  summary.md
```

失败或导出未完成的试验可能只有部分文件。源码快照不包含 .env，配置仅保存应用允许的非敏感字段。运行输出已加入 .gitignore。

快照用于核对当时的输入、代码和输出；不等于外部网页和模型服务可完全复现。当前评测没有冻结网络响应，也没有固定证据回放模式。

## 2. 填写 review.json

打开该题的 `report.md` / `body.md`、`evidence.json` 和 `review.json`，按本批次 `rubric.md` 评分。

- 保留 case_id、run_id、report_sha256 和 key_points 中的 point 原文及顺序。
- 每个要点填 score（整数 0、1、2）和具体 reason。理由应指向报告具体段落，说明缺了什么。
- 填 reviewer、evidence_sufficiency、limitations_score 和 limitations_reason。
- 在 claims 中逐条摘录需要证据支持的事实性结论，包括没有引用的结论。每条尽量只表达一个可核查主张；保留原句中的引用。
- 阅读对应 E 编号在 evidence.json 中实际保存的 text，再判 supported / partial / unsupported / uncertain。来源标题或链接本身不能代替支持内容，不能把网页中未被系统取得的内容补算为它使用过的证据。
- 只有确实枚举了正文中全部需要核查的事实结论，才将 claim_inventory_complete 设为 true；抽查时保持 false。
- 全部填完后才把 status 从 pending 改为 done。确实没有可核查结论时填 no_claims_reason，引用指标会是 N/A。

claims 中一条记录的格式如下。文字仅示意，必须换成当前报告的原句：

```json
{
  "text": "从报告正文复制的完整结论，保留引用标记 [E0001]",
  "citation_ids": ["E0001"],
  "verdict": "supported",
  "reason": "E0001 的具体内容直接支持这句话，没有扩大适用范围。"
}
```

无引用的事实性结论填 `citation_ids: []`、`verdict: "unsupported"`；这里表示“系统给出的证据未支持”，不自动认定其事实为假。需要重写很长的复合结论时，尽量选取原文中连续的、可单独判断的片段，不要自行改写 text。

代码会检查报告哈希、运行编号、固定要点、原文摘录、引用编号以及重复结论；它不能替代人判断证据是否真的支持结论，也不能证明人工已枚举全部结论。

## 3. 汇总

把下面路径替换为刚输出的实际批次目录：

```powershell
$batch = "C:\实际路径\backend\evals\runs\实际批次目录"
.\.venv\Scripts\python.exe -X utf8 -m evals.summarize $batch
```

汇总不调用模型，会重新检查原始状态和人工评分，生成 summary.json / summary.md。评分前运行也有效，此时质量分为 N/A。它不会覆盖 review.json。

兼容原命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evals.review summarize $batch
```

旧的历史运行导出流程仍保留，但历史样本不是预先排定的完整题集，无法从“只导出的成功报告”推算真实运行完成率；需要公平对照时使用新 run_suite。

## 4. 批量执行和对照

先运行基线，并保存它输出的目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evals.run_suite --variant baseline-v1 --split dev --repeats 3 --max-tasks 5
```

这会计划 20 × 3 = 60 次独立研究。可以先用默认 repeats=1 运行 20 次，再增加重复次数；`--dry-run` 可核对所选题目和次数，不会调用模型或搜索。

修改一项明确因素后，例如报告提示词，再运行候选版本：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evals.run_suite --variant report-prompt-v2 --split dev --repeats 3 --max-tasks 5
```

两组都完成评分后：

```powershell
$baseline = "C:\实际路径\backend\evals\runs\基线批次"
$candidate = "C:\实际路径\backend\evals\runs\候选批次"
.\.venv\Scripts\python.exe -X utf8 -m evals.compare $baseline $candidate
```

对照写入新的 evals/comparisons 子目录。它要求题集指纹、分组、运行模式、评分规则相同；按 case_id + trial 配对。缺失的试验和每项指标被排除的配对数都会显示。引用指标还要求双方同为 complete 或同为 sampled。

每项指标的差值为“候选减基线”。配对后，每题先平均各次差值，再对题目等权平均。比例差值 0.10 表示提高 10 个百分点；耗时差值 -5 表示平均少 5 秒。不要直接相减两个批次各自的总体均值来代替配对比较，因为有效样本集合可能不同。

源码、模型和搜索配置会保存并展示差异；代码不会限制只能修改一个因素。为了能解释结果，建议一轮只改一项主要因素。在线搜索在不同时间会返回不同材料，重复编号也不代表随机种子相同；这里提供描述性对照，没有显著性检验或因果保证。

开发期间使用 dev，待方案确定后再在 holdout 上比较基线和候选：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evals.run_suite --variant final-v2 --split holdout --max-tasks 5
```

不要用 holdout 的逐题错误反复调提示词后，还声称它是未见过的最终评测。

## 5. 指标定义与数据来源

### 运行与评分进度

来源是 manifest.json 中预先建立的清单和每次 attempt.json。

| 字段 | 计算 |
|---|---|
| scheduled / started | 计划试验数 / 已进入 running、completed、failed、interrupted 的试验数 |
| execution_completion | 完成研究并取得最终报告的次数 ÷ started |
| export_completion | 完成导出的次数 ÷ 研究完成次数 |
| review_completion | 通过校验的 done 评分数 ÷ 完成导出的次数 |

尚未开始的试验不进入执行完成率分母，仍在运行的试验会进入分母。研究完成、导出完成、人工评分完成是独立状态。资料不足的系统说明也是已交付输出，可能计为执行完成，但不会因此获得质量高分。无效记录无法可靠判断是否开始，会单独显示 invalid_record；此时完成率仅描述可识别记录。

### 自动结构检查

来源是 snapshot.json 中真实的状态、任务摘要、报告正文、引用和证据关联；汇总时调用 inspect_state 重算，不信任可手动改写的 checks.json。

- task：对每个 completed 子任务检查摘要引用，分母单位是子任务。
- report：剥离程序自动追加的参考条目和研究范围说明，再检查模型正文引用，分母单位是报告。
- overall：任务、报告或证据关联任一项失败即 fail；否则沿用报告检查状态，分母单位是试验。

检查是否至少有合法证据引用、是否存在对应证据及来源、是否在允许范围内、是否与保存的 citation_ids 一致，以及正文是否违规直接写入 URL。报告仅允许引用已完成任务摘要中使用过的证据。

三类 pass_rate 都是 pass / (pass + fail)。正文边界无法确认时记 unverified；已知的资料不足模板记 not_applicable；二者不当作 pass 或 fail，数量另外展示。自动通过只证明当前引用协议和关联结构成立，不证明内容正确，也不要求每个事实句都有引用。

### 人工质量指标

来源是固定题集的 key_points 和人工填写、通过校验的 review.json。

| 字段 | 单次试验计算 |
|---|---|
| coverage_ratio | 各要点得分之和 ÷ (2 × 固定要点数) |
| citation_coverage | 带至少一个引用的已核查结论数 ÷ 全部已核查结论数 |
| citation_support | 判为 supported 的结论数 ÷ 带引用的已核查结论数 |
| limitations_score | 人工 0～2 分，评价证据不足、适用条件和未完成范围的处理 |
| evidence_sufficiency | 人工 enough / limited / insufficient / uncertain 分类，汇总各类数量 |

覆盖要点 0=未回答，1=部分回答，2=完整回答；这是回答覆盖度，不是外部事实准确率。例如 4 个要点得 2、2、1、0，总分 5/8=62.5%。

引用以“结论”为计数单位，不以 E 编号数量为单位；一条结论引用 3 个来源仍只算 1 条。supported 才进入支持率分子，partial 不计半分，unsupported 和 uncertain 也不进分子，但有引用时都保留在分母。

例如核查 10 条结论，8 条有引用，其中 5 条 supported、2 条 partial、1 条 uncertain：引用覆盖率为 8/10=80%，引用支持率为 5/8=62.5%。这个例子不是实际模型成绩。分母为 0 时返回 null / N/A，不记为 0% 或 100%。

批次质量指标采用“同题有效重复先平均 → 对题目等权平均”。同时提供 n_attempts、n_cases 和逐题均值；只有 reviewed 的有效评分进入计算。抽查和全文核对的引用指标分别汇总。claim_counts 是各类结论数量，不使用“把所有结论池化”的比例冒充题目等权平均。

如果一组失败较多，它的有效报告质量可能看起来更高，因此必须同时看执行完成率、评分完成率和样本数。不能根据少数成功报告的质量均值判断整个系统更好。

### 效率与资源

来源是 runner 的单调计时器和应用已有的结构化日志；使用 run_id 隔离归属。

| 字段 | 来源与口径 |
|---|---|
| research_duration_s | 从构建研究器之前到执行/最终状态检查结束；包含初始化、保存运行编号、规划、检索、摘要、写笔记、报告和持久化等开销；不含评测材料导出及人工评分 |
| model_requests / model_responses | model_request、model_stream_started 的开始数 / model_response、model_stream_completed 的完成数 |
| search_requests / search_responses | search_started 的开始数 / search_completed 的完成数 |
| reported_tokens | 所有已返回合法 usage.total_tokens 的模型响应之和，可能仅为部分用量 |
| total_tokens | 只有每次模型请求都完成且全部返回 usage 时才给出总和，否则 null |
| report_ttft_s | 当前报告流式调用开始，到收到第一个非空 content 分片的秒数；不是从用户提问开始计时 |
| phase_duration_s / node_duration_s | LangGraph 节点 action 的单调时钟耗时；每次研究先按研究阶段/节点累加，同一阶段的多次任务会求和，再在批次中统计均值、中位数和 P95。包含正常返回及抛出普通异常的动作耗时，不等于整次研究总耗时 |
| node_calls | 本次研究已计时的各节点执行次数，包括普通异常失败；可用 node_duration_s ÷ node_calls 查看单次执行平均时间 |
| token_usage_complete | 完整获取 token usage 的已完成研究次数 ÷ 全部已完成研究次数 |
| cost | 当前为 null，未计算费用 |

模型和搜索调用次数是应用记录的逻辑调用数，不是底层 HTTP 次数；不统计 SDK 内部透明重试的次数。失败请求若没有返回 usage，可能已消耗 token，但无法从已有数据确定。当前流式实现没有主动要求供应商返回 usage，因此部分兼容服务的 total_tokens 经常会是 N/A；不能拿 reported_tokens 当完整成本比较。

没有模型调用时，0 次调用、0 token 是合法结果；遥测根本没有建立时是未知。费用还需要具体供应商价格、输入/输出/缓存等计费分项，本实现不根据总 token 猜测金额。

效率汇总提供样本数、均值、中位数、P95、最小值和最大值。P95 将数值排序，取 (n-1)×0.95 的位置线性插值，小样本不适合解释为稳定尾延迟。主要耗时统计使用成功研究，另列全部结束研究（完成/失败/中断）的耗时；调用量、token 和 TTFT 的批次统计使用已完成研究。失败试验的原始用量仍保留在 attempt.json。

阶段耗时保存在每题 `attempt.json` 的 `telemetry.phase_duration_s`、`telemetry.node_duration_s`、`telemetry.node_calls` 中；批次 `summary.json` 的 `stage_durations` 和 `summary.md` 的图阶段耗时表负责汇总。旧批次缺失这些字段时仍可读取，但缺失值不填零；未执行某节点的研究不进入该节点均值分母，因此需同时查看“有记录研究数”。

`phase_duration_s` 的 `plan`、`tasks`、`report`、`save_report` 是粗粒度分类；`node_duration_s` 可进一步区分网页发现、正文读取、索引、取证、摘要、报告生成和反思。两种视图来自同一批节点事件，不能相加。只消费 `graph_node_completed`，不会把服务内部或模型调用的耗时再次累加。

这里记录的是墙钟经过时间，包含动作内部的 I/O 等待和重试，不是 CPU 时间。节点动作之外的图调度、检查点持久化、状态编码与入口开销仍由 `research_duration_s` 覆盖。中断时仅保留已经记录的节点：`KeyboardInterrupt` 或进程被强制终止时，尚未返回的动作没有节点耗时，不能把部分统计当成完整耗时。如果未来图内并行执行，节点累计时间可能超过整次研究耗时，届时不能直接将相加结果解释为关键路径或占比。

对照效率只使用双方都完成且该指标均有效的配对，所以还必须同时查看失败率和排除数。

### 诊断数量与尚未测量的能力

planned_tasks、completed_tasks、skipped_tasks、sources、evidence、report_citations 分别来自任务状态和证据字典；最后一个是报告中不同引用 ID 的数量。数量可以辅助定位问题，更多资料或更多引用不自动代表质量更高。

当前没有检索 Recall@K / MRR / NDCG（缺少标准相关资料标注）、外部事实准确率、自动幻觉率、模型裁判分、金额成本、统计置信区间或完整网页回放。人工引用支持只能判断“本次保存的证据是否支持报告”，不能证明来源本身可靠且事实无误。

## 6. 离线验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_evals.py tests/test_streaming.py -q
```

评测测试覆盖题集重复/分组隔离、手工评分计算、无效评分、零分母、未知正文边界、token 缺失、真实图与 SQLite 导出、失败/中断保留、导出失败、题目等权及配对对照。不请求真实模型或搜索服务。

## RAG 局部性能回放

向量设备、分批编码、BM25 缓存与两种重排输出格式可以独立回放，无需每次重跑完整研究。
命令与缓存条件见 [RAG 提速说明](../docs/rag-performance.md)，本轮结果见 [实测记录](../docs/rag-speed-results.md)。
评测产物中的 `rag_step_duration_s`、`rag_step_calls`、`rag_cache_hits` 和新编码计数用于定位内部开销；内部步骤包含在图节点计时中，不能相加。
