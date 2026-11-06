# 日志使用

所有 demo 通过 `run_cli(main)` 启动。其他入口调用一次 `setup_logging()`，业务模块继续使用 `logging.getLogger(__name__)`。

```python
from deep_researcher.logging_config import setup_logging

setup_logging()                       # 控制台 INFO，JSON 文件 DEBUG
setup_logging(console_level="DEBUG")  # 展开底层调用和控制台异常堆栈
setup_logging(log_dir=None)           # 只输出控制台
```

控制台列：时间、等级、模块表情与简称、研究编号、任务编号、正文。
颜色在支持的终端中自动启用；重定向输出不带颜色，`NO_COLOR` 可关闭颜色。
终端编码无法显示表情时使用普通字符，JSON 文件始终使用 UTF-8。

默认日志：`backend/logs/research.jsonl`。单文件最大 5 MiB，保留 3 个备份。
`DEEP_RESEARCH_LOG_DIR` 或 `log_dir` 可更换目录；
`DEEP_RESEARCH_LOG_LEVEL=DEBUG` 可改变默认控制台等级。
文件时间使用 UTC，控制台显示本地时间。

```powershell
$env:DEEP_RESEARCH_LOG_LEVEL = "DEBUG"
.\.venv\Scripts\python.exe .\demo\demo_deepresearcher.py
```

新服务的同步入口使用 `@log_operation("阶段", task_parameter="task_id")`，
自动绑定阶段和任务编号，在服务边界记录异常。工作流每次运行生成研究编号。
`with log_context(...)` 支持嵌套上下文，在退出或异常时恢复。
ContextVar 隔离不同异步任务；新线程需要显式传递上下文，当前工作流串行执行。
装饰器本身用于同步函数。

INFO 记录研究进度、服务结果和持久化位置；DEBUG 记录 Agent 迭代、工具注册、
模型用量和底层文件操作。第三方库默认只显示 WARNING 及以上。

同一异常链由 `log_exception_once` 记录一次：控制台显示根因与 error_id，
文件保存完整堆栈。任务失败后另有一条调度提示，不重复堆栈。
`run_cli` 用退出码 1 结束失败 demo，避免未处理异常再次刷屏。
普通库调用仍然抛出异常，由宿主应用决定如何处理。

配置只替换本项目的 Handler，保留测试框架或宿主 Handler。
宿主已有控制台 Handler 时应决定保留哪些输出，避免多个 Handler 重复展示。
日志按进程写文件；多个服务进程应配置不同目录或接入集中日志收集。
