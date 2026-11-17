# 同一研究中的分阶段并发

本次让 LangGraph 分发独立子任务，复用同步服务和线程执行。没有将项目整体改成 async/await。默认最多两个业务分支同时执行，网络请求与模型请求另有限额，本地向量计算只有一个入口。

## 一次研究的数据流

```text
规划任务
  → 确定本批待完成任务、生成 batch_id
  → 并发改写查询 → 主图汇总查询
  → 补充任务并发检查已有资料 → 主图汇总覆盖情况
  → 仅缺资料的任务并发搜索、读取网页 → 主图汇总
  → 串行建立索引，复用相同正文和向量，冻结网页 snapshot_id 集合
  → 并发检索和重排，只保存选中的 chunk_id
  → 主图按任务顺序统一登记 S/E 编号
  → 并发生成摘要，每个摘要使用已经稳定的 E 编号
  → 主图汇总摘要、保存笔记
  → 生成报告 → 反思
                    ├─ 完成：定稿交付
                    └─ 缺资料：下一批补充任务进入同一流程
```

图在同一阶段内分发多个任务，阶段之间有汇总边界。慢任务会使本阶段的下一步等待；后续可研究流水线，但本版优先保证资料可见范围、编号和恢复语义清楚。

初始任务现在共同检索本批已经收集完毕的资料；旧串行流程中，前面的任务只能看到当时已有的资料。因此即使候选数量和重排模型不变，选段仍可能变化，不能把并发版本当作内容严格等价的加速。

## 为什么把选段和登记分开

`WebRagPipeline.select_evidence()` 只将候选评分与选中的 chunk_id 写入资料库，不修改 SummaryState，不分配 E 编号。不同任务可以独立执行它。

`WebRagPipeline.register_evidence()` 读取已经保存的选段，创建 Source/Evidence，返回 EvidenceBatch。主图逐任务接收，调用既有 `_accept_rag_batch()`，因此第二个任务能够看到第一个任务已经分配的编号，不会同时生成 E0001。

每个并发摘要收到的是相同已提交研究快照的独立副本。允许引用范围仍是该任务的 evidence_ids，合并与最终保存仍校验引用关联。

## 主要技术和语法

### 1. Send：一次分发多个任务

实际代码位于 `graph/parallel_tasks.py`：

```python
[Send(f"parallel_{phase}", {"values": values, "task_id": i}) for i in ids]
```

- `[表达式 for i in ids]` 是列表推导式，遍历任务编号，生成一个 Send 列表。
- `f"parallel_{phase}"` 是 f-string，将阶段名拼进节点名。
- `Send(节点名, 输入字典)` 创建一条调度指令，本身不执行搜索。LangGraph 接收到列表后，分别调用目标节点。
- 每条指令包含自己的 task_id。分支不再共同修改主图的 active_task_id。

例如两个任务会生成 `Send("parallel_collect", {..., "task_id": 1})` 和任务 2 对应的 Send。

