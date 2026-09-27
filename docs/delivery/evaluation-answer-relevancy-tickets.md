# 完整回答评估与 Answer Relevancy Tickets

状态：IMPLEMENTED_PENDING_VALIDATION，2026-09-27；EVAL-01 至 EVAL-05 已实现并进入 CI 验证，EVAL-06 仍需人工确认样本与显式预算的真实 Judge 运行。
唯一验收依据：[Spec](../design/09-evaluation-answer-relevancy-spec.md)。
按 EVAL-01 → 02 → 03 → 04 → 05 → 06 执行，避免先接门禁再验证 Judge。

## EVAL-01：建立完整回答输入与版本契约

- 优先级 P0；无依赖。
- 范围：`backend/app/evaluation/judge.py`、`runner.py`、相关测试；读取现有 QA detail 与会话持久化路径，避免修改在线问答行为。
- 工作：定位用户可见正文真实字段，提供 answerText、实际消息历史、expectation；固定 FINAL_TURN_WITH_HISTORY；区分空白、缺失、超限；保留 persona/ACL 检查；定义 v2 manifest 与 v1 排队任务兼容。
- 验收：fake gateway 捕获的正文与 QA 输出一致；历史包含实际助手回复；单轮、多轮、缺失、超限、来源撤权均有测试；v1 运行不被静默按 v2 判分。
- 完成证据：字段映射说明、样例请求（合成数据）、定向测试结果。

## EVAL-02：实现完整答案判定及独立 Relevancy

- 优先级 P0；依赖 EVAL-01。
- 范围：`judge.py`、`backend/tests/test_evaluation_quality.py`。
- 工作：同一次调用输出原 facts/claims 与三个新判定对象；严格验证枚举、索引和字段类型；实现正文/claims 对齐、题型规则、未知传播；Relevancy 不参与 correctness。
- 验收：覆盖 Spec §4/§7 语义反例；特别验证“切题但事实错”“必要解释不偏题”“正文漏 claim 不通过”“无依据 Gold 不自动判对”；零 claims 不产生 faithfulness=1；Relevancy 单独失败/不确定不抬高成功率。
- 完成证据：rubric v2、解析契约、对 v1 行为差异说明和 fake gateway 回归。

## EVAL-03：统一结果、汇总与历史兼容

- 优先级 P0；依赖 EVAL-02。
- 范围：`runner.py`、`metrics.py`、`backend/tests/test_evaluation.py`、门禁回归测试。
- 工作：持久化新维度，复用已有自动/人工记录；按维度采用最新人工标签；保留 taskSuccessRate 分母，增加 plannedTaskSuccessRate、Relevancy 分布/覆盖和输入版本；更新 Judge 预算估算，保留实际成本归属。
- 验收：1 成功、1 未知、1 执行失败场景断言全部分子分母；零样本、部分完成、取消、多 repeats、纯检索、旧报告均处理明确；未知不得补零；Relevancy 不改变既有 gate；不同 rubric 比较被标为不兼容。
- 完成证据：报告 JSON fixture、聚合与现有 `scripts/check_evaluation_gate.py` 回归结果。除兼容所需外不扩展门禁策略。

## EVAL-04：人工校验标签与差异统计

- 优先级 P1；依赖 EVAL-03。
- 范围：现有 review API/校验、runner/metrics、评估测试、合成校验 fixture。
- 工作：扩展现有 review JSON 标签，保留自动与人工各自结果；实现 correctness 与 Relevancy 混淆矩阵、一致率、样本数、FPR/FNR 及比较覆盖率；人工复核按维度独立提交，未填写维度不得被默认 UNKNOWN 覆盖。严格盲评 UI 不属于当前已实现范围。
- 验收：使用手算矩阵验证公式；未知、缺失标签、零分母、复核修订、只覆盖一个维度均有测试；合成标签明确不是人工 Gold；不新建表、不引入默认多次投票。
- 完成证据：手算 fixture、统计测试与校验集说明（代表性样本/难例分开）。

## EVAL-05：在现有报告与页面展示

- 优先级 P1；依赖 EVAL-03、04。
- 范围：现有评估详情/列表/复核页面、对应 API 类型和导出；实现前定位真实前端文件，不另建 dashboard。
- 工作：显示各判定与理由、自动/人工差异、观察指标标识、已判定与全计划成功率、覆盖率、版本、FINAL_TURN_WITH_HISTORY 范围；人工复核编辑器从空 `labels: {}` 开始，只提交明确修改的维度。列表当前仍可见 effective/自动结果；严格 blind-review 需后续单独实现。
- 验收：正常、偏题、未知、旧报告、纯检索、只有 correctness 人工标签等界面均可理解；页面与 JSON 报告数值一致；长理由/空字段不破坏布局；引用/历史可读性保持原权限边界；只改一个人工维度时其他维度保持不变。
- 完成证据：前端定向测试、构建结果、上述状态的界面截图（合成数据）。

## EVAL-06：人工确认样本与有预算的真实 Judge 校验

- 优先级 P1；依赖 EVAL-01 至 05。
- 范围：小型校验集、手动运行说明、交付报告与 `docs/rag-evaluation-guide.md` 的实际行为更新。
- 工作：准备至少 30 条覆盖 Spec §7 的候选样本，经人工确认后标记为 Gold；保存标注来源与版本；显式设置模型/预算后执行真实 Judge；输出原始计数、混淆矩阵、误判示例、未知情况和实际费用。
- 验收：代表性样本与难例分别报告；没有人工确认或模型凭据/预算时记录待办，不将 fake 结果作为完成证据；说明小样本局限。验证旧事实/证据能力是否因新 rubric 退化，不以新指标掩盖旧指标问题。
- 完成证据：人工标注记录、运行 manifest、脱敏校验报告、已知限制。Relevancy 仍为 OBSERVE_ONLY；门禁放行阈值与上线决策不属于该任务。

## 整轮完成检查

- [x] EVAL-01 至 05 已实现；review 后补充了历史来源二次授权、ABSTAIN `abstainReason` Gold 契约和按维度人工复核，最新分支继续由 PR CI 验证。
- [ ] EVAL-06 人工与真实模型证据齐备；当前明确未完成：现有 30 条样本尚不能冒充人工确认 Gold，且未在本变更中启动付费 Judge。
- [x] 旧结果可读，旧排队运行按 manifest.rubricVersion 保留 v1 路径；新增 plannedTaskSuccessRate、Relevancy 覆盖与自动/人工对比。
- [x] 未引入新框架、默认多次 Judge、自动收费重试或新的门禁阈值。
- [x] 文档区分已实现、CI 验证与仍待人工/真实模型验证，不宣称生产就绪。
