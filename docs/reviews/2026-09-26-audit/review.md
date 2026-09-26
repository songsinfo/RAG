# RAG 功能完成度与代码质量审查

日期：2026-09-26。基线：`main` / `cfc30f9`；开始审查时工作区干净。

本文保留修复前的审查证据；修复范围、合并前验证和部署前提见 [fixes.md](fixes.md)。

## 结论

**此前约定的功能没有全部实现，已实现功能也存在验收级缺陷。** 当前项目具备普通 RAG、Agent、权限、入库、会话、评测的主要骨架，有一定工程基础，但不能称为全部完成或已完成企业环境验收。

代码的模块方向合理，局部有批处理、连接复用、任务租约等有效设计；整体实现还不能评价为足够稳健、简洁。首要问题是业务契约不一致和正确性缺陷，随后才是性能调优与代码整理。

本次检查 PRD/验收追踪、实际调用链、已有测试、前端构建，并新增离线复现。没有修改业务代码，没有连接真实服务进行压测，没有调用付费模型。未读取或上传公司原始资料。新增测试全部使用合成文本和 SQLite/服务替身。

## 本次验证

| 检查 | 结果 | 能证明什么 |
|---|---|---|
| `backend/.venv/Scripts/python.exe -m pytest -q --disable-warnings --tb=short` | 80 passed，29.90s | 现有离线测试基线通过；不是 MySQL/Milvus/Docling/真实模型集成验收 |
| CI 中额外指定的历史审查测试 | 10 passed，3.64s | 已有租约、撤权、清理等专项回归通过 |
| `npm run check` | 通过 | TypeScript/Vue 类型检查 |
| `npm test` | 6 passed | API 辅助函数和静态契约；不是浏览器交互 E2E |
| `npm run build` | 通过，Vite 构建 8.47s | 可构建；主 JS 1,034.79 kB，gzip 338.05 kB，存在大包警告 |
| 本目录 `test_audit.py` | **8 failed，4.45s** | 8 个“预期正确行为”断言失败，复现下列缺陷；失败是审查证据，不是已修复 |

前端首次测试受沙箱 `spawn EPERM` 限制；获准在沙箱外重跑后通过。没有把环境错误计为产品缺陷。

`backend/tests/conftest.py` 使用内存 SQLite、替身解析和测试检索；SQLite 连接还被测试锁串行化，因此通过这些测试不能推导 MySQL 并发发布安全或真实检索性能。

## 按需求检查完成度

“有实现”表示发现真实调用入口并有相应代码/测试，不表示所有相关 AC 均已运行验收。

