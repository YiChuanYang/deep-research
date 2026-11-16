# 报告迭代真实回归材料

`report_cycle_hyperspectral.json` 来自研究 `67669d9717bd49a694eee4a6cd367350` 的已保存检查点和日志。

- `initial_state`：初始三项任务及当时的来源、证据，清除最终报告及笔记路径。
- `drafts`：原始两版报告；第二版仅增加六个换行。
- `review_responses`：三次模型返回的原始正文，含混填 omission 与补搜参数的错误结果。
- `bad_supplement`：当时的补充问题、检索词及照抄反馈的摘要。
- `supplement_evidence` / `supplement_sources`：补充任务实际拿到的材料。

这些是故障输入，不能作为正确答案或领域事实金标准。fixture 不含模型凭证、配置文件或用户本地笔记路径；测试读取它们而不依赖运行数据库。来源 URL 保留用于解释证据关联，离线测试不访问这些 URL。
