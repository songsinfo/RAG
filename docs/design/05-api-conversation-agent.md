# API、会话、页面与 Agent

> **历史设计说明（2026-09-22）**：本文保留早期技术设计基线，其中可能包含 Java、Spring、Elasticsearch、旧 Docling API/切块方案或尚未落地的设计，不代表 `main` 当前实现。当前系统行为请以仓库根目录 `README.md`、`docs/source-walkthrough.md`、`docs/rag-evaluation-guide.md` 与 `backend/app` 源码为准。


对应 PRD-01/03/04/07，评估与运维接口同时覆盖 PRD-05/06。API 前缀 /api/v1；JSON 使用 camelCase，ID 为 UUID，日期使用 UTC ISO-8601。以下是实现契约，尚未有运行中的接口。

## 1. 登录与统一协议

本地账号使用 BCrypt hash；默认示例密码仅用于隔离 demo seed，正式运行初始化时设置。登录后发 256 bit 随机 opaque token，数据库只保存 SHA-256 hash。Cookie 使用 HttpOnly、SameSite=Lax、非本地 HTTPS Secure；服务端会话默认 8 小时绝对过期，注销/停用即时复核。基于 Cookie 的写请求必须通过 CSRF token 与同源校验；不把 userId/tenantId 放进可自行改写的身份参数。

GET /auth/csrf 获取签名双提交 CSRF token：登录前绑定短期 nonce Cookie，登录后绑定当前会话，写请求同时校验 Cookie、请求头和服务端 HMAC。POST /auth/login 验证 loginName/password/tenantCode，统一错误不区分用户是否存在，限流起点账号+来源每分钟 5 次。tenantCode 只用于选择登录域，成功后身份以会话为准，不沿用后续客户端声明。

错误体：{requestId, code, message, retryable, details}。普通详情不含堆栈、SQL、受限标题和供应商响应正文。分页 limit 默认 20、最大 100，opaque cursor 绑定用户/查询条件；列表总数只计算可读条目。

写接口的 Idempotency-Key 默认保留 24 小时：绑定租户+用户+动作，规范请求 hash 相同返回原资源，不同返回 409 IDEMPOTENCY_CONFLICT。客户端不能通过同一 key 改变 scope、问题或上传内容。语料构建还使用版本/hash 的持久幂等约束，不因 24 小时到期重复发布。

## 2. 接口目录

