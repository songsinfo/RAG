# 实施任务图与协作契约

状态：开发中。实现 skill 使用独立 worktree 并行实施，feature/enterprise-kb 为集成分支。无远端，提交用 Codex <codex@localhost> 标记代理创建，不改全局 Git 身份。

任务：F0 编译/运行基础 → F1 身份/权限/入库生命周期，F2 检索/模型/问答/Agent，F3 功能前端；F1/F2 → F4 评估/数据集/预算集成 → F5 系统测试与文档/恢复。独立算法测试和前端可提前进行。

代码基础：Java 21，Spring Boot 4.0.3，包 dev.rag。SQL schema 以 docs/design/schema.sql 为基准，迁移 V1 已复制。Db 为 NamedParameterJdbcTemplate 包装，数据库行字段使用 snake_case；REST 输出应使用 camelCase。Json 用显式 Jackson 2 辅助处理 JSON 存储，MVC 由 Boot 默认序列化。

## 跨模块固定约定

- common.Actor(tenantId,userId,admin)、ApiException、Db、Json 已存在。
- identity.AccessPolicy 由 F1 实现：Actor actor(HttpServletRequest)，void requireAdmin(Actor)，boolean canReadDocument(Actor,String)，void requireDocument(Actor,String)，List<String> readableBuilds(Actor,List<String> kbIds)，<T> T guard(Actor,Collection<String> docIds,Supplier<T>)。guard 检查文档当前 LIVE 与权限；历史版本允许，当前 build 由调用方额外核验。
- F1 负责 knowledge 包全部入库/索引写、Docling、版本/删除/恢复；F2 负责 qa/search/model 包查询/模型/预算/会话/Agent，避免写同文件。
- ES 名称 indexPrefix-original / indexPrefix-vector，字段严格按设计映射；原文 _id=canonical chunk ID，向量 _id=representation ID。表示类型值 ORIGINAL/SUMMARY/QUESTION。
- F2 提供 model.ModelGateway：embed(Actor actor,String ownerKind,String ownerId,List<String> texts) -> List<List<Double>>；chat(Actor actor,String ownerKind,String ownerId,String purpose,List<Map<String,Object>> messages,int maxOutputTokens) -> Map<String,Object>（供应商 assistant message）。ownerKind 为 RUN/JOB；全部调用预算统一。F1 调 embed 与 chat 做入库增强和图片描述，避免旁路外发。
- F2 提供 qa.QaService：Map<String,Object> createConversation(Actor,Map<String,Object>)；Map<String,Object> submit(Actor,String conversationId,Map<String,Object> body,String idempotencyKey)；Map<String,Object> get(Actor,String runId)；Map<String,Object> cancel(Actor,String runId)。REST 与评估共用这些入口，submit 异步返回 runId。
- F4 主代理负责 evaluation 包、部署脚本/容器与完整集成测试；不得与 F1/F2 的 Java 包同时编辑。
- 所有 Java 新接口请通过上述契约，变更先发短消息并更新本文。无凭据时 MODEL_NOT_CONFIGURED，不返回伪造成功；测试替身仅在测试中。
- 主代理维护 pom/application.yml；需要依赖发消息。测试用 JUnit 5。Docker 尚在启动检查，勿把跳过的集成测试当 PASS。