参考：[LangGraph Send](https://docs.langchain.com/oss/python/langgraph/graph-api#send)。

### 2. Annotated 与 reducer：让分支结果正确合并

实际声明：

```python
parallel_results: Annotated[dict[str, dict[str, Any]], merge_task_results]
```

`dict[str, dict[str, Any]]` 描述字段的数据类型。`Annotated[类型, 附加信息]` 允许在类型旁附加元数据；这里 LangGraph 识别第二项为状态合并函数。

reducer 接收“当前字典”和“新结果字典”，返回合并后的字典。键为 `batch_id:phase:task_id`：不同任务互不覆盖，相同结果重放是幂等的，同键不同内容则报错。

分支只返回：

```python
return {"parallel_results": {key: result}}
```

不会返回整份 payload。否则多个分支写同一个完整业务状态，无法正确表达它们各自修改的部分。主图汇总节点从结果中提取本阶段允许更新的字段，再统一更新 payload。

### 3. 同步函数 + 线程并发

本轮仍然使用 `def` 与同步 SDK。LangGraph 的同步执行器在不同线程中运行分支，A 等远程搜索返回时，B 可以发出自己的请求。主要重叠的是网络等待，不是让普通 Python 计算自动变成多核计算。

不需要先把每个函数写成 async。以后如果使用异步 HTTP/模型客户端，可以逐步迁移，但仍需要这些任务边界、并发上限和合并规则。

当前 LangGraph 1.2.20 的同步 custom stream 会在执行器中运行事件队列等待。实测 `max_concurrency=1` 会造成等待占用唯一线程，因此图执行器使用 `task_concurrency + 1`，为调度保留余量；真正的业务分支数量由下面的信号量严格限制。

### 4. BoundedSemaphore 与 with：限制并发并保证释放

```python
self._task_gate = BoundedSemaphore(config.task_concurrency)

with self.nodes._task_gate:
    return run(job)
```

信号量可以理解成固定数量的通行证。`with` 进入时取得一个，没有剩余名额就等待；退出时归还，即使内部抛出异常也会释放。BoundedSemaphore 还能发现归还次数超过初始额度的错误。

网络和模型有各自的信号量：搜索/正文读取最多 network_concurrency 个，普通模型调用最多 model_concurrency 个。模型 SDK 的重试仍占用原名额。报告流式生成发生在汇总后，本阶段只有一个报告调用。

本地模型加载与编码用 RLock 保持单入口，避免多个任务把 4GB GPU 挤满。RLock 是可重入互斥锁，同一线程重复进入允许，其他线程等待；和允许多个名额的信号量不同。

### 5. copy、deepcopy 和 finally：隔离任务与释放资源

`copy(self)` 浅复制节点执行对象，保留配置、共享限流器及子类测试工厂；随后把 `_llm`、`_rag` 重置成独立客户端和检索缓存。共享限流器是有意的，否则每个任务各有两张通行证，总并发就不受控。

`deepcopy(job["values"])` 复制嵌套状态字典，避免修改一个任务时影响兄弟任务。服务中的 dataclass 用 `asdict()` 展开为普通字典，便于写入 SQLite 与检查点。

`finally: worker.close()` 在成功、异常时都释放该分支的模型客户端。主图对象仍拥有自己的客户端，不被分支关闭。

### 6. 闭包与 Callable：给每个阶段生成节点函数

`worker(phase)` 返回内部的 `execute(job)`。内部函数会记住外层 phase 和 self，这叫闭包；不用为五个阶段复制五份几乎一样的调度代码。

`Callable[[WorkerInput], dict[str, Any]]` 表示“接收 WorkerInput，返回字典”的函数类型。`TYPE_CHECKING` 下的导入只给类型检查器使用，运行时不执行，可避免节点模块和并发模块互相导入。

## 失败恢复

每个成功分支先把结果保存到当前研究的 corpus.sqlite3：`parallel-result:{batch}:{phase}:{task}`，再返回图更新。LangGraph 的检查点继续负责调度进度与分支更新。

例：A 摘要成功，B 摘要报错。研究不交付；A 的成功结果已经持久化。恢复时使用同一个 run_id 与已保存批次，复用 A 的结果，只补做未完成的工作。已选网页、向量和选段同样复用。只有汇总边界提交后，状态接口中的主业务状态才前进；分支已返回不等于整阶段已提交。

若进程在远端响应后、本地保存前被强制结束，这次请求仍可能重复；本版不承诺跨远端服务的 exactly-once。线程内正在执行的同步请求不能瞬间取消，错误退出可能需要等待正在运行的请求结束或超时。没有新增用户取消 API。

新研究的 `enable_parallel_tasks` 等参数写入配置快照；旧记录缺少开关时按 False 恢复，保留原串行节点。修改当前环境不会改变已创建研究保存的并发配置。

## 配置与能力边界

```dotenv
ENABLE_PARALLEL_TASKS=true
TASK_CONCURRENCY=2
NETWORK_CONCURRENCY=2
MODEL_CONCURRENCY=2
```

这些配置可放在 backend/.env。默认已经启用，不需要添加才能运行。设 `ENABLE_PARALLEL_TASKS=false` 可让新研究使用串行路径；`TASK_CONCURRENCY=1` 可验证新分阶段图的单分支执行。该能力只用于 Web RAG 路径，旧搜索路径保留原图。

反思仍受 `REPORT_MAX_REVISIONS` 和 `REPORT_MAX_SUPPLEMENT_TASKS` 控制。新并发模式允许同一轮的多个独立缺口组成一批，只增加一次报告修订轮次，追加任务逐个占用总任务预算。旧串行模式仍每轮追加一个。重复查询会被过滤。

各阶段批次汇总是确定顺序的；当前规划器没有任务依赖字段，默认这一批子任务可以独立研究。若未来出现“B 必须使用 A 的结论”，应显式表示依赖再分批，不能让它们盲目并发。

不同用户研究仍沿用 API 的原有排队策略，本轮不开放多个完整研究同时运行。前端增加了并发阶段名称和本批任务展示，SSE 按节点和 task_id 共同追踪，避免一个任务返回就清空其他任务的进行中显示。

## 可观测性与验证

- node_duration_s 是各分支工作时间累加，并发时可能大于实际等待。
- parallel_wall_duration_s 记录本次执行中，从首个分支开始到本阶段汇总的实际时间；未完成阶段不补零。它与节点时间不能相加。
- `batch_task_ids` 包括本批执行和排队任务，不是实时线程列表；节点 SSE 事件继续带具体 task_id。
- 103 项相关测试通过：并发屏障验证、失败兄弟分支复用、引用归属、同批语料冻结、补充资料本地优先、并发限额、旧图恢复、API、报告和流式链路。
- 前端类型检查与生产构建通过。在本沙箱中默认 Vite 配置打包会读取无权限父目录，验证命令使用 `npm.cmd run build -- --configLoader runner`，无需修改日常启动命令。

真实联网验证：批次 `20261117T023437Z-3070c63d`，DR-002，初始任务上限 2。研究、报告反思和导出完成，总耗时 103.75 秒；模型复核 accepted，自动引用检查通过。尚未进行独立质量评分，也不是旧 20 题批次的可比速度结论。

其中两个摘要分别用时约 12.58 秒和 13.09 秒，累计工作约 25.71 秒，但摘要阶段实际等待 13.13 秒，日志确认两任务重叠执行。其他并发阶段实际等待：查询改写 3.82 秒、网页收集 19.45 秒、检索重排 10.20 秒。统一索引节点 5.23 秒，报告生成 31.63 秒。

真实产物见 [汇总](../evals/runs/20261117T023437Z-3070c63d/summary.md)。这个试验用于验证闭环与并发发生，不证明整体质量提升或固定倍数提速。
