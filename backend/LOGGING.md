# 日志使用

所有 demo 通过 `run_cli(main)` 启动。其他入口调用一次 `setup_logging()`，业务模块继续使用 `logging.getLogger(__name__)`。

```python
from deep_researcher.logging_config import setup_logging

setup_logging()                       # 控制台 INFO，JSON 文件 DEBUG
setup_logging(console_level="DEBUG")  # 展开底层调用和控制台异常堆栈
setup_logging(log_dir=None)           # 只输出控制台
```

控制台列：时间、等级、模块表情与简称、研究编号、任务编号、当前操作、正文。
中文操作名按显示宽度补齐；32 位研究编号显示为 `R` 加前 8 位大写字符。
短编号仅用于阅读，`resume()` 必须使用完整 `run_id`，可从 demo 输出或 JSON 日志取得。
颜色在支持的终端中自动启用；重定向输出不带颜色，`NO_COLOR` 可关闭颜色。
终端编码无法显示表情时使用普通字符，JSON 文件始终使用 UTF-8。

默认日志：`backend/logs/research.jsonl`。单文件最大 5 MiB，保留 3 个备份。
`DEEP_RESEARCH_LOG_DIR` 或 `log_dir` 可更换目录；
`DEEP_RESEARCH_LOG_LEVEL=DEBUG` 可改变默认控制台等级。
文件时间使用 UTC，控制台显示本地时间。

```powershell
$env:DEEP_RESEARCH_LOG_LEVEL = "DEBUG"
.\.venv\Scripts\python.exe .\demo\demo_logging.py
```

新服务的同步入口可以使用：

```python
@log_operation(
    "任务总结",
    state_parameter="state",
    task_parameter="task_id",
    checkpoint="summarize",
)
def summarize_task(self, state: SummaryState, task_id: int) -> str:
    ...
```

装饰器自动读取 `state.run_id`，绑定当前操作和任务编号，并在服务边界记录异常。
即使单独执行服务 demo，也不会把真实研究编号替换成入口的临时日志编号。
`with log_context(...)` 支持嵌套上下文，在退出或异常时恢复。
ContextVar 隔离不同异步任务；新线程需要显式传递上下文，当前工作流串行执行。
装饰器本身用于同步函数。

INFO 记录研究进度、服务结果和恢复位置；DEBUG 记录 Agent 迭代、工具注册、
模型用量、数据库检查点和底层文件操作。笔记的完整编号和路径放在 JSON 字段中，
控制台只显示保存结果和字符数。第三方库默认只显示 WARNING 及以上。

沿用模块表情：🚀 research、📋 research.task、🧭 services.planner、🔎 services.search、
🧠 services.summarizer、📝 services.reporter、📒 services.note、🗄️ storage.repository。
Agent、模型、工具和监听器分别使用 🤖、🔌、🧰、🪝。

下面是简化的输出示例，`demo_logging.py` 可离线展示完整分栏和恢复场景：

```text
10:00:00 INFO  🚀 research            RABC12345 —   新建研究     │ 开始研究 · 模型能力评测
10:00:02 INFO  📋 research.task       RABC12345 T01 执行任务     │ 开始 · 基准比较 · checkpoint=search
10:00:04 INFO  🔎 services.search     RABC12345 T01 搜索         │ 检索完成 · 5 个来源 · 耗时=1.80s
10:00:10 INFO  🧠 services.summarizer RABC12345 T01 任务总结     │ 摘要已生成 · 986 字 · 耗时=6.20s
10:00:10 INFO  📒 services.note       RABC12345 T01 保存任务笔记 │ 任务笔记已保存 · 1080 字
```

JSON 中的关键追踪字段：

| 字段 | 含义 |
| --- | --- |
| `run_id` | 与 SQLite 和笔记文件一致的完整研究编号；恢复时保持不变 |
| `execution_id` | 一次 `run()` / `resume()` 调用的编号；每次执行不同 |
| `task_id` | 当前任务；报告阶段为 `null` |
| `task_position` / `task_count` | 本次选定任务列表中的位置与数量；报告/规划事件的 `task_count` 表示该操作处理的任务数 |
| `stage` | 当前操作，例如“模型对话”“读取任务笔记” |
| `research_stage` | 持久化流程阶段：`plan/tasks/report/save_report/done` |
| `checkpoint` | 任务当前步骤；`checkpoint_saved` 中表示已提交快照的恢复位置 |
| `event` | 用于筛选的固定事件名，不需要解析中文正文 |
| `error_id` | 同一次异常的编号，上层中止提示可以关联到该异常 |

只有工作流提供 `execution_id` 和 `research_stage`；单独运行服务时可以为空。
`checkpoint_saved` 还包含 `status` 与 `task_checkpoints`，在数据库提交成功后才记录。
常用事件包括 `research_started`、`research_resume_requested`、`research_claimed`、
`task_started`、`task_skipped`、`summary_completed`、`report_completed`、`research_completed`。
笔记事件 `note_read.found` 区分存在/不存在，`note_written.note_created` 区分创建/替换。

例如，在 backend 目录按完整研究编号筛选（查询轮转文件时将文件名改成 `research.jsonl*`）：

```powershell
$researchId = "替换为完整的32位研究编号"
Get-Content -LiteralPath .\logs\research.jsonl -Encoding UTF8 |
    ConvertFrom-Json |
    Where-Object { $_.run_id -eq $researchId } |
    Select-Object timestamp, level, execution_id, event, research_stage, task_id, checkpoint, message
```

同一异常链由 `log_exception_once` 记录一次：控制台显示根因与 error_id，
文件保存完整堆栈。任务失败后另有一条调度提示，不重复堆栈。
如果业务失败后，保存失败状态又出错，两个独立异常分别记录；第二个事件
`failure_checkpoint_failed` 的 `related_error_id` 关联原始错误。
`research_failed.failure_saved` 表示本次失败状态是否成功落库；为 `false` 时，
数据库保留上次成功快照，不代表本次失败检查点已保存。
`run_cli` 用退出码 1 结束失败 demo，避免未处理异常再次刷屏。
普通库调用仍然抛出异常，由宿主应用决定如何处理。

配置只替换本项目的 Handler，保留测试框架或宿主 Handler。
宿主已有控制台 Handler 时应决定保留哪些输出，避免多个 Handler 重复展示。
日志按进程写文件；多个服务进程应配置不同目录或接入集中日志收集。