| 方法与路径 | 输入/结果 | 权限及重要失败 |
|---|---|---|
| POST /auth/login、POST /auth/logout、GET /me | 本地登录/注销/本人组和维护资格 | 登录需 CSRF；登录态不可由请求正文覆盖 |
| GET /knowledge-bases | 可读 KB 列表 | 只返回当前可读 |
| POST /admin/knowledge-bases | name/readers → KB | 管理员；创建全员授权必须显式选择 |
| GET/PUT /admin/knowledge-bases/{id}/acl | readers/expectedVersion | 管理员、本租户；409 乐观锁冲突 |
| GET /admin/users、GET /admin/groups | 本租户维护列表 | 管理员，最小字段 |
| PUT /admin/groups/{id}/members | userIds/expectedSecurityEpoch | 管理员、全部用户同租户；事务变更并审计 |
| GET /documents?kbId=... | 可读 LIVE 文档及 activeVersion | 普通读列表，不泄露受限总量 |
| GET /admin/documents?status=TRASHED | 维护/回收站列表 | 管理员，本租户；无读权仅返回维护 ID/状态 |
| POST /admin/documents | multipart file/kbId → documentId/versionId/jobId | 管理员且可读 KB，新建默认 RESTRICT |
| POST /admin/documents/{id}/versions | multipart file → 新版本任务 | 管理员+文档读权限，不直接改 active |
| GET /documents/{id} | 可读基本信息/当前版本 | DocRead；不存在/不可读统一 404 |
| GET /documents/{id}/versions/{versionId}/preview | 来源单元/告警/分页 | DocRead；历史固定版本；原件经受控下载 |
| GET /documents/{id}/artifacts/{artifactId} | 下载/安全预览 | 同文档归属校验；无静态路径直出 |
| GET/PUT /admin/documents/{id}/acl | mode/readers/expectedVersion | 管理员，本租户；阅读正文另查权限 |
| POST /admin/import-previews、POST /admin/imports | rootAlias/relativePath/选中文件/estimateId | 预览清单再启动，仅允许配置根；预算变化使估算失效 |
| POST /admin/builds/{id}/accept-warnings | warningIds/reason/expectedVersion | 管理员+可读；仅可接受告警，禁止绕过硬失败 |
| POST /admin/documents/{id}/trash、/restore、/purge | expectedVersion → 状态或 jobId | 管理员；purge 不可逆，UI 明确动作 |
| GET /admin/jobs、GET /admin/jobs/{id} | 任务阶段/计数/错误分类 | 管理员；涉及正文另鉴权 |
| POST /admin/jobs/{id}/retry、/cancel | 幂等控制 | 管理员；重试不绕过租约/生命周期 fence |
| POST /conversations、GET /conversations | 创建/本人列表 | owner 来自登录态 |
| POST /conversations/{id}/segments | scope={mode,kbIds} → 新上下文段 | 本人会话；有活动 run 时 409 |
| GET /conversations/{id}、DELETE /conversations/{id} | 合法历史/清除 | 本人；清除先取消活动任务，正文清理持久执行 |
| POST /conversations/{id}/runs | question/segmentId/mode/strict → 202 run | 本人、幂等、同会话一个活动请求 |
| GET /runs/{id}、POST /runs/{id}/cancel | 状态/最终结果；取消 | 本人；重复取消稳定 |
| GET /runs/{id}/evidence/{evidenceId} | 固定版本来源位置与安全预览链接 | 本人+全部来源当前可读，不接收任意路径 |
| PUT /runs/{id}/feedback | rating/comment/issueType | 本人且答案仍可见 |
| GET/PATCH /admin/feedback/{id} | 状态/resolution | 管理员，正文和关联 run 仍授权 |
| GET /admin/feedback、GET /admin/configs | 反馈列表/配置版本列表 | 管理员；反馈正文仍做来源守卫 |
| POST /admin/configs、POST /admin/configs/{id}/publish | 不可变配置版本/设默认 | schema/能力/预算校验；发布审计 |
| POST /admin/eval/datasets、POST /admin/eval/datasets/{id}/versions | 创建/复制版本 | 管理员，仅评测隔离环境 |
| GET /admin/eval/datasets、GET /admin/eval/dataset-versions/{id}/cases | 题集版本与题目列表 | 管理员，题目来源仍授权 |
| POST /admin/eval/dataset-versions/{id}/cases | 题集 JSONL 导入或单题增改 | 仅 DRAFT，标注经过权限检查 |
| PATCH/DELETE /admin/eval/cases/{id} | 修改/删除草稿题 | 仅 DRAFT，固定 ID 归属检查；冻结题改动须新版本 |
| POST /admin/eval/dataset-versions/{id}/freeze | 校验后冻结 | 必须完整标注/哈希/划分，不能覆写 |
| POST /admin/eval/snapshots、GET /admin/eval/snapshots | 生成/列出已批准模拟或脱敏语料的冻结快照 | 仅隔离环境；不接受任意生产库连接或公司目录 |
| POST /admin/eval/estimates、POST /admin/eval/runs | dataset/snapshot/config/repeats → 估算/202 | 策略能力、隔离边界、运行 persona 和查看身份分别校验 |
| GET /admin/eval/runs/{id}、GET /admin/eval/runs/{id}/results | 汇总和逐题结果 | 来源当前授权；失败题也返回 |
| POST /admin/eval/runs/{id}/cancel | 停止发起新题/调用 | 已完成保留，报告 INCOMPLETE |
| POST /admin/eval/results/{id}/reviews | 人工标签/理由 | 保留自动判定，不直接修改冻结题 |
| GET /admin/eval/comparisons?runIds=... | 逐题配对对照 | 检查可比 manifest；不可比明确说明 |
| POST /admin/eval/runs/{id}/exports | mode=REDACTED/FULL → export job | 默认脱敏，FULL 明确选择且源权限允许，下载再鉴权 |
| GET /admin/runs/{id}/trace、GET /admin/budget | 受控诊断/预算 | 管理员不能绕过证据阅读 |
| POST /internal/budget/reservations、POST /internal/budget/reservations/{id}/settle | callerEnvironment/callId/modelConfig/最大 token；usage 与结算状态 | 仅配置的内部服务凭据，固定映射预算租户；不接受正文或浏览器调用 |
| GET /health/live、GET /health/ready | 最小健康信息 | 外部不返回密钥、依赖内网地址 |

单例读取中路径所属对象必须逐层匹配，不能只查末尾 UUID。管理员批量维护接口返回每个对象的状态，不因部分失败假装整批成功。

## 3. 问答请求与返回

