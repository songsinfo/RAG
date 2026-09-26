# 检索、上下文与证据协议

> **历史设计说明（2026-09-22）**：本文保留早期技术设计基线，其中可能包含 Java、Spring、Elasticsearch、旧 Docling API/切块方案或尚未落地的设计，不代表 `main` 当前实现。当前系统行为请以仓库根目录 `README.md`、`docs/source-walkthrough.md`、`docs/rag-evaluation-guide.md` 与 `backend/app` 源码为准。


对应 PRD-03，并由 PRD-01、02、04、05 约束。所有策略在 Java 中显式串联；框架默认 Retriever 不代替 ACL 和证据登记。

## 1. 配置与能力检查

retrieval_config 不可变 JSON 使用 version/hash。字段至少包含：
query.{rewrite,hyde,multiQuery,maxVariants}、retrieval.{mode,representations,perRouteK,numCandidates}、fusion.{rrfK,modalityWeights}、post.{rerank,rerankTopN,allowRerankFallback,mmr,lambda,parent}、context.{maxTokens,maxUnits}、answer.{strict,maxOutputTokens}、模型/Prompt 引用、限额与允许降级清单。

发布配置时校验类型/范围、索引模型维度与表示 capabilities、上下文预算和最大路数。普通用户只选 RAG/AGENT 与普通/严格，不能传任意策略 JSON。缺 Summary/Questions 的 build 不被当作支持该模式；实验启动失败 INDEX_CAPABILITY_MISSING。查询默认配置与当前 build 不兼容时返回明确错误，由管理员构建或回退配置，不能静默启用空策略。

默认候选：原查询 Hybrid/RRF + Reranker；追问允许 Rewrite；HyDE/Multi-Query/MMR/Parent/多表示分别实验后决定默认。所有开关关闭时不得有对应模型调用。

## 2. 查询侧

1. 原问题始终保留。先读取同上下文段、仍可读的最近至多 4 轮历史且总量不超过预算；旧助手事实仅用于解析指代，回答仍重新检索。
2. Rewrite 输出单个独立查询，保留错误码、时间、否定和条件。无足够历史则返回 NEED_CLARIFICATION；模型失败可按配置使用原问题并登记 degraded。
3. Multi-Query 最多新增 2 个文本问法，规范化空白后去重，不让模型提供 tenant/KB/ACL。扩展文本的来源依赖包含其所用历史。
4. HyDE 只生成一个最多 300 token 的假想段落，仅进入 Dense 查询，不进入 BM25、证据或回答上下文。
5. 每路向量均使用本 build 索引族的模型和 query 预处理。不同模型空间不得混检。

最大文本查询数 4（原始、Rewrite、两个 Multi-Query）+ 1 条 HyDE Dense 路；最多 9 路。预算估算按这一上界而非只算最终答案调用。路数上限不代表任意全开配置都能在普通请求 8 次模型调用内完成；发布时按批量 Embedding 的实际计费/请求契约估算最坏调用数，不足就拒绝该组合或显式降低扩展数量。

## 3. 授权候选与两种检索

SQL 在本次请求的知识库范围内求得当前可读 LIVE 文档的 active_build_id 列表，保存候选许可快照及 tenant.security_epoch。若空，返回 NO_EVIDENCE，不透露隐藏文档数量。最多 10,000 个 build ID，超过显式报错。

BM25：查询 original 索引，仅 canonical child 原文，包含 title、text 中文字段及提取出的 code_terms。中文初始采用 ES 内置 cjk analyzer，错误码/接口名由 Java 保留为 keyword 数组，用 term 精确匹配加权。分析器必须以中文同义、标点、版本号和错误码样本验证，不能假设中文分词天然达标。

Dense：查询 vector 索引的指定 representation 类型，设置 tenant_id + build_id allow-list 在 knn.filter 内。不是只在返回结果后过滤。ES 文档明确区分预过滤与普通后过滤，后者可能导致有授权候选却不足 K。[kNN 过滤说明](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-knn-query)

每路初始 K=50、Dense num_candidates=200；返回时只取 ID/原始分数/位置/表示类型，正文统一 SQL 回查。参数是表示级候选上限，canonical 去重后可能不足 50，记录实际数量，首版不无限补取。Query/Index 模型不一致、ES 错误或未就绪产生失败，不放宽权限。

## 4. RRF、重排与 MMR

每一路定义为 query_variant × modality，Dense 路内的 Original/Summary/Questions 合并后按 canonical_chunk_id 取最好原始名次，压缩为去重排名；保留所有命中表示供诊断，得分仅算一次。

