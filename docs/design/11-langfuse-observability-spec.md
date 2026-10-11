# Langfuse 可观测性接入与评估设计借鉴 Spec

状态：SPEC_READY，尚未实现、部署或执行 Langfuse 验收。日期：2026-10-12。
代码核对基线：`main@7a83f72a0b71199ff8fbfbb4f63f2633c5139285`。
实施清单：[Langfuse Tickets](../delivery/langfuse-observability-tickets.md)。

## 1. Problem Statement

当前统一 Agent 经 Durable Worker 执行，包含知识检索、模拟业务工具、多轮上下文、模型重试、strict 验证和离线 Judge。运行完成及最终质量分不足以解释错误发生在哪一阶段，需要把一次实际执行中的模型、工具、检索和评分关联起来。

已核对的基础与缺口：

| 当前代码 | 已具备 | 本轮需要补齐 |
| --- | --- | --- |
| [telemetry.py](../../backend/app/telemetry.py)、[observability.py](../../backend/app/observability.py) | OTLP exporter、`rag.agent` scope、Agent/model/tool span | Langfuse sink、统一初始化和关闭、scope 过滤与数据策略 |
| [main.py](../../backend/app/main.py)、[worker.py](../../backend/app/runtime/worker.py) | API lifespan 初始化 telemetry；独立 Worker 初始化日志 | 独立 Worker 的 telemetry 生命周期、执行批次关联 |
| [model_usage.py](../../backend/app/qa/model_usage.py)、[model_gateway.py](../../backend/app/qa/model_gateway.py) | call_id、用量、缓存读取量、耗时、预算结算；日志不记录 payload | 每次实际调用的 generation、可解释的重试和 fallback |
| [rag_graph.py](../../backend/app/retrieval/rag_graph.py) | 阶段事件记录数量、耗时和开关 | 阶段 span；评估环境的候选排名及真正发送上下文诊断 |
| [runner.py](../../backend/app/evaluation/runner.py)、[judge.py](../../backend/app/evaluation/judge.py) | 题集/快照、完整答案 Judge、人工复核、校准、费用和比较 | 将已有评分关联至对应执行；补充样本与指标契约 |

现有自建评估已适配 ANSWER/PARTIAL/ABSTAIN/CONFLICT 和真实历史。引入另一套评估框架会产生重复口径和兼容成本。

## 2. Solution

已确认：先引入 Langfuse；不安装或调用 Ragas，只借鉴其样本契约、分维度评估和评分可解释性。MySQL 继续是权限、运行结果、预算、题集、评分及人工复核的权威来源，Langfuse 是可丢失、可重建的诊断视图。

首期交付 Langfuse 进程生命周期、执行关联、模型/工具/检索观测、已有评分的显式同步，以及私有环境的合成数据验收。默认关闭，启用后默认仅导出白名单元数据。不迁移题集、不启用 Langfuse 托管 Judge，不增加模型调用。

后续评估增强独立交付：统一内部样本契约、阶段事实组覆盖、正文断言提取诊断。它们不阻塞 Langfuse 首期，也不改变现有质量门禁。

首期部署选择为独立私有 Langfuse 实例，开发演示仅使用合成数据和回环地址。生产实例容量、保留期和访问成员由部署负责人配置；本规格不授权部署生产服务或上传公司正文。SDK 和服务端版本在兼容性验证后精确锁定，禁止直接使用 `latest` 作为交付版本。

## 3. User Stories

1. 作为维护者，我希望按 QA run 找到每次 Worker 执行及其调用树，以区分模型失败、工具失败、取消和恢复。
2. 作为维护者，我希望比较检索阶段与实际上下文，以定位必要证据在哪一步丢失。
3. 作为评估人员，我希望从已有质量判定跳到产生答案的执行记录，以保留原评分口径和人工修订。
4. 作为系统维护者，我希望关闭或损坏 Langfuse 不影响问答、租约、预算和已有评估结果。
5. 作为数据维护者，我希望观测系统默认不复制问题、公司资料、业务对象和提示词正文。

## 4. Implementation Decisions

### 4.1 职责与 OpenTelemetry 集成

复用现有 `telemetry.py` 和 `observability.py`，只在它们及实际调用边界扩展。每个进程只初始化一个应用 TracerProvider，Langfuse SDK 显式使用该 provider；已有通用 OTLP sink 可并存，不再次覆盖全局 provider，不叠加重复 processor。API lifespan、内嵌 Worker 和独立 Worker 共用幂等初始化入口，退出执行有界 flush/shutdown。

