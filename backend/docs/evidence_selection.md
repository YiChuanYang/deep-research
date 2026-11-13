# 证据筛选与正文片段选择

## 已接入的流程

`rewrite_query → search〔搜索 → 页面筛选 → 按需正文提取 → 原文切分 → 片段筛选 → 证据登记〕→ summarize`

图结构保持原样，所有筛选步骤都在搜索节点内完成。新研究默认开启；旧研究的配置快照缺少开关时，恢复默认关闭筛选和提取，继续按原方式处理。新代码可读取旧状态，本次没有增加 SummaryState、TodoItem、Source、Evidence 的字段。

## 职责和数据

- SearchTool 只搜索、标准化结果、批量提取已选 URL。Tavily 使用已有 SDK 的 extract；DuckDuckGo 没有本实现支持的正文提取能力，回退搜索摘要。没有增加依赖或自建网页爬虫。
- EvidenceSelector 清理无效 URL 和空内容，合并同 URL 的结果，批量判断页面是否值得深入阅读。已有 raw_content 的页面直接使用，其他入选页面最多发起一次批量提取。提取部分失败只影响对应页面。
- 切分按连续段落打包，过长段落优先按句末/换行切分并保留重叠上下文。最多处理每页取得文本的前 100000 字符，超限明确提示。仅保守删除整块固定导航文本和完全重复文本，不声称已完成通用网页清洗或语义近重复检测。
- 中文相邻双字与英文词项匹配只用于候选排序。每个来源先提供一段，再填入其他高分片段，最多 12 段进入模型筛选。未进入模型的片段记录为预算排除，不伪装成模型判定无关。
- 筛选模型只能为输入编号返回 keep / drop / uncertain 和理由。必须覆盖全部编号，不能增删或重复编号。相关冲突资料可以保留；模型明确全部 drop 就返回空证据，不补入原始材料凑数。
- 普通模型异常或非法输出才触发规则降级，相关性标记为尚未确认。uncertain 可以保留并明确提示。用户中断向上传播，不当作模型故障。
- 正式 Evidence.text 由程序复制候选原文，不采用模型生成文本。EvidenceService 的 preselected 分支不再裁剪，保证 text 与审计中的原文区间一致；证据类型仍区分 page_excerpt 和 search_snippet。

每阶段最多一次逻辑模型调用，即每个有材料的任务最多新增两次筛选调用（页面、片段），以及一次有页面数量上限的提取批次；SDK 网络重试仍遵循原配置。正文提取不保证取得完整页面，取得时间也不是文章发布时间。提取与模型选择都不能证明来源可靠或事实正确。

## 状态和审计

EvidenceCandidate 是服务内部的临时类型。P 编号代表页面、C 编号代表候选片段，选中后才分配 E 编号。EvidenceBatch 增加临时 selection_audit，图节点不把它写入 SummaryState。

每次搜索的审计追加到研究数据目录的 selection_audits.sqlite3，包括：

- run_id / task_id / execution_id、实际查询、选择预算；
- 当时取得并受上限约束的页面文本及 SHA256；
- 候选片段、相对于保存文本的 start/end 字符位置；
- 实际模型输入、原始回复、每项选择理由、降级及预算排除原因；
- C 编号到最终 E 编号的映射。

偏移相对于保存的提取文本（统一 CRLF 为 LF），不是网页 HTML 行号。不同页面、不连续段落不会被拼成一个原文片段。过长表格仍可能跨窗口，不能保证任意格式表格的表头/限定条件始终完整；此限制应结合审计核查。

研究删除同时删除筛选审计。批量评测对完成的研究导出 selection_audits.json，可用于追查误选/漏选；失败研究仍可在本批次数据库查看已保存审计。审计写入失败会记录警告与任务提示，正式证据仍可保存；审计与检查点不是同一个数据库事务。

搜索节点成功后摘要失败，恢复会复用选好的证据，不重新搜索和筛选。搜索节点内部失败或提交检查点前退出，可能重做本节点。重复尝试的审计保留为多条记录，不宣称外部请求恰好执行一次。

## 配置

在 backend/.env 配置，修改后重启后端，对新研究生效；恢复任务使用其保存快照。

```dotenv
ENABLE_EVIDENCE_SELECTION=true
ENABLE_PAGE_EXTRACTION=true
SELECTION_MAX_SOURCES=3
SELECTION_MAX_FRAGMENTS=6
SELECTION_PER_SOURCE=2
SELECTION_INPUT_CHARS=16000
EVIDENCE_MAX_CHARS=2000
```

SELECTION_INPUT_CHARS 是每次筛选用户消息的字符上限，不是 token 数，也不包含固定系统提示词。每段不超过 EVIDENCE_MAX_CHARS，最后保留数量同时受任务与每来源预算约束。

启用筛选时 SearchTool 不预先为所有页面请求 raw_content，改为选中后按需 extract。FETCH_FULL_PAGE 对原搜索深度的选择继续保留；关闭筛选时沿用旧流程。正文关闭时仍可利用搜索返回中已有的 raw_content。没有 Agent 工厂的直接 SearchService 调用会明确降级为规则选择；主图已经提供工厂。

日志使用 🧾 services.selection，记录页面数量、保留片段数量和降级。提取请求/响应批次数、请求页面数进入评测遥测与对照；它们不是金额成本，也不是成功提取页面数。模型调用继续计入已有模型用量。

## 运行与评估

在 backend 目录执行：

```powershell
# 固定页面、固定模型，不消耗外部额度。输出到 data/evidence-selection-demo/audit.json。
uv run python demo/demo_evidence_selection.py

# 搜索材料仍固定，真实调用最多两次筛选模型，检验供应商输出格式与选择效果。
uv run python demo/demo_evidence_selection.py --live-selector

uv run python -m pytest tests/test_evidence_selection.py tests/test_query_rewriting.py tests/test_citation_normalization.py tests/test_streaming.py tests/test_evals.py -q
```

正式质量评测时，在同一份代码上通过 ENABLE_EVIDENCE_SELECTION 切换开关，沿用 evals.run_suite / summarize / compare。保持模型、任务数、查询改写和 FETCH_FULL_PAGE 相同。检查报告覆盖率、证据支持率、完成率、调用量和耗时，也检查被剔除的关键材料。实时搜索有波动，单轮成绩不能证明稳定提升。

筛选没有解决所有问题：词项预筛可能漏掉同义表达，模型也会误判；搜索阶段找不到的内容无法靠筛选补出来。本版本不加入多轮补查、向量库或结论级事实核查。
