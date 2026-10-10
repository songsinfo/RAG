# 统一业务 Agent 实施状态

更新时间：2026-10-10。PR #14 **草稿，禁止未经验证直接合并**。

## 已编码（并非验收通过）

- T01：四种固定的本地 fixture、参数 schema、校验、金额分单位与证据文本；合成测试。
- T02：run_source 新增 source_type / tool_name / source_snapshot；DOCUMENT 兼容，TOOL 不使用假 document/build。迁移脚本 20261010_add_tool_run_sources.sql。
- T03：现有 LangGraph Agent 注册业务工具；search_knowledge 改用 RagRetrievalGraph（无内层 checkpoint）；证据与 mock 标识进入结果；按来源类型做外发校验。
- T04/T05：新建问答无论旧 RAG/AGENT 还是无 mode，统一保存为 AGENT 并创建 durable job；聊天页面移除模式选择并强制显示模拟结果。
- T06：新评测默认 Agent；旧 manifest 保持原含义；纯检索评测路径保留。
- T08 部分：高低水位完整轮次裁剪；默认模型窗口 8192，扣除工具 schema、回答和安全余量，超出时失败而不是扔掉当前问题。完整长期快照续接未完成。
- T09 部分：持久化供应商缓存读取 Token（缺失为 null）、模型/耗时信息。**供应商真实 KV cache 命中未实测。**

## 未完成的硬性验收

- UA-04：真实模型自动选工具及多源回归尚未实测；无 Java 服务接入是有意边界。
- UA-05/11：跨请求持久化稳定前缀、结构化对象来源、澄清补参、复用旧证据的权限边界仍需更完整的测试与实现。
- UA-08/09：上线前必须查清旧 REALTIME RAG 排队任务，排空/失败处理后才能删兼容分支。旧执行代码目前暂保留，防止静默更改历史运行语义。
- UA-10：低水位压缩仅是当前 run 内策略，未实现压缩快照版本和跨 run checkpoint 续接。
- UA-12：真实缓存/延迟/费用对比尚无外部模型环境数据。不能宣传缓存收益。
- CI、MySQL 8.4 schema、前端构建及服务端完整回归以 GitHub Actions 实际结果为准，不把静态提交视为测试通过。

## 迁移和回滚

1. 备份 MySQL 与运行队列；确认新代码部署前执行迁移 SQL，验证 run_source 允许 TOOL 空 document_id/build_id，且旧 DOCUMENT 保持原值。
2. 查 `qa_run.mode='RAG'` 且 `status IN ('QUEUED','RUNNING','WAITING_APPROVAL')` 的旧任务。未排空时不得移除旧执行分支。
3. 回滚应用代码时必须继续保留新 run_source 列、TOOL 快照和可空文档列；不能直接反向 ALTER 丢失数据。已用统一 Agent 建立的记录需要按权限安全处理。
4. 评测人工核验须分别记录真实模型标识、工具调用轨迹、提示版本、缓存 Token、耗时、费用和严格 Judge 结果。fake model 通过只证明代码调度，不证明真实工具选择准确率。

## 2026-10-10 PR 审查修复（待最新 CI 校验）

- 复核 P1：集中压缩前从显式用户参数及已执行的模拟工具中抽取有来源的对象状态；压缩保留当前完整 tool call 组，模型循环与工具节点通过 `AgentState.business_state` 传递状态，使用现有 LangGraph checkpoint 持久化。跨 run 历史初始化暂仍受 `_rag_history` 最近四轮/字符预算限制，**不宣称 T08 全量完成**。
- 复核 P2：`normalize_provider_usage()` 在共用记账处识别 `input_token_details.cache_read`、顶层缓存字段及 `prompt_tokens_details.cached_tokens`；保留真实 0，缺失记 null；主模型与 fallback 共用。
- 修复旧 `RETRIEVING` 阶段断言，验证 `AGENT_RUNTIME`、`AGENT_MODEL`、`AGENT_TOOL`、`RAG_SCOPE` 和 `RAG_FINAL_HITS` 及 Durable Job 状态；仍需以最新 CI 为准。

## 2026-10-10 第二轮复查：历史证据幂等和审计测试

- `business_context.py` 按原始 `sourceRunId/evidenceId` 去重工具证据引用。重复扫描 LangGraph 工具消息不会重复写入同一来源或更新最初查询时间；同一 ID 与不同工具/对象冲突时按 `SOURCE_CHANGED` 拒绝。
- 回归测试覆盖连续 `state_from_messages` / `compact_messages` 重放、跨 run 同名 E1、旧 checkpoint 重复引用及冲突来源。
- 旧审计测试已迁移至统一 Agent 的 `ModelGateway.chat_tools()`，同时验证历史问题、当前问题均在模型消息内，以及模拟工具选择最新问题作为检索词。
- 仍有待验收的跨 run 持久化上下文版本与旧路径排空；最新 Actions 是否全绿以 PR Checks 为准，不能用代码提交代替 CI 通过。

## 2026-10-10 跨 run 持久化改造

新增 `context_segment.context_payload/context_version/context_through_turn/context_boundary_turn/context_last_run_id` 及增量迁移文件 `20261010_segment_context_snapshot.sql`。新 Agent run 在初始化时优先读取经过租户、分段、用户归属与文档 ACL/build 复核的快照；成功终态与快照更新在同一数据库事务内提交，重复运行按 run_id 幂等。未压缩只追加消息、保留版本，压缩时增加版本、记录边界；旧 segment 首次回退到原授权历史。

**上线限制**：快照仍只保存 user/assistant 的受限历史文本与显式业务对象/来源引用，不是共享的完整 LangGraph checkpoint；原 run 的 tool/evidenceId 不作为当前证据。旧 RAG 运行分支需先在目标环境完成队列清点，并保留对旧任务的可控兼容处理；未清点时不安全删除。此阶段仍为 Draft，必须等 CI 和专项验收结果。