| 能力 | 当前判断 | 依据/缺口 |
|---|---|---|
| 本地登录、租户、KB/文档/组 ACL | 有实现和测试 | `identity/deps.py` 统一权限条件；检索预过滤与外发前复核存在 |
| Dense / BM25 / Hybrid / RRF | 有实现和测试 | `MilvusHybridService.search` 三模式接入 |
| Reranker、Parent-Child、Parent Expansion | 有实现，但配置契约有缺陷 | Pipeline 已接入；前端旧 `post` 配置不生效，见 F8 |
| Rewrite、HyDE、Multi-Query | **未接入** | `RetrievalConfig` 无相应执行字段；Pipeline 直接用原 query；出现于 UI/无模型模式检查不能算实现；AC-03-02/10 未完成 |
| MMR | **未接入** | 排序实际为 RRF、可选 rerank、父扩展，没有 MMR 执行；AC-03-04 未完成 |
| Original / Summary / Hypothetical Question 多向量 | **仅 Original** | 入库固定 `representation_type="ORIGINAL"`，检索也固定过滤 ORIGINAL；AC-02-07/09、AC-03-03 未完成 |
| 多格式解析、OCR、表格、来源定位 | 部分实现 | 全格式调用 Docling HybridChunker，并传 OCR/表结构选项；有 page/doc_items 映射；本次没有真实样本验收 |
| 图片描述、解析质量审核和待处理发布 | 未见完整链路 | 未见图片描述调用；解析输出只取 chunks；SourceUnit 固定 TEXT、machine_generated=False；预览 warnings 固定空；有 chunks 即进入自动发布。不能用 OCR 参数代替 AC-02-03/05 |
| 持久任务、租约、重试、恢复 | 有实现和专项测试，仍有缺陷 | Worker lease/fence 存在；首次 Milvus 连接失败无法自动恢复，见 F7；评估更新也未使用同样的 lease fencing |
| 文档版本和原子发布 | 有实现但验收失败 | 旧任务可覆盖已发布新版，见 F1，对应 AC-02-08 |
| 回收站/恢复/彻底删除 | 有实现和回归 | 未因此推导备份恢复正确；外部服务不可用时的彻底删除仍需真实演练 |
| 结构化断言、引用、无依据/冲突状态 | 有实现 | `claim_validator.py`；但越界 span 会被改成全文，而非拒绝，精确引用保证有限 |
| 严格语义校验 | **有绕过** | 普通 RAG 空裁判结果通过；Agent 直接标 SUPPORTED，见 F2/F3 |
| 会话、阶段、取消、评分/反馈 | 主要入口已实现 | 当前测试覆盖部分运行及取消流程 |
| 普通 RAG 追问 | **未形成上下文闭环** | 原问题直接检索，模型仅收到当前 question/evidence，见 F5 |
| 切换范围后的上下文隔离 | **验收失败** | Agent memory 只筛 conversation，未筛 segment，见 F4，对应 AC-04-03 |
| Agent 两个只读工具、轨迹、轮次/工具上限 | 有实现和测试 | LangGraph、checkpointer、工具约束、重复调用限制存在 |
| 全局预算、真实计费 | 部分/未完成 | Agent 使用已记账 token/call 检查；Embedding 不记入 model_call；重试多次仅记一条、attempt_no 固定 1；金额固定 0；未见统一预留与供应商结算闭环 |
| 题集版本/冻结、指标、人工复核、导出/比较 | 有主要实现 | catalog/metrics/judge/runner 已存在；不能等同于评测有效性完整 |
| 多轮评测、persona、快照约束 | **存在静态可确认缺口** | 只执行 turns[0]；纯检索用启动者而非题目 persona；完整 QA 新会话未限制 snapshot 文档，见 F9 |
| 全策略消融、当前版本端到端质量 | 未完成当前基线验收 | 历史报告只覆盖 B0-B3，且生成失败明显；不能用旧 Java/ES 的实验结果证明当前 Python/Milvus 全策略或回答质量 |
| 备份恢复、吞吐、干净环境完整演示 | 本次未确认完成 | 没有找到当前版本完整演练和负载报告；设计追踪 81 项不是 81 项运行验收 |

## 优先修复问题

### F1 · P1 · 旧入库任务会把当前版本倒退

位置：`backend/app/knowledge/service.py:945-965`。

发布检查只有当前 Job 的 lease，没有与文档当前版本/发布顺序比较。两个不同版本各持有合法租约，后提交的新版本先完成、旧版本后完成时，旧任务仍无条件写 `active_build_id`。

复现：`test_old_upload_cannot_replace_newer_published_version`。先发布新任务，再完成旧任务，active 最终等于旧 build。应在同一发布事务中校验文档版本序号及生命周期，再做条件更新；Job lease 与文档版本顺序解决的是不同问题。

### F2 · P1 · 普通 RAG 严格模式接受空裁判结果

位置：`backend/app/qa/service.py:919-922`。

`all(...)` 对空列表为真，因此 `{}` 或 `{"verdicts":[]}` 都可能被标为 SUPPORTED。当前也没有校验 verdict 与 claimId 的一一对应，漏判、错 ID、重复 ID 均缺少完整性约束。

复现：`test_strict_rag_rejects_empty_judge_verdicts`。应先检查每个 claim 恰有一个合法判定，再要求全部支持；可复用项目评估裁判已有的完整性检查思想。

### F3 · P1 · Agent 严格模式未调用语义裁判

位置：`backend/app/qa/service.py:723-740`。

Agent 返回 claims 后，strict 分支直接赋值 `verification="SUPPORTED"`。`agent.py` 本身只做结构化断言校验，既未接收 strict 参数，也未调用 JUDGE。

复现：`test_strict_agent_actually_calls_judge`。替身 Agent 返回 claims，捕获模型网关调用，JUDGE 调用数为零而结果标为支持。建议普通 RAG 与 Agent 在结构校验之后复用同一套严格校验出口。

