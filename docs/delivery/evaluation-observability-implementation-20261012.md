# EOBS 评估增强与真实模型联调（2026-10-12）

基于 PR #16 的 Langfuse 首期，实现 EOBS-01/02，以及 EOBS-03 的显式实验、人工校准入口和一个真实模型 pilot。没有引入 Ragas、迁移评估执行器或修改现有门禁。

## 样本与指标契约

`evaluation/observability.py` 的 `eobs-v1` 样本保存最终问题、真实授权历史与 run 引用、完整回答、claims、persona/snapshot/config、最终引用及实际模型上下文。存于私有 `judge.evaluationSample`；指标描述存于 `metrics.metricDiagnostics`。样本中的 TOOL/DOCUMENT、mock、缺失回答与空回答分别表示。旧数据没有外发快照时保持 UNKNOWN，不用最终引用回填实际上下文。

读取新样本时重新核对 persona、来源及历史；停用/撤权后移除新样本正文。REDACTED 导出不带 `judge.evaluationSample`。原 correctness、taskSuccess、Recall@5、人工复核、v1/v2 rubric 和门禁字段不变。

指标含 name/version、scope、requiredInputs、method/rubricVersion、numerator/denominator、value/status/errorCode；状态为 OK / NOT_APPLICABLE / UNDETERMINED / ERROR。新增聚合 `metricCoverage` 展示计算、未知、不适用和错误数；均值只来自 OK，不把缺失或失败补零。多次检索/外发的 observation 分母与 plannedCases 分开，不能替代最终轮成功率。

## 阶段与实际上下文覆盖

`metrics.stageCoverage` 的 stageFactRecall = 已覆盖的 Gold 事实组数 / Gold 事实组数。组内替代来源命中任一即可；重复候选不重复计数，doc/version 必须一致。

| scope | 输入与边界 |
| --- | --- |
| CANONICAL_CANDIDATES | Dense/BM25、融合、重排等有序 canonical 候选，k 来自配置快照 |
| RETRIEVAL_CONTEXT | parent 扩展、预算及最终检索输出；私有文本摘要与 canonical parent/child 对齐，正确识别退回 child 的情况 |
| SENT_STRUCTURED_DOCUMENTS | 真正外发消息的结构化文档证据，只搜索 EXACT 区间内的文字；不包含无法重建的历史或 TOOL |

空 Gold 为 NOT_APPLICABLE；来源无法解析、未知区间或不完整快照为 UNDETERMINED。`sourceRangesComplete=true` 仅表示结构化文档部分可计算，整个模型历史仍 `complete=false`。这不是 Ragas 的语义 Context Recall，也不证明答案业务正确。

手算与实际报告测试覆盖替代来源、版本不符、重复命中、parent/child fallback、裁掉 Gold、空 Gold、未知范围和错误分母。诊断字段故障不改变现有业务/评分结果。

## 独立断言实验

`evaluation/assertion_diagnostic.py` 采用两阶段：

1. 从完整正文独立提取断言，输入没有原生成 claims；返回唯一原文子串，服务器确定 Unicode 区间，拒绝变换/歧义/重复片段。
2. 使用证据检查支持，并判断原 claims 是否包含每条断言。未知不伪造成功，合理拒答没有事实性断言时不计算满分。

版本 `independent-assertions-v1`，策略 OBSERVE_ONLY。原 correctness、评估结果和门禁不被改写。新增调用仍经过 ModelGateway/invoke_tracked 与现有预算，属于独立 EVALUATE job，不重复运行原评估。

管理员显式调用：

```http
POST /api/v1/admin/eval/runs/{evalRunId}/assertion-diagnostics
Idempotency-Key: 独立实验的唯一键
Content-Type: application/json

{"resultIds":["已完成的结果ID"],"maxCostCny":5}
```

只选择 1～30 个完成且可读取的 QA 结果，题集必须 HUMAN_REVIEWED；缺预算、未人工复核、纯检索、失败结果或撤权均不启动模型调用。Worker 每阶段重核 persona/ACL/snapshot/job lease，checkpoint 按当前 epoch 写回，已完成结果恢复时不重放。

