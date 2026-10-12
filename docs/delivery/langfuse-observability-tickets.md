# Langfuse 接入与自建评估增强 Tickets

状态：LF-T01～06 已完成；EOBS-01/02 已实现，EOBS-03 显式实验/校准入口及真实小样本 pilot 已完成，尚未充分校准。日期：2026-10-12。
EOBS 与真实模型证据：[后续实施记录](evaluation-observability-implementation-20261012.md)。
LF 首期证据及测试边界：[实施记录](langfuse-implementation-status-20261012.md)。后续已完成真实供应商合成联调；生产部署与生产负载未验收。
唯一验收依据：[Spec](../design/11-langfuse-observability-spec.md)，基线 `main@7a83f72`。
按 `to-tickets` 拆解；本文件为仓库任务清单，没有创建外部工单。

## LF-T01：建立 SDK、进程生命周期与安全导出基础

- 优先级：P0；blocked-by：无。
- 来源/目标：Spec §4.1、4.3、4.8；LF-AC01、04、05、09。
- 实现范围与上下文：`backend/app/telemetry.py`、`observability.py`、`main.py`、`runtime/worker.py`、依赖清单与 `.env.example`；目前 API 初始化 OTel，独立 Worker 仅初始化日志。读取现有配置和日志测试，不重写业务执行器。
- 工作：验证并锁定 Langfuse SDK 与现有 OTel/LangChain/LangGraph 的依赖兼容；共用一个应用 provider 幂等接入 SDK，保留原 OTLP；增加默认关闭开关、明确项目 scope 与导出白名单、独立 HMAC secret 配置及版本，未配置时省略身份字段；处理异常 event 清洗、线程安全、有界队列、网络与关闭等待；不启用自动 callbacks。
- 验收：API/内嵌/独立 Worker 重复初始化不重复导出；关闭/缺配置路径无 Langfuse 网络；捕获实际 payload 证明 input/output/metadata/events/exception 均无注入的正文或密钥；清洗异常丢弃观测；SDK/exporter 故障不改变业务异常或返回。项目父节点不被 scope filter 删除。
- 完成证据：锁定依赖和兼容性结果、合成 payload fixtures、针对性生命周期/故障/隐私测试、各进程的初始化及关闭配置。
- 风险：SDK masking 与 scope 规则随版本变化；不能仅验证日志/界面，不捕获真实公司数据以调试脱敏。

## LF-T02：关联 Worker 执行批次与逻辑 QA run

- 优先级：P0；blocked-by：LF-T01。
- 来源/目标：Spec §4.2、4.8；LF-AC02、05、09。
- 实现范围与上下文：`runtime/worker.py`、`qa/service.py`、`qa/execution.py`、`qa/agent.py`、现有 `run_event.payload`；复用 job attempt/lease_epoch 和当前 fencing。
- 工作：领取/恢复批次创建 root trace；会话使用 HMAC session 标识；追加可选版本化 OBSERVABILITY_TRACE 事件与采样/目标实例信息；保留本地原 ID 映射；HITL 等待关闭批次，resume 使用新 trace；终态关联指向实际产出答案的批次。仅运行期传播 SDK context，不持久化对象或 token。
- 验收：正常、失败重领、恢复、HITL、取消、lease 丢失和并发多租户 fixture 可追踪，旧 worker 不污染新批次；未采样/记录失败明确不可关联；关闭开关与旧事件可读。当前 API 入队与 Worker 通过业务关联，不要求共用一个 trace。
- 完成证据：批次关联 JSON fixture、并发/恢复定向测试、合成 trace 树示例。
- 风险：业务事件写入不能成为完成条件；若现有事件入口缺 fencing，在本票仅补该观测事件所需的保护，不借机重构全部事件。

## LF-T03：覆盖模型外发、工具、重试与用量

- 优先级：P0；blocked-by：LF-T01、LF-T02。
- 来源/目标：Spec §4.4；LF-AC03、04、06、09。
- 实现范围与上下文：`qa/model_usage.py`、`model_gateway.py`、`retrieval/embedding.py`、`qa/agent.py` 及定向测试；先核对 Chat、工具模型、Judge、Embedding、Rerank 的实际 tracked 入口。
- 工作：每次真实调用建立 generation并关联原 call_id；分别记录 retry/fallback、实际 alias、usage 来源、缓存量及状态；拒绝外发保留事件；工具 span 保留类别/mock；费用读取原结算结果，币种不匹配不写原生 cost。禁用网关重复 tracing，不添加默认模型调用。
- 验收：合成模型失败后 fallback、重试、预算/权限拒绝各有正确节点；一次实际请求一个 generation且计费/调用次数不变；缺 usage/缓存、真实零缓存和未知费用有区别；固定模拟业务失败与工具执行错误不混淆。关闭/损坏 exporter 不触发模型重试。
- 完成证据：调用与 generation 对照表、usage fixtures、原预算/取消/来源检查回归结果。
- 风险：不能将 pricing alias 自动估价视为供应商结算；严格保留 model_call 所属 run/job 约束。