### F4 · P1 · Agent 跨上下文段携带旧回答

位置：`backend/app/qa/agent.py:784-833`。

`_conversation_memory` 按 conversation_id/owner/status 查历史，没有 segment_id 条件。新段虽切换检索范围，旧段回答仍进入 Agent prompt，直接违反 AC-04-03。这里确认的是范围隔离错误；未把它扩大表述为已复现跨租户泄露。

复现：`test_agent_does_not_reuse_previous_segment`。新段取 memory 仍得到旧问题及旧答案。应限定当前 segment，并沿实际上下文依赖检查来源可读性。

### F5 · P2 · 普通 RAG 没有真正支持追问

位置：`backend/app/qa/service.py:769-777,869-872`。

检索 query 是当前 question，生成 messages 也只有系统指令和当前 question/evidence，没有历史、指代消解或独立问题改写。因此“它有哪些限制？”这样的追问无法可靠关联上一轮对象。

复现：`test_regular_rag_followup_receives_history`。捕获第二轮实际模型 messages，不含第一轮问题。应按 PRD 使用当前段中经授权的有限历史，并明确用于检索的问题构造方式。

### F6 · P2 · 生产入库无条件保留一份内存测试索引

位置：`backend/app/retrieval/milvus_service.py:134`。

即使真实 Milvus upsert 成功，代码仍写 `_mock_store`，内容含正文及 1024 维 Python float 向量。内存随该进程入库的 chunk 数量增长，无容量/TTL 上限。100,000 个向量仅按 float64 数值载荷计算就约 781 MiB；Python list/float 对象开销会更高。此数字为算术估算，不是实测 RSS。

复现：`test_production_upsert_does_not_fill_mock_store`。模拟非测试环境及正常 Milvus 客户端，仍填充测试缓存。最小修复是仅测试环境维护该缓存。

### F7 · P1 · Milvus 首次连接失败后不会重连

位置：`backend/app/retrieval/milvus_service.py:27-30,78-84`。

连接前便设置 `_client_checked=True`；失败后 `_client=None` 但标志不复位。后续请求/任务重试始终取得 None，外部服务恢复也需重启后端才能重新建客户端。

复现：`test_milvus_reconnects_after_initial_failure`。连接替身第一次失败、第二次可成功，但第二次根本未调用。应只缓存成功的客户端，并为失败重试使用有界退避及初始化同步。

### F8 · P2 · 前端配置与后端解析不一致，开关静默失效

位置：`frontend/src/Operations.vue:14`、`backend/app/retrieval/config.py:51-55`。

前端创建模板用 `post.rerank/post.parent/post.mmr`、`fusion.rrfK` 等旧字段；后端读根级 `rerank`、`parentExpansion` 和另一套 RRF 字段。开关可保存在配置中，但不会对应实际执行。Query 增强和 MMR 也缺少实现/不支持时的拒绝。

复现：`test_ui_config_post_options_are_not_silently_ignored`。传入 rerank=true、parent=false，解析结果却为 false、true。应统一一个配置契约，明确拒绝未支持策略，避免“保存成功”等同于“策略启用”。

### F9 · P1 · 评测执行条件与声明不一致（静态确认，未新增动态复现）

位置：`backend/app/evaluation/runner.py:404-406,415-420,452-484`。

- turns 只取第一轮，没有循环提交后续轮次；估价却计算全部轮次。
- RETRIEVAL_ONLY/RETRIEVAL_PIPELINE 使用启动管理员 actor，未使用每题 persona，因此权限测试不能反映目标用户。
- 完整 RAG/AGENT 创建默认 ALL 会话，未传 allowed_doc_ids，可能检索到快照外资料；快照 document_ids 仅用于纯检索分支。
- 完整 QA 的 persona 查不到时回退启动者 user_id，会改变题目权限条件。

应先修正执行范围、persona 和多轮行为，再使用这些报告评价策略质量；否则同一 manifest 并不保证相同实验条件。

### F10 · P1 · 预算和费用尚未形成全调用闭环（静态确认）

位置：`backend/app/retrieval/embedding.py:40-101`、`backend/app/qa/model_gateway.py:70-113,493-526`、`backend/app/qa/runtime.py:170-210`。