读取 `GET /api/v1/admin/eval/assertion-diagnostics/{jobId}`；人工逐断言校准用 `POST /api/v1/admin/eval/assertion-diagnostics/{jobId}/calibration`，提交 labels：resultId、assertionIndex、support（SUPPORTED/UNSUPPORTED/UNCERTAIN）。保留 reviewer、时间、全部修订与最新 summary。agreement 排除任一方 UNCERTAIN；falseSupportedRate 的分母为人工 UNSUPPORTED。没有标签时为 NOT_CALIBRATED，没有可比标签时不补满分。

## 人工确认与真实 pilot

[30 条合成校准资料](../../datasets/eobs_assertion_candidates_30.md)覆盖漏提错误、无依据正文、合理/错误拒答、多轮指代、冲突、mock 工具、中文数值、否定、条件。当前对话用户于 2026-10-12 明确核对认可候选和建议标签；JSONL 保留确认来源及 reviewer。生成器拒绝覆盖已经复核的数据。它们是合成 Gold，不能冒充生产业务 Gold。

本次在用户授权的总额 5 元范围内，对一个已确认的漏提反例进行了真实模型独立提取/支持实验：

- 提取两条断言，第一条有支持，第二条“所有用户都可叠加其他优惠”没有支持并被原 claims 漏提。
- 两条与已确认参考文字精确对齐，paired agreement=2/2，falseSupported=0/1；这是两个断言的局部结果，不是总体 Judge 一致性。
- 两次真实调用，本地保守账本为 0.1418 CNY；未默认执行其余 29 条，也没有据此升级门禁。

记录见 [真实供应商与断言实验证据](evaluation-observability-real-acceptance-20261012.json)。API 单测另验证两阶段计费、幂等、人工修订历史及原结果不变。

## 真实 Worker / 供应商 / Langfuse 联调

复用了现有本地 LiteLLM 的 chat-main / judge-main 和已配置供应商，只用合成语料；不将业务库接入验收。最终短引用题真实 Worker COMPLETED，真实完整答案 Judge 返回并解析，目标实例收到 2 个 AGENT generation 和 1 个 JUDGE generation，input/output 均为空，供应商 usage 有记录，Judge cache read 的真实 0 被保留。

失败与修复也保留：首次模型引用越界被拒绝，恢复又触及次数上限；第二轮 QA 成功但 Judge 30 秒超时，账本保留未知；针对 Judge 将等待上限扩为 60 秒且保留调用 deadline，时限回归通过。第三轮 QA/Judge 成功，但验收夹具误用 v2 简写断言而失败；已改为真实 RUBRIC_VERSION，随后只读复核实际 generation、账本和解析经过，不重复已完成付费调用。修正后的整份 paid pytest 没有再次付费整跑，不能将该次脚本宣称为全绿。

使用保守验收费率 100/200 CNY 每百万 input/output，作为本地预算上界，**不是供应商实际价目或账单**。累计已结算账本 1.7188 CNY；30 秒超时调用按其私有消息字节数、最坏 JSON 转义、输出上限推导预留上界 2.743 CNY；合计 4.4618 CNY，低于用户 5 元总额。该超时的供应商实际费用仍未知，不记为零；没有核对供应商账单。

默认 CI 不执行付费调用。再次执行前需确认预算，安装 requirements 到 backend/.venv，并准备 `.env` 中已有网关及 `.local/langfuse-eval.env`：

```powershell
pwsh -File scripts/check_real_model.ps1 -MaxCostCny 5
pwsh -File scripts/check_real_assertions.ps1 -MaxCostCny 1
```

两条分别有自己的预算，示例不是授权同时消耗 6 元；实际剩余额度需扣除已有结算及未知预留，不盲目重跑。脚本运行在 child process，凭据不提交。本次不再发起付费请求。

## 验证边界

本地完整后端 220 passed / 3 skipped（私有 Langfuse、真实问答、真实断言均为 opt-in）；历史审查回归 32 passed；真实断言 opt-in pytest 1 passed。编译与 diff 检查通过。部署与 SDK 载荷仍沿用 PR #16 的验证。SQLite/BM25 合成验收不是生产 MySQL/Milvus 压测；一个 pilot 不是充分校准或模型收益证明。仍需在真实业务 Gold 上扩展类别、样本与供应商账单核对，才讨论生产 SLO 和质量门禁。