提交前执行高置信度输入安全检查：拦截私钥/凭据、手机号/身份证号和以命令形式出现的系统提示词绕过请求，且在写入 `qa_run` 前结束。规则只覆盖高置信度模式，不能替代公司 DLP。问题路由采用保守规则：完整问候进入 `SMALL_TALK`，明确的通用助手请求进入 `UNSUPPORTED`，其余全部进入 `KNOWLEDGE_QA`。FAQ 仍作为知识内容参与检索；路由器不能授予权限或判断某篇受限文档是否存在。

生成后检查最终展示文本中的高置信度凭据和个人敏感信息；命中时不提交答案正文，运行以 `SENSITIVE_OUTPUT_BLOCKED` 失败。检索证据仍由 ACL 决定，安全检查不能代替文档授权。

提交示例（结构契约，ID 使用占位符）：

```json
{
  "segmentId": "<current-segment-uuid>",
  "question": "订单补偿任务一直重试，应该检查什么？",
  "mode": "RAG",
  "strict": false
}
```

**当前 Python 实现（PR #11，2026-10-09）覆盖本段旧方案：**在线普通 RAG 只写 `qa_run`，返回 HTTP 202（含 `runId`、`status=QUEUED`、`executionMode=REALTIME`），再由 FastAPI BackgroundTasks 立即执行，无 `QA_RUN` Job。Agent 和内部评估 RAG 同事务创建 `qa_run` 与 `QA_RUN` Job（`executionMode=DURABLE`），由 Worker 执行。前端通过 GET `/runs/{id}/events` 订阅 SSE 阶段事件，结束时 GET `/runs/{id}` 获取最终结果，断线时退回轮询。模型仍输出完整结构化 JSON，不是逐 Token 生成；未经验证的正文不展示。

完成结果字段：runId/status/terminalReason/answer/claims/evidence/warnings/verification/usageSummary。answer 由已校验 claims 渲染。evidence 每项含 evidenceId/documentId/versionId/sourceType/location/machineGenerated/isCurrent，但只对仍具备阅读权限的调用者输出。内容撤权后的历史读返回 visibility=HIDDEN 和固定原因提示，不输出旧 answer 或来源标题。

运行状态统一：
- 活动态 QUEUED、RUNNING，stage 可为 REWRITING/RETRIEVING/RERANKING/GENERATING/VERIFYING。
- 终态 COMPLETED、PARTIAL、NO_EVIDENCE、CONFLICT、NEED_CLARIFICATION、FAILED、CANCELLED、BUDGET_EXCEEDED、REPEATED_CALLS、VERIFICATION_FAILED、VERIFICATION_INCOMPLETE。
- SOURCE_CHANGED、AUTHORITY_UNAVAILABLE、MODEL_TIMEOUT 是 terminalReason/errorCode；不再增加互相矛盾的第二状态字段。

GET 任务资源 HTTP 200 只表示成功读取状态，不能据此把 status=FAILED 统计成成功问答。

## 4. 错误与重试

| HTTP | code 示例 | 客户端行为 |
|---|---|---|
| 400 | INVALID_INPUT、INVALID_CONFIG、PROMPT_INJECTION_BLOCKED、SECRET_INPUT_BLOCKED、PII_INPUT_BLOCKED | 修正字段，不自动重试 |
| 401 / 403 | AUTH_REQUIRED / OPERATION_FORBIDDEN | 重新登录/显示无资格 |
| 404 | RESOURCE_NOT_FOUND | 不区分受限与不存在 |
| 409 | ACTIVE_RUN_EXISTS、VERSION_CONFLICT、IDEMPOTENCY_CONFLICT、INDEX_CAPABILITY_MISSING | 查看原任务或调整版本/配置 |
| 413 / 422 | INPUT_TOO_LARGE / PARSE_UNSUPPORTED、QUALITY_REVIEW_REQUIRED、SENSITIVE_OUTPUT_BLOCKED | 拆分或人工处理；敏感输出不展示正文 |
| 429 | RATE_LIMITED、BUDGET_RESERVATION_DENIED | 显示限额及可安全公开的重试时间 |
| 503 | AUTHORITY_UNAVAILABLE、DEPENDENCY_UNAVAILABLE | 不自动放宽权限；按有限策略重试 |

在线普通 RAG 的执行错误记录在 `qa_run.terminal_reason`，不存在对应 `QA_RUN` Job；Agent 和评估 RAG 还会更新持久化 Job 的错误状态。已返回 HTTP 202 后，业务失败不能改写原 HTTP 状态码。请求超时不等于任务取消；用户可凭幂等 key 找回运行再取消。

