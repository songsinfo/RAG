# EOBS 独立断言合成校准集（30 条已确认）

30 条合成候选及建议标签已于 2026-10-12 获当前对话用户明确核对确认。来源是合成资料，不是公司数据；确认不等于真实 Judge 已改善。

支持标签针对正文里的事实性断言；合理拒答可能没有事实性断言，支持率应为空。
错误拒答可以没有不忠实断言，却仍是业务错误；这两个维度分开。

| caseKey | 资料 / 来源 | 答案 | 建议支持标签 | 建议业务正确性 |
| --- | --- | --- | --- | --- |
| eobs-omitted_error-1 | 订单满100元减10元。 | 订单满100元减10元，所有用户都可叠加其他优惠。 | UNSUPPORTED | WRONG |
| eobs-unsupported_body-1 | 规则没有规定退款时限。 | 退款将在24小时内到账。 | UNSUPPORTED | WRONG |
| eobs-valid_refusal-1 | 无资料 | 资料不足，无法确定退款到账时间。 | UNCERTAIN | CORRECT |
| eobs-wrong_refusal-1 | 订单满100元减10元。 | 资料不足，无法确定优惠金额。 | UNCERTAIN | WRONG |
| eobs-multiturn_reference-1 | A款优惠10元，B款优惠15元。 | 你刚才问的B款优惠15元。 | SUPPORTED | CORRECT |
| eobs-conflict-1 | 来源一规定优惠10元，来源二规定优惠15元。 | 两份来源分别规定10元和15元，无法确定唯一金额。 | SUPPORTED | CORRECT |
| eobs-mock_tool-1 | 合成工具返回可用优惠金额10元，mock=true。 | 模拟工具返回优惠金额10元。 | SUPPORTED | CORRECT |
| eobs-chinese_number-1 | 优惠金额为10元。 | 优惠金额为11元。 | UNSUPPORTED | WRONG |
| eobs-negation-1 | 此优惠不可与其他优惠叠加。 | 此优惠不可与其他优惠叠加。 | SUPPORTED | CORRECT |
| eobs-condition-1 | 只有订单满100元时可享优惠。 | 所有订单都可以享受优惠。 | UNSUPPORTED | WRONG |
| eobs-omitted_error-2 | 订单满150元减15元。 | 订单满150元减15元，所有用户都可叠加其他优惠。 | UNSUPPORTED | WRONG |
| eobs-unsupported_body-2 | 规则没有规定退款时限。 | 退款将在24小时内到账。 | UNSUPPORTED | WRONG |
| eobs-valid_refusal-2 | 无资料 | 资料不足，无法确定退款到账时间。 | UNCERTAIN | CORRECT |
| eobs-wrong_refusal-2 | 订单满150元减15元。 | 资料不足，无法确定优惠金额。 | UNCERTAIN | WRONG |
| eobs-multiturn_reference-2 | A款优惠15元，B款优惠20元。 | 你刚才问的B款优惠20元。 | SUPPORTED | CORRECT |
| eobs-conflict-2 | 来源一规定优惠15元，来源二规定优惠20元。 | 两份来源分别规定15元和20元，无法确定唯一金额。 | SUPPORTED | CORRECT |
| eobs-mock_tool-2 | 合成工具返回可用优惠金额15元，mock=true。 | 模拟工具返回优惠金额15元。 | SUPPORTED | CORRECT |
| eobs-chinese_number-2 | 优惠金额为15元。 | 优惠金额为16元。 | UNSUPPORTED | WRONG |
| eobs-negation-2 | 此优惠不可与其他优惠叠加。 | 此优惠不可与其他优惠叠加。 | SUPPORTED | CORRECT |
| eobs-condition-2 | 只有订单满150元时可享优惠。 | 所有订单都可以享受优惠。 | UNSUPPORTED | WRONG |
| eobs-omitted_error-3 | 订单满200元减20元。 | 订单满200元减20元，所有用户都可叠加其他优惠。 | UNSUPPORTED | WRONG |
| eobs-unsupported_body-3 | 规则没有规定退款时限。 | 退款将在24小时内到账。 | UNSUPPORTED | WRONG |
| eobs-valid_refusal-3 | 无资料 | 资料不足，无法确定退款到账时间。 | UNCERTAIN | CORRECT |
| eobs-wrong_refusal-3 | 订单满200元减20元。 | 资料不足，无法确定优惠金额。 | UNCERTAIN | WRONG |
| eobs-multiturn_reference-3 | A款优惠20元，B款优惠25元。 | 你刚才问的B款优惠25元。 | SUPPORTED | CORRECT |
| eobs-conflict-3 | 来源一规定优惠20元，来源二规定优惠25元。 | 两份来源分别规定20元和25元，无法确定唯一金额。 | SUPPORTED | CORRECT |
| eobs-mock_tool-3 | 合成工具返回可用优惠金额20元，mock=true。 | 模拟工具返回优惠金额20元。 | SUPPORTED | CORRECT |
| eobs-chinese_number-3 | 优惠金额为20元。 | 优惠金额为21元。 | UNSUPPORTED | WRONG |
| eobs-negation-3 | 此优惠不可与其他优惠叠加。 | 此优惠不可与其他优惠叠加。 | SUPPORTED | CORRECT |
| eobs-condition-3 | 只有订单满200元时可享优惠。 | 所有订单都可以享受优惠。 | UNSUPPORTED | WRONG |

OMITTED_ERROR 的原生成 claims 只列出了满减事实，正文另加“所有用户都可叠加”错误断言。
MULTITURN_REFERENCE 是合成的指代例子；MOCK_TOOL 的工具返回也是合成资料，不能证明真实用户资格。
当前 annotationStatus=HUMAN_REVIEWED，确认来源与 reviewer 已保留于 JSONL；真实模型新抽取的断言只在与已确认文字精确对齐时得到人工标签。
