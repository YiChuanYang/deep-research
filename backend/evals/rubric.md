# Deep Researcher 人工评测规则

## 一、研究要点覆盖

每份报告先根据研究主题列出 3～5 个预期要点。

每个要点按照以下规则评分：

- 0 分：完全没有回答。
- 1 分：提到了相关内容，但不完整、不清楚或缺少关键条件。
- 2 分：回答完整，和研究主题直接相关。

coverage_ratio：

coverage_score / coverage_max

这个指标只表示预期要点覆盖程度，不表示事实一定正确。

## 二、证据充足性

evidence_sufficiency 可以填写：

- enough：证据足以支持主要结论。
- limited：有部分证据，但覆盖不完整。
- insufficient：证据明显不足。
- uncertain：无法确定。

## 三、结论级引用检查

在 claims 中逐条摘录报告正文中的事实性结论。

每条结论需要填写：

- text：从报告中原样复制的句子。
- citation_ids：该句中实际出现的引用编号。
- verdict：supported、partial、unsupported 或 uncertain。
- reason：人工判断理由。

判定规则：

- supported：引用材料直接支持该结论。
- partial：材料只支持结论的一部分，或结论表达超出了材料。
- unsupported：没有证据支持。
- uncertain：材料含义不清，或多个来源存在冲突。

## 四、限制处理

limitations_score：

- 0 分：没有说明资料不足，或者把不足证据说成确定事实。
- 1 分：提到了部分限制，但没有明确影响。
- 2 分：清楚说明资料不足、未完成任务和结论边界。

## 五、注意事项

自动引用检查通过，不等于事实正确。

不要把 pending、invalid_review 或 export_failed 当作 0 分。

历史运行没有完整的模型耗时和 token 统计时，填写 N/A，
不要填写 0。