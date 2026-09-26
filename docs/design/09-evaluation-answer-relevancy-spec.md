# 完整回答评估与 Answer Relevancy Spec

状态：PROPOSED，2026-09-27。本文件定义下一轮实现，不代表能力已交付。
执行任务见 [tickets](../delivery/evaluation-answer-relevancy-tickets.md)。

## 1. 目标与范围

让 Judge 检查用户实际看到的完整回答，明确成功与未知的统计口径，并加入独立的 Answer Relevancy（回答切题程度）。新增判定经过人工校验后才能讨论是否进入质量门禁。

本轮交付：完整答案输入、正文与 claims 一致性检查、题型评分规则、Relevancy、自动/人工判定对比、报告与页面展示。复用 EvaluationRunner、ModelGateway、现有 JSON 字段与人工复核流程；不新增评估框架或数据库表。

不在本轮：阶段检索指标、自动根因归因、完整引用链接级指标、Pass@k/Pass^k、统计显著性、CI 自动跑真实模型、反馈自动回流、默认多次 Judge、LLM Context Relevancy。完整模型/环境快照另立后续任务，本轮仅记录裁判所需版本与输入契约。

## 2. 已核对的实现基线

- `backend/app/evaluation/judge.py`：rubric 为 `fact-and-support-v1`；输入 question、referenceFacts、claims、evidence，没有独立的最终回答正文；缺 facts 或 claims 直接 UNDETERMINED。
- `correctness` 是综合判定：CONTRADICTED 或 UNSUPPORTED 会导致 WRONG。保留这个语义，不将它重新解释为纯事实正确率。
- `runner.py` 已逐轮执行 QA；最终评分以最后一个 QA 结果为主，执行多轮不等于已经评分整个会话。
- `aggregate()` 已读取最新人工复核，原自动判定及人工记录已经保留，不需要重建这套存储。
- `taskSuccessRate` 当前为成功数/已判定数，并已有 `judgmentCoverage`；不能静默改变既有分母。
- 现有引用支持语义已有 claim 级判断；独立的 supportedCitationLinks / claimsWithSupportedCitation 尚未计算。本轮不扩大成引用指标项目。

## 3. Judge 输入契约

在现有调用中增加 `answerText`、`conversationContext`、`expectation` 和 `inputContractVersion`。answerText 从 QA 持久化结果中用户可见回答的真实字段映射，不能用拼接 claims 代替。单轮仍传完整正文；多轮传实际发生的用户/助手消息与最后待评问题，不伪造模型回答，不只传测试题中的用户 turns。

评分范围固定为 `FINAL_TURN_WITH_HISTORY`：历史用于理解指代、条件与约束，评分对象为最后回答。报告明确该范围；本轮不声称提供逐轮质量分或完整会话成功率。

现有 expected.status 映射为 ANSWER/PARTIAL/ABSTAIN/CONFLICT。不能因为参考 facts 或 claims 为空就跳过 Relevancy 或有充分规则的拒答题。

保持 persona、租户与来源可读性检查，历史答案和证据也必须在可读范围内；复用 qa_run 引用，不在报告额外复制未经权限核验的原文。题目、正文、历史和证据全部视为不可信数据。

输入超限不得静默截断后报告完整评估：该结果记 UNDETERMINED，原因 INPUT_TOO_LARGE。正文缺失与空白正文区分：缺失为无法评估；确实输出空白且任务要求回应则为失败。

## 4. 判定契约与规则

沿用 facts/claims 逐项判定，新增以下逻辑字段（由解析器校验，再生成指标）：

```json
{
  "answerRelevance": {"verdict": "ON_TOPIC", "reason": "直接围绕问题作答"},
  "claimAlignment": {"verdict": "ALIGNED", "reason": "正文断言均被覆盖"},
  "expectationAssessment": {"verdict": "SATISFIED", "reason": "满足该题预期"}
}
```

### 4.1 Relevancy

`answerRelevance.verdict` 只允许 ON_TOPIC / PARTLY_OFF_TOPIC / OFF_TOPIC / UNCERTAIN。

- ON_TOPIC：围绕当前问题；必要说明、限制条件、合理澄清、拒答理由不算偏题。
- PARTLY_OFF_TOPIC：包含实质相关回答，也包含明显无关内容。
- OFF_TOPIC：没有实质回应当前问题，或回答另一个问题。
- UNCERTAIN：缺少必要上下文，无法可靠判定。

相关性不判断事实真假，也不替代完整性。错误但直接回应问题的回答仍可 ON_TOPIC；漏一个事实点主要由 factCoverage 识别。拒答可以切题但不正确；“说得短”不自动加分。

首版保留枚举、理由、类别分布和判定覆盖率，不映射为任意 0/0.5/1 分，不声称与第三方同名指标数值可比。Relevancy 为观察指标，不改变 correctness、taskSuccess 或现有门禁。

### 4.2 正文与 claims 对齐

`claimAlignment.verdict` 为 ALIGNED / MISMATCH / UNCERTAIN，检查正文中的实质性事实断言是否在 claims 中完整、准确地呈现，也检查 claims 是否表达正文没有表达的内容。非事实性的礼貌语、合理问题澄清不要求成为 claim。