## LF-T04：记录检索阶段与实际上下文诊断

- 优先级：P1；blocked-by：LF-T01、LF-T02。
- 来源/目标：Spec §4.4、4.5；LF-AC04、05、07、09。
- 实现范围与上下文：`retrieval/rag_graph.py`、`pipeline.py`、外发消息构造边界、`evaluation/runner.py` 及现有私有制品/JSON；当前纯检索评测使用 pipeline，Agent search 使用图。
- 工作：为真实执行阶段增加 span及检索调用序号，显式传播 Dense/BM25 线程 context；在受控评估保存有序 canonical Chunk/Build/parent/child/位置数据，标记配置上限和不完整；在实际消息外发前记录被保留的来源区间/上下文版本/裁剪信息，保持 ACL。默认向 Langfuse 只发送白名单计数与版本，原文/排名快照留本地。
- 验收：同 run 多次 search 和并行不同 run 不串阶段；构造重排丢证据与上下文裁证据两例，可从快照区分；最终 evidence 不被误当实际上下文；权限变化拒绝内容读取；诊断开关不改变排名/答案/模型调用，超限明确不完整。阶段评分尚不在本票实现。
- 完成证据：两个合成诊断快照、线程传播/权限/超限测试、开启关闭对照。
- 风险：parent expansion 与字符串压缩会改变实际区间；映射无法确定时标未知，不生成伪精确 provenance。

## LF-T05：显式同步已有评估分数到对应 trace

- 优先级：P1；blocked-by：LF-T02；调用与用量视图验证推荐在 LF-T03 后。
- 来源/目标：Spec §4.6；LF-AC08、09。
- 实现范围与上下文：已有 evaluation report/results/review 读取及一个显式同步入口；复用现有自动/人工结果与 rubric version，不改 Judge prompt、聚合公式或在线 review API 的响应链。
- 工作：读取最终 QA 批次与目标实例；复核 persona/来源；categorical 与 numeric 分开，自动/人工命名空间和版本分开；稳定 score ID 支持重跑，人工 revision 保留；输出 sent/skipped/failed。缺数值、旧报告、未采样或关联缺失有明确跳过原因。
- 验收：相同批次重跑不重复；人工修订不抹自动结论；null 不补零、UNDETERMINED 保持未知、纯检索不伪造答案分；撤权不上传；服务失败不修改评估结果；返回明确上传计数，不将 SDK 入队等同服务端已收到。
- 完成证据：合成 report→score 对照、幂等/版本/权限测试、目标服务端收件确认方法。
- 风险：显式同步为 best-effort，首期不保证自动补送；Langfuse 采样统计不能替代完整报告及门禁。

## LF-T06：私有部署、合成端到端验收与回滚说明

- 优先级：P1；blocked-by：无（环境准备）；整体验收依赖 LF-T01～LF-T05。
- 来源/目标：Spec §2、4.3、4.8、5；LF-AC01～10。
- 实现范围与上下文：独立可选 Langfuse 部署文件、配置说明、`backend/README.md` 与交付记录；不把服务强制加入业务启动依赖，不复用业务数据库/密钥。
- 工作：按官方配置锁定服务端及依赖镜像版本，独立私有存储与 project，开发端口绑定回环地址；设置保留/访问、数据模式、采样、网络/关闭上限；先完成合成模式，不生产部署。说明启用、停机、显式评分同步和关闭回滚步骤；更新源码导读只描述已实现的部分。
- 验收：合成题真实经过 Worker 到私有 Langfuse 并核对树/用量/阶段/评分；独立进程初始化有效；scope/隐私及全部故障反例通过；停 Langfuse 后同题业务结果一致；采样视图和本地报告的分母分开；观测开关对照记录样本数、并发、P50/P95 与额外资源。没有 SLO 不声称性能达标。
- 完成证据：LF-AC01～10 逐项映射、版本/配置清单、脱敏端到端截图或收件记录、故障与性能对照、已知限制。真实供应商调用未做时单列未实测。
- 风险：实例运行环境/访问策略在执行时锁定；无容器或服务环境时环境验收保持未完成，fake exporter 不能代替它。内容模式需确认合成来源和访问隔离。

## EOBS-01：统一自建评估样本与指标解释契约（后续）

状态：IMPLEMENTED。版本 eobs-v1；真实历史与实际上下文区分、未知/失败分母、读取撤权和旧指标兼容已验证。