RRF(c) = Σ route_weight(r) / (60 + rank_r(c))，未命中贡献 0，rank 从 1 开始。Hybrid 初始 Dense 与 BM25 各占总权重 0.5，各自均分到本模态有效路；仅一个模态总权重为 1。有效路数按计划固定，失败不自动重新分配掩盖降级。同分按 canonical ID 稳定排序。RRF 在 Java 实现，不依赖 ES 服务端 RRF 授权能力。

融合最多 30 个 canonical 候选 → SQL 复核当前权限、生效 build 与来源映射 → 回查原文 → Reranker。重排输入为独立查询+原文，不是生成摘要；重排分数仅表示本次排序，不称为正确率。

MMR 选取初始 6 个候选：
- relevance：本次候选的重排分数 min-max 到 [0,1]；无重排则使用 RRF 分数同样归一化；全相等记 1。
- redundancy：canonical Original 向量的 cosine，映射为 (cos+1)/2；同一 embedding 模型，不用摘要向量替代。
- 每步最大化 λ×relevance − (1−λ)×max redundancy，初始 λ=0.7；空集合第二项为 0，平局按已有排名。
- 开启 MMR 需要 Original 向量制品，纯 BM25 且未建向量时返回能力缺失。记录每步相关性、多样性代价和最终选择，不做一文档最多一条的硬配额。

Reranker 失败仅当配置允许才保留融合顺序并返回 RERANK_SKIPPED；数据权限故障绝不类似降级。

## 5. Parent 扩展与上下文预算

选中的 child 可扩为同 build parent，父单元重新授权。多个 child 同父只放一次；合并重复来源范围并保留命中 child→实际 parent 关联。不把一个父段假装成多个不同证据加大覆盖。

总上下文初始 6,000 token，输出 1,200，另留系统提示/历史/工具结构安全余量；必须满足已验证模型窗口。按来源单元边界装入，不截断表格表头与引用范围。大 parent 装不下时退回命中 child 及必要标题，标记 PARENT_TRIMMED；再装不下则剔除并记缺口。

evidence E1..En 只在确定实际上下文后编号，映射 version/build/source_unit/范围/实际正文 hash。每次外发（含 rerank、vision、judge）先持久化 run_source 依赖，再通过权限闸门复核发送；失败登记发送状态，不把“有依赖”混同“供应商已收到”。

## 6. 答案输出结构

模型只输出结构化对象：status、claims、gaps、conflicts；每个 claim = {id, text, evidence:[{evidenceId, start, end}]}。text 为需要依据的事实声明，evidence 范围以对应已发送文本的 Unicode code point 偏移定义；Java 显式转换，避免 UTF-16 对中文扩展字符偏移混乱。

应用从 claims 确定性渲染正文与编号，不接受模型另给一段无法核对的自由 answer。gaps 只呈现未覆盖的问题/条件，不用于夹带额外业务事实；conflicts 两边各引用受支持 claim。无证据答复用固定提示，不让模型生成企业规则。

程序校验：
- 对象符合 schema，枚举和容量合法；
- evidence ID 属于本次实际发送集合，source span 在界内、文本 hash 一致；
- 每个展示事实至少有非空合法来源，正文完全由已校验 claims 渲染；
- 原文是引用依据，HyDE、摘要、假设问题 ID 不能充当 evidence；
- 图片描述/OCR 的派生标记必须到前端；
- 最终提交前再次检查全部输入依赖、会话所有者、取消和生效版本。

程序证明的是结构与来源合法性，不证明自然语言推断正确。严格模式用 JUDGE 对每一 claim+实际证据判 SUPPORTED / UNSUPPORTED / UNCERTAIN；未全部通过不返回已验证成功，可返回 VERIFICATION_FAILED 或保留已验证部分并明确 PARTIAL。检查超时返回 VERIFICATION_INCOMPLETE，不能静默普通成功。

## 7. 失败与安全边界

模型格式失败最多一次受预算限制的修复，不得执行输出中“更新权限”等指令。跨文档冲突由证据并列展示，不简单选较新日期。证据不足只回答支持部分或拒答，低检索 score 不是独立的真实性阈值。

资料是被引用数据，放在固定分隔结构中；工具白名单、服务器授权和校验承担边界，Prompt 只是辅助。禁止任意链接抓取与模型侧请求修改。调用前授权复核采用单实例闸门，精确并发语义见数据库设计。

Trace 区分原始排名、融合排名、重排排名、MMR 排名、最终上下文、最终引用。评估分别计算召回阶段和上下文阶段指标，避免“检索找到了但扩展挤掉”的问题被掩盖。
