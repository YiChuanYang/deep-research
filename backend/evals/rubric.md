# Deep Researcher 人工评测规则

## 一、研究要点覆盖

固定题集使用 cases.v1.json 中预先写好的 3～5 个验收要点，并在运行时冻结。
评分时不得增删、改写或重排 point；只填写 score 和 reason。
历史报告导出的空白评分表才需要预先补充要点，不应根据报告内容倒推标准。

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

结论必须原样摘录正文，并包含该句实际使用的引用标记；每条尽量只表达一个可核查主张。
核对 evidence.json 中本次真正保存的 text，不能仅凭来源标题、网址或个人常识判 supported。
需要包含没有引用的事实性结论；这类结论的 citation_ids 为 []，verdict 为 unsupported。
这里的 unsupported 表示缺少系统提供的支持证据，不自动表示事实错误。

判定规则：

- supported：引用材料直接支持该结论。
- partial：材料只支持结论的一部分，或结论表达超出了材料。
- unsupported：没有证据支持。
- uncertain：材料含义不清，或多个来源存在冲突。

citation_coverage = 带引用的已核查结论数 / 已核查结论总数。
citation_support = supported 结论数 / 带引用的已核查结论数。
partial 不按半分计入支持率；有引用的 uncertain、partial 和 unsupported 仍保留在分母。
分母为 0 时指标为 N/A。

完整枚举全部需要核查的事实结论后，才填写 claim_inventory_complete=true。
只抽查部分结论时保持 false；两种范围的指标分开汇总，不把抽查结果称为全文质量。

## 四、限制处理

limitations_score：

- 0 分：没有说明资料不足，或者把不足证据说成确定事实。
- 1 分：提到了部分限制，但没有明确影响。
- 2 分：清楚说明资料不足、未完成任务和结论边界。

若内容已有充分支持，恰当交代适用范围即可，不要求凭空编造限制。

## 五、注意事项

自动引用检查通过，不等于事实正确。

不要把 pending、invalid_review 或 export_failed 当作 0 分。

各项 reason 应写明报告中的对应内容和判断依据，不只填写“正确”“一般”等空泛评价。
尽量由同一评审者按相同规则评审两组，并在评分时隐藏版本标签以减少偏好影响。
批次质量指标先对同题的有效重复取平均，再对题目等权平均，并同时展示有效样本数。

历史运行没有完整的模型耗时和 token 统计时，填写 N/A，
不要填写 0。