- 优先级：P2；blocked-by：无；不阻塞 LF 首期。
- 来源/目标：Spec §4.7；EOBS-AC01。
- 实现范围与上下文：`evaluation/runner.py`、`metrics.py`、相关内部样本映射和报告可选字段；保留 v1/v2 rubric 及当前 API。只借鉴 Ragas 设计，不新增依赖或通用指标注册平台。
- 工作：从已持久化 QA/实际历史创建版本化内部样本，区分实际上下文和最终引用；指标显式输入、适用状态、方法/版本、分子/分母/错误码；TOOL/mock 独立类别；缺旧字段标未知；聚合展示可计算与失败覆盖，不静默补零。
- 验收：单轮/多轮、拒答、冲突、纯检索、历史缺字段、评估错误 fixtures 符合契约；仍仅评分最终轮；所有已有成功率、人工校准、门禁与历史读取保持原义。
- 完成证据：契约说明、输入输出 fixture、向后兼容及手算分母测试。
- 风险：新增字段不得被称为已恢复历史真实模型上下文；无需为此新增数据库表。

## EOBS-02：计算阶段事实组覆盖与实际上下文覆盖（后续）

状态：IMPLEMENTED。stageCoverage、parent/child 文本对齐和真实发送区间覆盖已实现，手算及实际报告回归通过。

- 优先级：P2；blocked-by：EOBS-01、LF-T04；不阻塞 LF 首期。
- 来源/目标：Spec §4.5、4.7；EOBS-AC02。
- 实现范围与上下文：`evaluation/metrics.py`、`runner.py`、阶段诊断快照和可选报告展示；复用现有 Gold evidenceGroups 和来源匹配。
- 工作：各阶段分别计算命中/事实组 Recall@K，实际上下文按真正保留的证据区间计算覆盖；组内替代来源命中即覆盖，重复片段不重复计数。指标携带 stage、k、scope/version、完整状态、分子/分母；与现有最终检索指标并列，不改旧定义。
- 验收：手算排名、替代来源、重复覆盖、parent/child、裁剪移除 Gold quote、空 Gold、来源版本不符及不完整上下文样例结果明确；新指标不适用/未知单独展示；不得把 Ragas Context Recall 当本项目 Recall@K。
- 完成证据：公式、手算 fixtures、重排和裁剪两类合成归因报告。LLM 语义上下文 precision/recall 不在本票默认新增。
- 风险：source unit/quote 与真实发送区间无法准确匹配时不计算确定分数；高覆盖也不证明答案正确。

## EOBS-03：试验独立正文断言提取与人工校准（后续）

状态：IMPLEMENTED_WITH_PILOT。显式预算/任务/两阶段实验、人工修订与校准统计已实现；30 条合成资料获用户确认，一个真实漏提反例完成两断言对照。未全量执行 30 条，不宣称生产 Judge 改善或充分校准。

- 优先级：P2；blocked-by：EOBS-01；不依赖 Langfuse，不阻塞 LF 首期。
- 来源/目标：Spec §4.7；EOBS-AC03。
- 实现范围与上下文：小型脱敏校准集、独立诊断评估入口、`evaluation/judge.py`/ModelGateway 的必要复用；现有 Judge 已检查正文/claims 双向对齐。
- 工作：参考 Ragas“正文抽取断言→证据支持”思路设计独立提取 rubric，比较生成 claims 漏提情况；所有新增模型调用经过预算和权限，显式手动执行；预先记录模型/提示词/契约版本，保留现有 Judge。准备至少 30 条候选，人工确认后才能称 Gold。
- 验收：包含漏提错误断言、无依据正文、合理拒答、错误拒答、多轮指代、冲突、工具 mock、中文数值/否定/条件；报告人工一致性/误判/未知/新增调用与费用，缺 Gold 或预算不执行真实评分；未完成校准不修改 correctness 或门禁。
- 完成证据：人工标注及来源、版本化实验报告、失败示例和是否继续的证据；仅合成测试不得宣称真实 Judge 改善。
- 风险：独立提取仍是模型判断；一次提取异常不等于业务答案错误，不增加默认投票/多裁判或新 embedding 切题指标。

## 依赖与完成标准

真正阻塞：LF-T01 → LF-T02 → LF-T03/LF-T04/LF-T05；LF-T01～05 → LF-T06 整体验收。LF-T06 环境准备可从一开始进行。后续：EOBS-01 → EOBS-02/EOBS-03，且 LF-T04 → EOBS-02。LF-T03 → LF-T05 仅为推荐验证顺序。

- [x] 首期 LF-T01～06 已实现，LF-AC01～10 有逐项合成证据。
- [x] 关闭/故障/隐私/恢复/租约、未知费用、采样与人工修订反例通过。
- [x] 私有实例已锁版本与访问策略；默认元数据模式；没有公司正文或业务凭据进入提交/观测。
- [x] MySQL 权限、预算、质量与历史评估仍是权威；未启用托管 Judge 或 Ragas。
- [x] EOBS-01/02 已实现；EOBS-03 入口、已确认合成数据和真实 pilot 完成；未充分校准，不升级门禁。
- [x] 文档、真实 exporter 载荷、私有合成收件分别有证据；真实模型质量明确未实测。