Langfuse 的默认 scope 过滤可能忽略现有 `rag.agent` span，必须显式允许项目 scope 并验证父节点没有被过滤；禁止全量导出 SQL、HTTP header 等第三方 span。首期手动在应用边界记录，不启用 LangChain 全图 callback 或 LiteLLM 全局 callback，以避免重复 generation 和自动捕获敏感图状态。将来启用 callback 必须重新证明数据策略及“一次调用一个观测”的契约。

官方支持复用现有 OTel，具体 SDK API 以锁定版本验证为准：[已有 OTel 接入](https://langfuse.com/faq/all/existing-otel-setup)、[scope 过滤与生命周期](https://langfuse.com/docs/observability/sdk/advanced-features)。

新增应用开关 `RAG_LANGFUSE_ENABLED=false`；启用时使用 `LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY`。配置缺失或初始化失败时降级为本地已有观测并记录不含敏感值的状态。现有 `OTEL_EXPORTER_OTLP_ENDPOINT` 的通用 collector 语义保留，不把它静默改为 Langfuse 专用路径。不开新通用可观测性框架。

### 4.2 执行身份与 trace 关联

逻辑单位为 `qa_run_id`；物理 trace 单位为一次 Worker 执行批次。每次领取任务、恢复或重新取得 lease 创建新 trace，通过同一个 qa_run、job 和会话标识关联，不让一个 span 跨越进程重启或 HITL 等待。

| 标识 | 本地用途 | Langfuse 表达 |
| --- | --- | --- |
| `tenant_id`、`conversation_id`、`segment_id` | 权限和会话定位 | 带环境/租户命名空间的 HMAC 标识；会话映射到 session |
| `qa_run_id`、`job_id` | 本地执行与任务关联 | HMAC 标识；原 ID 与 trace_id 的映射仅存本地 |
| `lease_epoch`、job `attempt` | 区分执行批次 | 元数据；不得用 model_call 的固定 attempt_no 冒充 job 尝试数 |
| `call_id` | 每次真实外发/预算结算 | 同一调用的 generation 关联标识；重试创建新 call_id |
| eval run/result、case/repeat、最终 qa_run | 评分归属 | HMAC 关联标识及指标版本 |

HMAC 密钥独立于业务/模型密钥，通过部署 secret 注入并版本化；不同环境隔离。未配置该密钥时不导出身份字段，也不降级为明文 ID。Langfuse trace_id/observation_id 使用合法 SDK/OTel 标识，不直接将 UUID 填入 OTel trace_id。

在现有 `run_event.payload` 追加版本化 `OBSERVABILITY_TRACE` 事件，保存真实 trace_id、执行批次、采样决定和目标实例标识；本地原租户/run 列提供关联。评分时使用最终产出答案的批次，早期失败批次只用于诊断。无采样/无 trace/映射失败时明确不可关联，不能声称上传成功。

记录事件沿用当前 lease fencing；不得让观测写入成为业务成功提交的前置条件。不把 SDK 对象、Span 对象或 token 放入 LangGraph checkpoint。API 入队请求与 Worker 不要求共享一个分布式 trace，首期通过本地业务关联定位。

### 4.3 观测内容及数据不变量

生产首期采用 `METADATA_ONLY`：仅允许环境、代码提交、组件/阶段名、版本、开关、数量、Token、用量来源、耗时、状态/错误码、HMAC 关联标识及已校验的数值。问题/回答/原文/完整消息、工具入参及结果、文档标题/路径、用户与订单等业务 ID、请求 header、URL 参数、异常消息/stack、Judge 理由均不得导出。

白名单适用于 input、output、metadata、attributes、span events、exception events 和 score comments。当前 `agent_span.record_exception(exc)` 可能包含原始异常信息，接入时须变更为安全错误属性或在 Langfuse 导出前删除该事件；本地已有安全日志继续独立处理。不得仅在应用 JSON 日志中脱敏后假定 span 已安全。

白名单清洗在发送前完成；清洗失败则丢弃该观测，不上传原数据、不影响业务调用。锁定版本的 SDK masking 用作补充防线；必须以实际导出载荷验证第三方 span 与异常字段，不能只检查 UI。参考：[Langfuse Masking](https://langfuse.com/docs/observability/features/masking)。

隔离评估环境可显式开启 `SYNTHETIC_EVAL` 内容观测，前提为选定题集/语料经过合成数据确认，并通过当前 persona/ACL；仅凭 ENVIRONMENT 名称不足以授权。设置内容上限、截断标识与来源范围，拒绝含未确认来源的请求；不静默截断后标为完整记录。关闭后新记录立即恢复元数据模式。旧内容副本的保留/删除按实例策略处理，应用撤权不会自动撤销 Langfuse 内容。

Langfuse 凭据仅在后端；首期不向普通用户暴露 trace 链接或建设浏览器代理。实例访问只授予维护人员，默认按部署环境分 project；不同租户的读者不能靠 metadata filter 实现隔离，需独立 project/实例并验证实际权限。未满足访问隔离的环境不能开启内容观测。

### 4.4 模型、工具、检索与费用

围绕 `invoke_tracked()` 的一次实际请求建立 generation，覆盖 Chat、Tool Calling、Judge、Embedding 和 Rerank；它们继续通过现有 ModelGateway/预算路径，不绕过权限检查。没有真实外发的预算/权限拒绝记录拒绝事件，不伪装成功 generation。重试和 fallback 分别记录实际 alias、call_id、状态和失败阶段，观测失败不触发模型重试。

手动 generation 与现有 `agent.model` span 保持父子关系。工具观测保留工具名、耗时、执行状态及 DOCUMENT/TOOL、mock/fixtureVersion 等类别，不把业务“不满足资格”当作执行失败；禁止把固定 fixture 说成真实查询。

费用使用已有 `model_call` 的 usage_detail、pricing_snapshot、settled_amount 和状态。供应商报告、byte-ceiling 估算、未知分别标记；缓存字段缺失不能填零。LiteLLM alias 可能没有 Langfuse 标准价格，不能使用默认价格推导已结算 CNY。未完成结算的 SDK 自动估价只能标为估算；首期仅传明确的供应商 usage 和已结算费用属性，不将 CNY 填入未核对币种的原生 cost 字段。

检索为 scope、query prepare、Dense、BM25、RRF、rerank、MMR、parent expansion、context budget、provenance/ACL 建立阶段 span，沿用现有图与事件，不更改检索结果。并行节点必须显式传播 context，不能假定线程中的 ContextVar 自动继承；每个 span 保留检索调用序号，避免多次 search 的阶段被混在一起。

### 4.5 阶段诊断数据

完整有序候选数据只在受权限控制的评估路径保存，复用现有评估 JSON/私有制品存储；不写入普通在线日志或默认 Langfuse payload。记录检索调用序号、stage、rank、canonical Chunk/Build ID、parent/child 对应、query/config 版本、候选数量、截断状态及来源位置。上限以该次配置的候选预算为准，超限明确标记不完整。

实际模型上下文单独记录其被保留的来源 ID、原文区间、消息组/上下文版本、裁剪原因和计数；从外发前的实际消息构造取得，不能用最终引用 evidence 列表替代。对持有模型调用权限的 persona 再核验来源；内容快照只用于确认的合成评估。Langfuse 元数据展示各阶段数量/耗时/裁剪情况，具体候选与原文仍从受控评估详情读取。

首期只采集诊断事实；阶段 Recall@K 与 Context Coverage 在后续 EOBS-02 实现，不用“候选数量变少”自动推断质量下降。

### 4.6 已有评分关联

提供显式执行的评分同步入口，读取已持久化且仍可访问的评估报告，将已有自动/人工标签和数值关联到最终 QA 批次。首期不在问答响应、Judge 完成或人工 review 请求中同步等待 Langfuse，也不建设持久 outbox。

categorical 分别保留 correctness、answerRelevance、expectationAssessment；数字保留原 factCoverage/faithfulness/检索指标定义。自动与人工使用不同命名空间及版本，人工修订按 review ID 单独保留，不覆盖自动判断。score ID 由目标 project、eval result、维度、rubric/metric 版本和 review revision 生成稳定标识；重复显式同步不能产生重复项。

null 指标不发送数值零；UNDETERMINED 可作为分类标签并保留原因码，不能转为失败/成功。只有实际存在的适用指标才能发送，纯检索评测不伪造回答质量。现有报告仍按 planned/judged/unknown 分母汇总，Langfuse 的采样平均值不能代替报告。

同步前复核 persona、来源权限和目标实例；输出 sent/skipped/failed 计数及安全错误码。目标 trace 未采样或没有有效关联时跳过；上传失败可以显式重跑同一批次，不修改评估/QA 终态。参考：[SDK 自定义评分](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)。

### 4.7 借鉴 Ragas 的设计，不引入依赖

下列为设计借鉴与本项目决定，不声称本项目采用了 Ragas 实现或与其同名指标数值可比。

| 可借鉴设计 | 现有基础与差距 | 决定及交付阶段 |
| --- | --- | --- |
| 显式区分单轮样本、真实多轮消息和 reference | 已重建真实历史，但 QA evidence 不等于实际模型上下文 | EOBS-01 建立内部版本化样本契约，复用现有数据，不新增框架 |
| 按检索、上下文、生成分别评分；指标声明输入前提 | 已有事实组 Recall 和全文 Judge；缺少阶段覆盖 | EOBS-02 按各阶段有序候选及真实发送区间计算事实组覆盖；不能与 Ragas Context Recall 混用 |
| 从完整回答抽取断言再判断上下文支持 | 已有正文/claims 对齐检查，但 claims 由生成流程提供 | EOBS-03 小样本试验独立正文断言提取，诊断漏提 claim；校准后才决定是否修改 Judge |
| 单指标失败独立记录，保留逐题诊断 | 已有 UNDETERMINED 和人工校准 | EOBS-01 明确 metric version、适用状态、分母与错误码；借鉴失败隔离，不采用 NaN/null 作为零分 |
| 评分方法与执行平台分离 | 现有 EvaluationRunner、ModelGateway、metrics/judge 已分层 | 首期同步已有评分；不迁移执行器，不让 Langfuse 再调用一次 Judge |

依据：[样本模型](https://docs.ragas.io/en/stable/concepts/components/eval_sample/)、[指标总览](https://docs.ragas.io/en/stable/concepts/metrics/overview/)、[Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)、[Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)、[评估失败配置](https://docs.ragas.io/en/stable/references/evaluate/)。

内部样本契约至少包含 sample/contract version、eval case/repeat、最终问题、真实历史引用、完整回答、实际上下文引用、expected/fact groups、claims、persona/snapshot/config、来源与内容完整状态。多轮评分继续 `FINAL_TURN_WITH_HISTORY`；TOOL 与 DOCUMENT 分开，mock 明示。缺实际上下文的旧结果标记不可计算新指标，不回填伪造快照。

指标描述至少包含 name/version、required inputs、scope、applicability、numerator/denominator、value/status/errorCode、方法与模型/提示词版本。状态区分 OK/NOT_APPLICABLE/UNDETERMINED/ERROR，聚合展示适用、已算、未知与失败计数；保留现有 API 和历史字段，不为本轮发明通用插件注册系统。

不照搬 embedding 反生成问题的切题分数，也不把“有来源支持”当“业务正确”。保留现有 Gold、拒答、冲突、claim 对齐和人工校准；后续新方法有预算、版本和独立证据后才讨论门禁。

### 4.8 失败、采样和渐进上线

telemetry 为 best-effort，队列有界，网络超时、重试及关闭等待有配置上限；不得在每次模型/工具结束后 flush，不得与业务重试共享循环。初始化失败、导出超时、队列满和服务停机均只影响观测；需要安全的可观测状态，不假装零丢失。

每个执行批次只做一次采样决定，所有子 span 沿用；恢复批次可重新采样但须标记。合成验收 sample_rate=1，在线采样率在部署配置指定。头部采样不保证捕获全部失败，不将采样 trace 统计作为全量错误率。禁止重复导出相同模型调用来补“失败可见性”。

顺序：关闭状态兼容 → 合成私有环境启用 → 故障/隐私验收 → 受控元数据灰度。关闭开关可恢复原路径；新 JSON 字段为可选，旧运行/旧报告仍可读。不删现有 OTLP、run_event、model_call、日志或评估实现；不做无关旧 RAG 分支清理。退出进程允许尚未导出的 span 丢失，状态必须说明。

## 5. Testing Decisions

实现验收编号如下；本次文档交付不将任何一项标为已通过。fake exporter 验证实际序列化 payload 与树结构；私有 Langfuse 使用合成数据做端到端收件检查；真实供应商调用与付费模型收益另列未实测。

| 编号 | Given / When / Then |
| --- | --- |
| LF-AC01 | 开关关闭或缺配置时，API/内嵌/独立 Worker 均可完成原用例；无 Langfuse 外发，无重复 provider/processor |
| LF-AC02 | 正常、恢复、HITL、取消、lease 丢失执行可定位相应批次；并发 run/tenant 不串关联；恢复不伪造同一长 span |
| LF-AC03 | 一次实际模型请求对应一个 generation；重试/fallback 各自 call_id 与别名可见；观测失败不增加模型调用或费用 |
| LF-AC04 | 植入正文、业务 ID、标题、路径、密钥、异常消息及第三方事件后，默认所有导出载荷均无这些值；清洗异常只丢观测 |
| LF-AC05 | 本地 `rag.agent` 父子 span 出现在目标实例；Dense/BM25 并行 span 正确关联，线程上下文不串 run |
| LF-AC06 | provider usage/估算/未知、缓存缺失/零值和结算未知被区分；MySQL 金额不受 SDK 估价影响 |
| LF-AC07 | 合成题中“召回有证据、重排丢证据”和“预算裁掉片段”保存不同阶段/实际区间；关闭诊断时检索与答案一致；截断不冒充完整 |
| LF-AC08 | 显式同步自动/人工评分可关联最终批次且重复运行不重复；null、未知、旧报告、纯检索、未采样及撤权均不伪造数值或上传 |
| LF-AC09 | exporter 停机、超时、队列满、flush 失败不改变业务状态/预算/lease；退出有界；关闭后可回到原观测路径 |
| LF-AC10 | 独立私有实例的版本、配置、维护人员访问与数据模式可核对；没有访问隔离或合成资料确认时不能启用内容观测 |
| EOBS-AC01 | 旧结果缺上下文、单轮/多轮、DOCUMENT/TOOL、未知/失败均按新契约表达；既有 API、报告及门禁不变 |
| EOBS-AC02 | 手算事实组/替代来源/重复片段/被裁区间样例匹配阶段指标；缺来源或上下文不完整时不出确定覆盖分 |
| EOBS-AC03 | 独立正文断言提取试验包含生成 claims 漏提、错误拒答和冲突题；人工校准、误判、未知与新增成本完整记录，未校准不升级门禁 |

验证必须交付代码提交、依赖/镜像版本、脱敏载荷 fixture、定向测试、合成端到端 trace 示例及故障结果。上线前比较观测开关前后的相同合成工作负载，报告样本数、并发、P50/P95 和额外资源；具体可接受开销由部署 SLO 决定，不在未测量时宣称零开销。

## 6. Out of Scope

Ragas 依赖、代码复制、题集/执行器迁移；Langfuse 托管 Judge/Prompt 管理/自动调参；默认正文上传；生产部署与公司数据导入；自动反馈转 Gold；新通用日志平台；完整分布式 API→Worker trace；持久 outbox 与保证送达；自动根因结论；本轮扩展质量门禁；真实业务工具接入。

本轮 Git 交付仅为技术规格、tickets 和阅读入口，不安装 SDK、不修改运行代码或 schema、不启动服务、不调用付费模型。

## 7. Notes

用户关于 Ragas 九个月未更新的观察不作为未经验证的停维护结论。本次浏览的[发布页](https://github.com/vibrantlabsai/ragas/releases)显示 v0.4.3 于 1 月 13 日发布；[提交历史](https://github.com/vibrantlabsai/ragas/commits/main/)可见 2026-01-19 提交，但浏览索引存在缓存，不能证明当前没有更新。选型依据是明确不增加该依赖及已有评估能力重合，后续维护状态变化不自动改变本项目决定。

SDK/server 补丁号、部署容量、保留期、采样率及观测开销在 LF-T01/LF-T06 的实际环境锁定；这些不会阻碍现在编写文档。上线结论仍需 LF-AC01～10 的证据，评估设计增强需分别满足 EOBS-AC01～03。历史消融报告及 fake model 成绩不能代替当前版本真实模型质量验收。

官方资料核对日期：2026-10-12；接口与许可/部署能力需在实施时复核。[LangChain/LangGraph 集成](https://langfuse.com/integrations/frameworks/langchain)是备选实现参考，首期采用手动观测；[私有部署说明](https://langfuse.com/self-hosting/deployment/docker-compose)用于实施环境准备。