Embedding 绕过 model_call 记录；retry_call 多个实际请求只在最后记录一次；record 固定 attempt_no=1、reserved/settled=0、pricing=fixed。运行上限依赖这份不完整记录，普通 RAG 和批量评估也没有与真实供应商费用关联的统一预留机制。启动前估价不能替代 AC-06-02/03、AC-07-03 的运行时限额。

## 性能判断

已做得合理的部分：Embedding/Milvus 分批、MySQL 批量 insert、HTTP 和数据库连接复用、Agent graph 编译缓存、QA/INGEST/EVAL 分池。没有理由整体重写。

除了 F6/F7，优先关注以下路径：

1. `identity/deps.py:202-218` 每次 assert 都重查整个可读 build 集合；Pipeline、生成前、Judge 前会多次执行。文档数增大时，数据库扫描、结果传输及 Milvus filter 文本随可读文档数增加。保留授权复核，但复核阶段可只查询此次候选文档；不要直接删除安全检查。
2. `qa/service.py:207-259,1204-1257` 历史详情加载全部 runs，再逐 run 查累计来源、逐文档 ACL、逐 evidence 当前版本。会话增长时产生明显 N+1，且重复核验此前来源。适合分页和批量查询，仍需保持派生历史的权限语义。
3. `knowledge/router.py:75-120` async 上传路由直接执行同步数据库/文件操作，并在检查 25MB 之前 `await file.read()` 读完整文件。并发上传时有事件循环阻塞与额外内存峰值风险；这次为静态判断，未压测。
4. 前端入口全量注册 Element Plus，页面全部静态 import；本次构建单 JS 约 1.03MB。对小型内网工具不是首要阻塞，可在正确性修复后按需组件和页面延迟加载。

`test_runtime_performance.py` 检查图缓存/批次切分，`test_chunker_performance.py` 检查分块语义；它们没有负载、吞吐、P95 或内存增长断言。**目前不能给出“性能优秀”“支持多少并发”的结论。** 下一轮应分别测无模型 Search、含模型 QA、入库任务，并报告数据规模、并发、P50/P95、失败/超时率、数据库查询量和 RSS。

## 实现是否优雅

优点是模块化单体适合当前项目，RetrievalPipeline 和 ACL 复用方向正确，LangGraph 原生恢复替代平行协议也有价值。

主要维护问题：knowledge/service.py 1,692 行、qa/service.py 1,611 行、evaluation/runner.py 1,198 行，混合事务、执行编排、数据映射、计费及接口展示；前端多个函数和模板压成整行；大量 Dict/Any 加上新旧配置并存，让接口契约错误难被类型系统和现有测试发现。文件长本身不是缺陷，但 F2/F3/F8 表明重复出口和弱契约已经造成真实问题。

建议只做有直接收益的整理：统一严格校验出口、统一配置 schema、把真实服务与测试替身分开、按现有业务事务边界拆出少量函数。不要为了“优雅”增加通用框架、微服务或额外抽象层。

README 也有漂移：仍描述 Markdown/TXT 本地解析、二进制调用 `/v1/convert/file`，而当前 `parser.py` 所有格式都调用 `/v1/chunk/hybrid/file`。历史 9 月 13 日 Java/ES 报告须明确保留旧基线标识，不得当成当前代码验收。

## 推荐顺序

1. 修复版本倒退、严格校验、上下文段隔离和 Milvus 恢复；让对应复现测试通过。
2. 修复配置契约、评测范围/persona/多轮、预算记录，恢复可信的实验条件；补齐普通追问。
3. 完成明确承诺但缺失的查询增强/MMR/多向量，以及多模态质量审核流程。
4. 运行隔离环境 MySQL/Milvus/Docling/真实模型端到端验收、各策略消融、并发负载及备份恢复。

不建议给出单一完成百分比：81 条 AC 中有的只完成设计，有的有离线测试，有的已复现违反。逐项标注实现、离线验证、真实验收三层状态更有用。

## 复现命令

在项目 `backend` 目录执行（PowerShell）：

```powershell
.\.venv\Scripts\python.exe -m pytest ..\docs\reviews\2026-09-26-audit\test_audit.py -q -p no:cacheprovider --tb=short
```

当前预期结果为 8 个失败；修复后应逐项变为通过。测试放在审查目录，不改现有业务测试基线，也不修改业务代码。
