# 评测可信度与验收

## 评分

估算与启动接口支持 `judgeMode: HUMAN|LLM`，默认为 HUMAN。
HUMAN 模式仅自动识别状态不匹配；状态匹配不代表回答正确，未复核结果保留 UNDETERMINED。
LLM 模式通过已有 `judge-main` 网关执行 `full-answer-and-relevance-v2`：同时评估完整用户可见 `answerText`、标准事实、claims/evidence、Answer Relevancy、正文/claims 对齐和题型 expectation。评分范围为 `FINAL_TURN_WITH_HISTORY`，历史用于理解最后一轮，但只评分最后回答。
裁判缺项、重复索引、格式错误或调用失败不会变为通过。manifest 记录 `rubricVersion`、`inputContractVersion`、`evaluationScope` 和 `relevancePolicy`；旧 v1 运行继续按旧 rubric 解析。
不可答题不通过“无检索金标准”自动判对；ABSTAIN 只有在存在非空 `expected.abstainReason` 时才具备自动判 CORRECT 的 Gold 基础，否则保持 UNDETERMINED。拒答无事实 claims 时 faithfulness 为 N/A，而不是 1。

最新人工复核用于实时汇总，原自动评分及所有人工记录仍保留。Correctness、Answer Relevancy 和题型评价按维度独立采用最新人工值；未提交的维度不会被默认未知值覆盖。
`taskSuccessRate` 的分母为有明确判定的结果数，同时新增 `plannedTaskSuccessRate=successful/planned`，必须连同 `judgmentCoverage` 与 `completionCoverage` 一起读取。执行失败与语义 UNKNOWN 是两个轴，不能把执行错误直接当作语义 WRONG。
只有 1 道题已复核且正确时，即使已判定成功率 100%，也不能代表剩余题目通过。

## 费用

费用读取 model_call 的结算记录；固定占位价格不作为实际结算。
`knownCostCny` 是已知部分，`totalCostCny=null` 表示总额未知。
当前网关仍记录 fixed 占位价格，因此真实账单对账尚需接通。不能把未知费用当零。
检索流水线缺少调用归属时也保留未知；无模型 BM25 可以明确记零。
现有启动费用估算仍是静态估算，不构成供应商实际账单的硬预算保证。

## 比较与门禁

比较增加 mode、strict、repeats、environment、judgeMode、rubricVersion、inputContractVersion、evaluationScope、relevancePolicy 一致性检查。
检索命中同时要求 documentId、versionId 和引文匹配，不接受缺失来源的命中。
语义裁判需先在人工复核集上校准，自动标签不等于人工金标准。Answer Relevancy 当前为 `OBSERVE_ONLY`，不改变 correctness、taskSuccess 或发布门禁。

导出评测报告后执行（阈值由业务验收确定，以下仅为命令示例）：

```powershell
python scripts/check_evaluation_gate.py report.json --min-success 0.9 --max-cost 10
```

未完成、判定覆盖不全、费用未知或纯检索报告均不能通过回答质量门禁。
脚本返回非零退出码，可接发布流水线；普通 CI 运行其回归测试，不调用付费模型。

## 题集状态及剩余工作

本地自动生成的 300 条为 DRAFT 候选集，不能直接冻结为已审核金标准。
生成器保留标识符中的下划线和运算符，但问题范围、事实完整性、单事实组、长引文跨 chunk、
跨文档语义重复仍需逐题修订并在实际入库后验证。

仍需独立交付：逐题业务审核、真实模型裁判校准、供应商计费对账与预算控制、
全量端到端验收、模型及提示词部署快照、统计置信区间、线上反馈回流。
历史会话在重新发送给 Judge 前会逐个 QA run 重新校验来源权限和快照可读性，撤权或来源变化时失败关闭。现有语料快照与实际 QA 检索范围仍需要进一步端到端验证。
本次代码修复不构成企业级系统已验收的声明。