## 5. 会话、终态及清除

创建段时保存 scope=ALL 或 SELECTED 的明确 KB 集合。ALL 每次重新求当前可读范围；SELECTED 保存用户选择但每次求交集。切换范围不迁移旧段模型上下文。

POST run 在会话行锁内检查所有者、当前段、幂等键和活动唯一约束，分配单调 turn_no。历史从 qa_run 构造，首版一行就是一个问答轮次，不另建重复 messages 表。

完成与取消均锁 qa_run：
- 若已经终态，保留原终态。完成先提交则取消返回 alreadyTerminal。
- 若取消先被接受，立即设 CANCELLED，写 cancel 时间并释放会话活动唯一键；迟到 worker 的 CAS 必须失败。
- 取消后的在途模型占用的并发与预留不能立即全部释放，直到调用结束/明确未发送/进入未知结算；新请求仍受全局上限。
- 进程重启时未终结在线 run 设 FAILED/PROCESS_RESTARTED，释放会话锁；不自动重放付费问答。持久入库/评估任务按租约恢复。

清除会话先隐藏并阻止新读，取消活动 run，再清理问题/答案/上下文事件/反馈正文，保留无内容的最小运行和用量审计。用户自己输入的敏感内容也在清理范围内。

## 6. Agent 工具与循环

search_knowledge 参数：{query:string(max 2000),topK:integer(1..10)}。
read_section 参数：{documentId:uuid,buildId:uuid,sectionId:uuid}。
additionalProperties=false；tenant/user/权限/tokenBudget/URL/任意文件路径不在 schema 内。sectionId 必须属于本次范围、已接受当前 build 的 parent 或 source 单元；若是搜索未出现的合法 ID，也须重新走同一范围和版本核验，不因“模型知道 ID”授予读取。

工具结果仅包含结构化状态、实际 evidence ID、原文片段、必要定位和可公开错误。服务端限制每次返回的总 token，长章节按既定边界裁剪并标缺失，不能整库倒入。

循环：
1. 建立一次全局 RunBudget 和 EvidenceRegistry。
2. 通过 ModelGateway 调用 Spring AI `OpenAiChatModel`，将两个只读工具的 JSON Schema 转成 `ToolCallback`，保存模型返回的可观察工具请求；不保存/展示模型隐藏思考链。
3. 验证工具名/参数，按 tool_call_id 回传结果。一个响应多个工具按顺序执行，逐个检查取消/权限/预算。
4. 下一轮把 assistant tool_calls 和对应 tool 结果转换为 Spring AI `AssistantMessage`/`ToolResponseMessage`，按 tool_call_id 保持调用关联。
5. 最终结构化 claims 进入普通问答相同的验证与提交路径。
6. 达到终止条件不再调用模型“做个总结”；只有已有并通过校验的内容可作为明确标记的部分结果。

初始主模型最多 6 轮、工具 8 次、全局模型调用最多 12 次、60 秒 deadline；token 上限由配置统一计算。相同规范化工具名+参数在同 run 第三次提出时终止 REPEATED_CALLS，前两次允许重试/确认。工具内 Rewrite/HyDE/Embedding/Rerank 计入同一父预算，不可 reset。

模型请求未知工具不执行并返回参数错误；权限失败不能让模型修改身份恢复。版本变化终止 SOURCE_CHANGED，重新发起新问答，不在一个答案里混合不同版本。

这里有意使用低层 `ChatModel.call(Prompt)`，不让 `ChatClient` 自动执行工具。Spring AI 负责 OpenAI-compatible 消息、Media、工具定义和响应的序列化/解析；`QaService` 负责工具白名单、参数校验、执行顺序、终止条件及证据链，因此模型不能绕过业务闸门直接运行工具。Spring AI 的自动与用户控制两种执行方式见[官方 Tool Calling 文档](https://docs.spring.io/spring-ai/reference/api/tools.html)。

## 7. 最小页面

登录页；资料页（列表、上传、版本/解析预览、ACL、回收站）；问答页（范围、普通/深入、严格开关、阶段、取消、校验后答案、引用、反馈）；实验页（题集、标注、冻结、估算、运行、配对详情、人工复核）；运行页（任务、降级、预算）。

页面只做这些业务表单与列表。无权限操作可隐藏，但所有 API 仍校验；测试入口依赖隔离环境标识，不提供能把生产模型响应切成 Mock 的用户按钮。