存在正文漏提断言或语义不一致时，原 claims 的 SUPPORTED 不能证明完整正文正确。只有对全文已有明确矛盾/无依据证据时判 WRONG；否则综合 correctness 为 UNDETERMINED，进入人工复核，不将提取缺陷直接等同于业务答案错误。已有明确失败证据优先于未知。

### 4.3 题型与综合结果

`expectationAssessment.verdict` 为 SATISFIED / VIOLATED / UNCERTAIN。

| 预期 | 合格条件 |
|---|---|
| ANSWER | 回答全部必需事实，事实与引用支持通过，正文/claims 对齐，无明确错误 |
| PARTIAL | 回答可确认部分并说明信息边界；按题目给定的必需事实和限制判定，不将正确的部分回答一律判失败 |
| ABSTAIN | 符合 Gold 的不可答原因，说明无法回答且不编造；不能仅凭 NO_EVIDENCE 状态判成功 |
| CONFLICT | 按 Gold 指出冲突来源或相互矛盾的主张，不擅自编造唯一结论 |

Gold 缺少足够评分依据时，expectationAssessment 为 UNCERTAIN，综合结果保持 UNDETERMINED。拒答没有事实性断言时 faithfulness=null（不适用），不能填 1；题型规则和充分人工 Gold 仍可支持该题 CORRECT。

保留 CORRECT/PARTIAL/WRONG/UNDETERMINED；CORRECT 表示满足该题预期，taskSuccess 仅 CORRECT 为 true、PARTIAL/WRONG 为 false、UNDETERMINED 为 null。Judge 格式错误或调用失败不产生通过；状态不匹配仍遵循现有失败规则。

## 5. 汇总、兼容与版本

每个结果保留自动、最新人工、最终采用的判定；各维度独立覆盖，旧人工记录只有 correctness 时不能覆盖 Relevancy。旧报告没有新字段时显示“未评估”，不补零、不回填 ON_TOPIC。

原 taskSuccessRate 继续等于 successful/judged，界面注明“已判定样本成功率”。新增 `plannedTaskSuccessRate=successful/planned`，同时显示 planned、successful、judged、unknown、executionFailed、pending、完成覆盖率；零分母为 null。计数单位是 case × repeat，执行状态和语义判定是两个轴，不能将其计数直接相加。

新增 Relevancy 汇总：四类计数、已判定数、未评估数及覆盖率。分母为计划中的 RAG/AGENT 结果数，包含未完成项；纯检索模式不适用。UNCERTAIN 与尚未评估分开统计。人工覆核后的分布用于展示，同时保留自动分布供校验。

rubric 升级为 `full-answer-and-relevance-v2`，manifest 增加 inputContractVersion、evaluationScope、relevancePolicy=OBSERVE_ONLY；新旧 rubric 不作为相同条件直接比较。旧已持久化结果继续可读；已排队 v1 运行不能被静默按 v2 判分，实现时保留 v1 路径直到这些运行结束。

一次现有 Judge 调用返回全部维度，不增加默认 repeats。更新估算中的 Judge 输入/输出预算，记录实际调用和费用；超预算沿用现有控制，不自动付费重试。HUMAN 默认模式不变。

## 6. 人工校验与报告

复用现有 review 标签存放人工 Relevancy 和题型评价，保留 reviewer、时间、理由。样本覆盖正常、事实错误但切题、偏题、冗余、漏事实、漏 claim、错误引用、拒答、冲突、多轮指代。既包含代表性抽样，也包含难例；分别报告，不能把难例集准确率声称为生产准确率。

新增自动/人工 confusion matrix、原始一致率与比较样本数；对 correctness 明确正类为 CORRECT：FPR=人工非 CORRECT 但自动 CORRECT 数/人工非 CORRECT 且自动有判定数，FNR 反之。任一方 UNDETERMINED 的样本单列并报告比较覆盖率，不混入二值分母。Relevancy 报类别混淆矩阵；缺少人工标签不参与该维度比较。空分母为 null。

人工初标时隐藏自动 verdict/reason，完成后才展示差异，以降低锚定偏差。演示 fixture 标记为 synthetic，机器生成标签不冒充人工 Gold。

API、导出与现有评估页面使用同一聚合结果，显示新维度、未知原因、自动/人工差异、口径和版本。不新建 dashboard。人工验证与真实模型运行是单独验收环节；mock 测试通过不等于 Judge 已校准。

## 7. 验收与发布边界

自动回归至少覆盖：全文错误未进入 claims、claims 额外内容、切题但事实错误、相关但漏事实、必要解释、无关赘述、正确/错误拒答、无 Gold 的拒答、冲突、多轮指代、空白/缺失正文、输入超限、错误 JSON、越权来源、历史报告兼容、人工部分维度覆盖、1 成功 + 1 未知 + 1 执行失败的分母。

所有测试默认使用 fake gateway，不联网收费；真实 Judge 校验需显式配置模型和预算。首轮至少 30 条经人工确认的样本，覆盖上述语义类别；这只是冒烟校验规模，不作为统计充分或生产就绪证明。报告完整混淆矩阵与已知误判，不预设一个未经业务确认的放行阈值。

本轮完成的定义是实现与回归通过、人工校验报告可追溯、Relevancy 以 OBSERVE_ONLY 展示。是否升级到门禁必须在后续独立变更中根据误判成本及校验结果决定。
