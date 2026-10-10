# RAG 项目源码导读：从上传文件到回答与评估

这份文档带你沿着真实数据流阅读当前 Python 实现。目标是读完后能解释：请求从哪里进入、主要函数按什么顺序调用、关键数据怎样变化、结果存在哪里，以及失败时应该检查哪一层。

核对基线：2026-10-10，当前 `main`（合并提交 `2b40a9b`，包含已合并的 [PR #14](https://github.com/songsinfo/RAG/pull/14)）。统一业务 Agent 与版本化跨轮上下文快照已进入主分支；**合并不代表 UA-01～UA-12 全量生产验收通过**，尤其不代表真实业务系统接入或供应商 KV Cache 收益已实测。本文按当前 `main` 源码说明调用链；示例代码省略无关分支，不能直接当作可运行函数。教学数据用简短 ID 代替运行时 UUID，不代表真实实验结果。

**阅读规则：当前行为以 `backend/app` 源码为准。** `docs/design` 是早期设计，其中仍含 Java、Spring、Elasticsearch 和尚未完全落地的能力，不应直接用来推断当前 Python 执行过程。

阅读导航：[项目地图](#1-先认识项目再确定阅读顺序) → [贯穿示例](#2-用一个例子贯穿所有流程) → [启动与任务](#3-启动与入口请求和后台任务如何衔接) → [文档入库](#4-文档入库文件如何变成可以检索的内容) → [检索](#5-检索问题如何找到当前用户可用的证据) → [统一问答](#6-统一问答工具证据如何变成用户答案) → [Agent 与评估](#7-agent-与评估统一问答主线) → [失败排查](#8-第二遍阅读追踪权限状态和失败) → [测试跟读](#9-跑测试读源码一个可操作的学习路线)。

## 1. 先认识项目，再确定阅读顺序

这个项目是企业内部知识问答系统：先把文件转换为可检索内容，再在当前用户可读范围内检索，把证据交给模型，最后校验并展示带引用的答案。统一 Agent 让模型决定是否调用知识检索或四个固定模拟业务工具；评估模块则批量执行并检查这些流程。

| 组件 | 在本项目中的工作 | 源码入口 |
| --- | --- | --- |
| Vue 前端 | 上传、提问、SSE 阶段进度与轮询兜底、查看引用 | [Chat.vue](../frontend/src/Chat.vue)、[Documents.vue](../frontend/src/Documents.vue) |
| FastAPI | HTTP 路由、认证依赖、CSRF、错误响应 | [main.py](../backend/app/main.py) |
| MySQL | 用户权限、文档版本、生效构建、任务和运行记录 | [models.py](../backend/app/models.py)、[db.py](../backend/app/db.py) |
| 跨轮上下文 | 按 Segment 保存授权历史、结构化业务对象、来源依赖和压缩版本（不是模型 KV Cache） | [segment_context.py](../backend/app/qa/segment_context.py)、[business_context.py](../backend/app/qa/business_context.py) |
| Worker | 从 MySQL 领取 INGEST/PURGE/EVALUATE/全部新问答的持久任务；旧 REALTIME RAG 仅作历史兼容 | [worker.py](../backend/app/runtime/worker.py) |
| Docling | 所有支持格式统一解析并由 HybridChunker 生成语义块 | [parser.py](../backend/app/knowledge/parser.py) |
| Milvus | 保存子块索引，完成 Dense/BM25/Hybrid 检索 | [milvus_service.py](../backend/app/retrieval/milvus_service.py) |
| LiteLLM | 对接模型服务，提供 Chat、Embedding、Reranker 等别名 | [model_gateway.py](../backend/app/qa/model_gateway.py)、[embedding.py](../backend/app/retrieval/embedding.py) |
| LangGraph | 普通 RAG 的无 Checkpointer 检索 DAG；Agent 的 model/tools 循环和 Checkpointer 恢复是另一张图 | [rag_graph.py](../backend/app/retrieval/rag_graph.py)、[agent.py](../backend/app/qa/agent.py) |

第一遍按第 2～7 节看成功主线。第二遍看第 8 节的权限、租约和失败分支。第三遍运行第 9 节的测试，观察变量。不要一开始从 `models.py` 第一行一路读到底；先遇到哪个对象，就回头找它对应的表。

## 2. 用一个例子贯穿所有流程

假设上传 UTF-8 文件 `retry.md`：

```markdown
# 订单重试规则

订单重试前必须检查幂等键。

重试前应确认尚未达到重试次数上限。

重试前需核对补偿日志，避免重复补偿。
```

用户提问：“订单重试前需要检查哪些事项？”

整个过程可以概括为：

```mermaid
flowchart TD
    A[retry.md 文件字节] --> B[上传接口：保存原件并创建 INGEST job]
    B --> C[Worker：解析 source_unit]
    C --> D[切块：PARENT / CHILD / chunk_span]
    D --> E[CHILD 写入 Milvus]
    E --> F[MySQL 发布 active_build_id]
    Q[用户问题] --> R[持久化 qa_run]
    R --> DURABLE[同事务 QA_RUN job → Durable Worker]
    F --> T[MySQL 筛选可读生效 build]
    DURABLE --> AGENT[LangGraph Agent：模型选择工具]
    AGENT -->|选择知识检索| T
    T --> U[RagRetrievalGraph：ACL / Dense+BM25 / RRF / Rerank]
    AGENT -->|选择模拟业务工具| MOCK[固定 fixture：TOOL 来源，mock=true]
    MOCK --> V[证据 E1、E2 等]
    U --> V[证据 E1、E2 等]
    V --> W[模型输出 claims 和引用范围]
    W --> X[校验并渲染 answer]
    X --> Y[保存结果；前端 SSE 看阶段、终态 GET 获取答案]
    Y --> Z[评估执行器对照标准事实评分]
```

先记住这些对象的区别：

| 对象 | 含义 | 为什么单独存在 |
| --- | --- | --- |
| `document` | 逻辑文档 | 文件更新后仍属于同一份资料 |
| `document_version` | 某次上传的版本 | 保留原件和版本身份 |
| `index_build` | 某次解析与索引构建 | 新构建完成后才切换生效指针 |
| `source_unit` | 标题、段落、表格、代码等原文单元 | 保存来源位置 |
| `chunk` | 为检索或上下文准备的文本块 | 组织不同粒度内容 |
| `chunk_span` | 子块与原文单元及偏移的关系 | 让检索结果可以追溯到原文 |
| `hit` | 某次查询返回的候选记录 | 有分数、build、chunk 等信息 |
| `evidence` | 某次问答登记的证据 | 分配运行内编号，例如 E1 |
| `claim` | 模型给出的一条事实断言 | 必须关联本次 evidence |
| `answer` | 程序根据 claims 渲染的展示文本 | 保持正文和引用的对应关系 |
| `context_segment` | 一个会话范围内的持久化历史及业务对象状态 | 支持下一次新 run 按当前权限续接历史 |
| `job.checkpoint` | 单次 `QA_RUN` 的 LangGraph 节点状态 | 支持当前 run 的暂停/崩溃恢复，而不是替代跨轮历史 |

例如 `E1` 不是全局文档 ID，而是本次问答的证据编号。另一轮问答的 E1 可以指向完全不同的资料。

## 3. 启动与入口：请求和后台任务如何衔接

### 3.1 从哪里开始读

打开 [main.py](../backend/app/main.py)，先定位 `lifespan()`，再看 `csrf_middleware()` 和 `app.include_router(...)`。

```python
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(retrieval_router, prefix="/api/v1")
app.include_router(qa_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")
app.include_router(eval_router, prefix="/api/v1")
```

路由文件里的路径还要加 `/api/v1`。例如 `qa/router.py` 中的 `/conversations`，最终是 `/api/v1/conversations`。

`lifespan()` 检查数据库，初始化种子数据并处理未完成任务恢复；非测试环境且启用内嵌 Worker 时，启动三组工作线程：

- `qa` 领取所有新在线问答和新完整问答评估的 `QA_RUN`，旧 REALTIME 运行仅保留兼容执行代码。
- `ingest` 领取 `INGEST / PURGE`。
- `eval` 领取 `EVALUATE`。

Worker 也有独立入口。学习时先理解“数据库里的 job 被谁领取”，再研究部署在哪个进程。

### 3.2 HTTP 层负责什么

阅读 [api.ts](../frontend/src/api.ts) 的 `api()`：请求地址加 `/api/v1`，携带会话 Cookie，写请求处理 CSRF，普通对象转换为 JSON，上传使用 FormData。

后端 [identity/deps.py](../backend/app/identity/deps.py) 的 `get_current_actor()` 从 `RAG_SESSION` 查会话、用户和租户，构造：

```python
@dataclass(frozen=True)
class Actor:
    tenant_id: str
    user_id: str
    admin: bool
    security_epoch: int = 0
```

`Actor` 是后续权限判断的身份上下文。不能从客户端 JSON 里的用户 ID 推断当前身份。`require_admin()` 检查维护资格，文档阅读权限则由另一组函数判断。

### 3.3 为什么请求返回了，任务还没完成

文档入库、评估主任务及**所有新在线问答**均写 MySQL Job，由 Worker 执行；新问答先同事务写 `qa_run + QA_RUN Job`。HTTP 202 只表示请求已接收，不表示答案生成完成。旧 `REALTIME` 路径仅为历史未完成运行保留兼容代码。

在 [worker.py](../backend/app/runtime/worker.py) 按下面顺序读：

```text
run_forever()
  → run_once()
      → _claim_one()                 领取可执行或租约过期的任务
      → _heartbeat_loop()           执行期间续租
      → 按 kind 分派给 _run_*()
      → _requeue_failed_if_retryable()
```

`_claim_one()` 使用行锁与 `skip_locked=True`，领取时增加 `lease_epoch`。这表示“当前第几代执行者”。旧 Worker 即使恢复运行，也不应覆盖新 Worker 的结果，后面的发布与终态写入会检查租约。

**测试环境不同：** 入库、问答、评估的一些入口会直接同步执行。因此测试里 POST 完就能读到结果，不能据此推断生产请求也在 HTTP 线程里完成全部工作。

## 4. 文档入库：文件如何变成可以检索的内容

### 4.1 完整调用链

```text
Documents.vue::upload()
  → POST /api/v1/admin/documents
  → knowledge/router.py::upload_document()
  → KnowledgeService.upload_document()
      → 保存原件和版本、构建记录
      → 写 job(kind=INGEST, status=QUEUED)
  → DurableJobWorker._run_ingest()
      → read_artifact(storageKey)
      → KnowledgeService._execute_ingestion_job()
          → parse_and_chunk_document()
          → Docling /v1/chunk/hybrid/file
          → adapt_docling_hybrid_chunks()
          → source_unit / PARENT / CHILD / chunk_span 入库
          → MilvusHybridService.upsert_chunks()
          → index_build READY + document.active_build_id 切换
```

阅读入口：[knowledge/router.py](../backend/app/knowledge/router.py)、[knowledge/service.py](../backend/app/knowledge/service.py)、[artifacts.py](../backend/app/knowledge/artifacts.py)。

`upload_document()` 接收文件字节、知识库 ID、幂等键和构建配置，完成权限及请求检查，并保存原始文件和数据库记录。返回 `documentId / versionId / buildId / jobId` 后，客户端可通过任务接口查看进度。

**这四个 ID 不要混用。** 查文件生命周期看 document；查上传原件看 version；查解析和索引内容看 build；查执行进度看 job。

### 4.2 统一解析与切块：字节 → Docling Hybrid Chunks

打开 [parser.py](../backend/app/knowledge/parser.py) 的 `parse_and_chunk_document()`。当前所有支持格式都进入同一条路径：

```text
file_bytes
  → Docling Serve /v1/chunk/hybrid/file
  → Docling Parser
  → HybridChunker
  → chunks JSON
  → adapt_docling_hybrid_chunks()
```

Markdown、TXT、PDF、DOCX、Office 等不再分成“本地文本解析”和“二进制 Docling 解析”两条链路。请求会根据配置传入 OCR、表格结构、heading hierarchy、tokenizer、max_tokens 与 merge_peers 等参数。

默认 Child 上限来自 `DOCLING_CHUNK_MAX_TOKENS=400`，默认 Parent 上限来自 `DOCLING_PARENT_MAX_TOKENS=1200`。这里单位是 token，不是字符；Child 的实际分割由 Docling HybridChunker 负责。

### 4.3 适配：Hybrid Chunks → source_unit / PARENT / CHILD / span

打开 [chunker.py](../backend/app/knowledge/chunker.py) 的 `adapt_docling_hybrid_chunks()`。这个函数不再重新切 Child，而是把 Docling 已经生成的 Hybrid Chunk 适配到本项目的数据模型。

核心规则：

- 每个 Docling Hybrid Chunk 对应一个 CHILD。
- 相邻、heading path 相同的 Child 会按 `parent_max_tokens` 组织成 PARENT。
- 同一组 Child 会生成对应 SourceUnit。
- `location` 保留 Docling 的 headings、captions、docItems、pageNumbers 和 metadata。
- `chunk_span` 记录 CHILD 在 SourceUnit 中的偏移和原始 location，供 evidence provenance 使用。

示意数据：

```json
{
  "kind": "CHILD",
  "parent_id": "parent-1",
  "text_content": "订单重试前必须检查幂等键。",
  "token_count": 12
}
```

PARENT 主要用于检索后的上下文扩展；CHILD 才是索引和高精度召回单元。

### 4.4 建索引与发布：有向量不等于已生效

在 `_execute_ingestion_job()` 看这一段：

```python
child_chunks = [c for c in chunks if c.get("kind") == "CHILD"]
```

只有 CHILD 传给 `MilvusHybridService.upsert_chunks()`。入库还按构建配置为 Child 生成 ORIGINAL，以及可选 SUMMARY / QUESTION 表示；这些表示分别入索引，但都关联原始 Child，生成证据前还原原文。PARENT 留在关系数据里，检索后可扩展为上下文。

在 [milvus_service.py](../backend/app/retrieval/milvus_service.py) 看 `upsert_chunks()`，继续追到 [embedding.py](../backend/app/retrieval/embedding.py) 的 `get_embeddings()`。文档文本转换为 dense 向量，真实 Milvus 集合还通过 BM25 function 生成 sparse 表示。索引携带租户、文档、版本、build、chunk 和索引代次等字段，便于过滤和溯源。

最后回到入库执行器的发布事务：将 `index_build.status` 更新为 `READY`，将 `document.active_build_id` 指向该 build，并完成任务。

```text
旧 active_build_id → 仍是当前可检索版本
新 build 正在解析/切块/写索引 → 尚未成为当前版本
发布事务成功 → active_build_id 指向新 build
```

这也是“Milvus 不是版本权威”的含义：数据库生效指针决定哪些索引记录可以参与当前检索。

**对应测试：** [test_ingestion.py](../backend/tests/test_ingestion.py) 中 `test_document_upload_and_preview()`。重点看上传响应、任务状态、preview 中的 units/chunks，以及新版本上传后的变化。

## 5. 检索：问题如何找到当前用户可用的证据

### 5.1 先读短函数，再读底层实现

**先区分入口**：新完整问答走 `LangGraphAgent`；Agent 选择 `search_knowledge` 才调用 [rag_graph.py](../backend/app/retrieval/rag_graph.py) 的 `RagRetrievalGraph.retrieve()`。独立 Search API 和纯检索评估仍调用 [pipeline.py](../backend/app/retrieval/pipeline.py) 的 `RetrievalPipeline.retrieve()`。旧 `mode=RAG` 在线代码暂保留供历史任务兼容，不再承接新请求。这些入口复用同一套 Milvus、RRF、Reranker、MMR、Parent、权限与来源工具。

Agent 的 `search_knowledge` 检索工具图实际节点顺序如下（Dense / BM25 并行完成，再汇入 RRF）：

```text
QaService._execute_run(mode=AGENT)
  → LangGraphAgent._tools_step(search_knowledge)
  → RagRetrievalGraph.retrieve()
  → resolve_scope：config + evaluation_scope + ACL + active_build_id
       ├─ 无可读 build → no_scope → END (hits=[])
       └─ 有可读 build → prepare_query (Rewrite / Multi-Query / HyDE)
                           ├─ dense_recall  (可按配置禁用)
                           └─ bm25_recall   (可按配置禁用)
                         → rrf_fusion (汇合两路)
                         → hydrate_and_acl
                         → rerank → mmr → parent_expansion
                         → context_budget → finalize_hits
                         → END (hits / warnings / config / allowedBuildCount)
```

独立 Search API / RETRIEVAL_* 纯检索评估继续使用原单体检索函数，函数内部的步骤为：

```text
RetrievalPipeline.retrieve(actor, query, kb_ids, document_ids, config_id, run_id, history)
  → load_retrieval_config()
  → 应用 run_document_scope()（评测运行）
  → get_readable_build_records()
  → _prepare_queries()：按配置 Rewrite / Multi-Query / HyDE
  → 各查询变体分别调用 MilvusHybridService.search() 做 BM25 / Dense 召回
  → 多路结果通过 rrf_fuse() 融合
  → _hydrate_original_chunks()：多表示命中还原为原始 Child 文本
  → assert_hits_still_readable()
  → 可选 rerank_hits()
  → 可选 _mmr_select()
  → 可选 _expand_parents()
  → 按 context_max_bytes 与 final_top_k 限制上下文
  → _attach_provenance()
  → 按 final_top_k 截取
  → 再次 assert_hits_still_readable()
  → 返回 hits / warnings / config / allowedBuildCount
```

**图的执行边界**：内层检索图只返回证据，外层 Agent 负责模型、工具调用和最终回答，`QaService` 负责 Judge/提交。检索子图不启用 Checkpointer，即使外层 Agent 有 Durable Job，也不能从内部 `rrf_fusion` 或 `rerank` 节点恢复。新建完整问答评估仍走 `QA_RUN` Worker，但内层检索图没有节点级持久化恢复能力。详见 [普通 RAG LangGraph 检索图说明](rag-langgraph-retrieval.md)。

### 5.2 权限过滤先于检索

```python
with get_db_connection() as conn:
    readable = get_readable_build_records(conn, actor, kb_ids, document_ids)

allowed_build_ids = [row["buildId"] for row in readable]
```

在 [identity/deps.py](../backend/app/identity/deps.py) 定位 `get_readable_build_records()`：检查租户、用户有效性、知识库授权、文档 ACL、文档 LIVE、当前 build READY，再应用可选知识库/文档范围。

没有可读 build 就直接返回空结果。管理员维护资格也不会在这段逻辑里自动绕过正文授权。

允许列表和 `indexEpoch` 传入 Milvus。检索后再次检查，是为了应对“检索期间撤权或切换了版本”；尤其不能在将候选发送给外部 Reranker 后才第一次检查权限。

### 5.3 BM25、Dense 和 Hybrid 在哪里分支

无论 Agent `search_knowledge` 使用的 `RagRetrievalGraph`，还是独立 Search API / 纯检索评估使用的 `RetrievalPipeline.retrieve()`，都根据当前有效配置选择 BM25 / Dense 路线，调用 `MilvusHybridService.search()`；底层 `search()` 也支持直接 HYBRID 调用：

| 模式 | 核心操作 | 阅读时关注 |
| --- | --- | --- |
| BM25 | 搜索 sparse 字段 | 查询词与文档词项匹配 |
| DENSE | 问题 embedding → dense 向量搜索 | 查询与索引维度一致 |
| HYBRID | 两路检索，通过 RRF 融合 | 两路候选数量和融合排名 |

Agent 的检索工具图通过并行 `dense_recall` / `bm25_recall` 节点取回排名列表，在 `rrf_fusion` 节点调用 `rrf_fuse()`；`RetrievalPipeline.retrieve()` 也沿用相同的应用层加权融合逻辑。底层 `MilvusHybridService.search(mode="HYBRID")` 也分别调用 `client.search()`，将 ORIGINAL / SUMMARY / QUESTION 等表示归一并按 canonical chunk 去重后再做 RRF；不再直接使用 Milvus `hybrid_search(..., ranker=RRFRanker(...))`，避免同一子块的不同表示重复投票。RRF 综合各路线里的名次，不直接混加两种不可比的原始分数。

测试环境另有内存检索和确定性 embedding 路径。它们适合验证过滤和执行逻辑，不能代表真实向量的语义质量，也不能用其表现估计生产 Milvus 性能。

### 5.4 重排、父块扩展与来源映射

`rerank_hits()` 使用问题和候选文本重新排序；是否启用、保留多少条、失败是否允许降级，由有效配置决定。阅读 [config.py](../backend/app/retrieval/config.py) 的 `RetrievalConfig.from_payload()`，不要只看配置 JSON 的名字就认为某个开关已被执行。

`_expand_parents()` 找到命中 CHILD 对应的 PARENT，将未超过 `parent_max_chars` 的父文本放进 `hit.text`，原命中文本留在 `matchedText`，并增加 `parentChunkId`，同父块去重。父文本过长时保留 Child，不截断父文本，并记录 `PARENT_TRIMMED` / `parentTruncated=true`。扩展后还按 `context_max_bytes` 限制 UTF-8 字节总量；若最佳父块独自超限，尝试退回原始 Child，不能满足预算则不保留该项。

因此：

```text
chunkId        = 用来定位检索命中的子块
parentChunkId  = 实际扩展使用的父块（如果启用）
matchedText    = 子块命中文本（发生父扩展时）
text           = 后续用于上下文的文本
```

`_attach_provenance()` 通过 `chunk_span → source_unit` 补充来源。能在当前上下文找到完整单元时，设置 `contextMapped=true` 并保存上下文偏移；找不到时仍保留来源信息，不能把 `contextMapped=false` 解释为已有精确上下文映射。

两条检索编排入口均接入 Rewrite、HyDE、Multi-Query 和 MMR，但都由配置开关控制。Rewrite 还要求存在历史，并保留原问题与改写问题；Multi-Query 增加等价问法；HyDE 只贡献 Dense 路线，不把假想文本作为事实证据；MMR 在重排后、父块扩展前选择候选。开启策略不等于已证明效果更好，仍需固定其他条件做实验。

**对应测试：** [test_qa.py](../backend/tests/test_qa.py) 的 `test_qa_run_lifecycle_and_evidence()` 校验统一 Agent 的 Durable Job、知识检索工具图各阶段事件与来源证据；[test_enterprise_retrieval.py](../backend/tests/test_enterprise_retrieval.py) 的 `test_config_driven_rerank_parent_expansion_and_provenance()`、[test_retrieval.py](../backend/tests/test_retrieval.py) 的 `test_hybrid_search_acl_and_rebuild()` 校验仍被 Agent / Search API 等入口复用的原检索组件。

## 6. 统一问答：工具证据如何变成用户答案

**当前 `main` 的统一问答运行路径（PR #14 已合并）**：

```text
Chat.vue::submit()（不再发送 mode）
  → QaService.submit_run()
  → qa_run(mode=AGENT) + QA_RUN Job
  → DurableJobWorker → QaService._execute_run()
  → LangGraphAgent.run() → model / tools 循环
      ├─ search_knowledge → RagRetrievalGraph.retrieve()
      ├─ read_section → 章节内容与文档 ACL
      └─ diagnose_coupon / get_final_price_rules /
         diagnose_product_group / diagnose_subsidy → 固定模拟 fixture
  → 注册 DOCUMENT 或 TOOL evidence（E1、E2 …）
  → validate_claims(evidence_map) → 可选 strict Judge
  → _finalize_run() → SSE 阶段事件与 GET /runs/{runId}
```

`DOCUMENT` 证据包含文档/build、原文、source unit / location；`TOOL` 证据包含 `toolName`、`queryObjects`、`queriedAt`、`fixtureVersion`、`mock` 和固定结果快照，不创建假 `documentId/buildId`。两者都使用本次运行的 `evidenceId` 与 Unicode 字符半开范围做结构校验。文档读取继续 ACL/build 复核；TOOL 快照只表示本地演示，未查询真实系统。

**示例（合成数据，并非实际查询）：**调用 `diagnose_coupon(user_id="u-demo",coupon_id="c-demo")` 会得到“参与金额 168 元、门槛 200 元、排除商品 70 元”的固定演示结论。无参数的 `get_final_price_rules()` 给出“先活动优惠、再券、再补贴、价格最低为零”的演示规则，示例到手价 `200-20-30-10=140` 元。

页面展示后端确定的“**模拟结果，未查询真实业务系统**”，并区分文档来源与工具来源。模型没有引用到真实取得的 Evidence 或引用偏移错误时，应失败关闭，而不是生成无依据答案。严格 Judge 判断本次证据是否支持断言，不证明演示业务结果真实。

**历史/结果注意：**`qa_run` 保存执行状态，`result_json.status` 表示业务回答状态；`get_run()` 会合并这两种信息，因此同一响应的 `status` 不一定等于数据库运行状态。已实现跨 run 的授权历史快照、结构化业务对象、压缩边界和版本记录；历史来源每次复用仍需 ACL/build 复核。这并非跨 run 共享 LangGraph checkpoint，也**不能**视为供应商 KV Cache 命中或成本收益已实测。

## 7. Agent 与评估：统一问答主线

### 7.1 Agent 增加了什么决策

当前新问答统一使用 Agent：模型可决定检索、调用固定模拟业务工具，或在已有证据足够时结束。检索图负责召回与重排，不把固定知识检索硬编码为每次问答必经步骤。

在 `_execute_run()` 的 `mode == "AGENT"` 分支进入 [agent.py](../backend/app/qa/agent.py) 的 `LangGraphAgent.run()`。按这个顺序阅读：

```text
run()：准备输入、历史、策略、截止时间和 checkpoint/resume
  → _get_graph()：定义图
  → _model_step()：模型决定下一步
      ├─ 无 tool_calls → 解析最终 JSON → validate_claims → 结束
      └─ 有 tool_calls → _tools_step()
                           → 执行工具、累积证据、追加 tool 消息
                           → 返回 _model_step()
```

图的实际定义节选：

```python
builder.add_node("model", cls._model_step)
builder.add_node("tools", cls._tools_step)
builder.add_edge(START, "model")
builder.add_conditional_edges(
    "model",
    cls._route_after_model,
    {"tools": "tools", END: END},
)
builder.add_edge("tools", "model")
```

`AgentState` 保存 messages、`business_state`、round、tool_calls_count、repeated_calls、evidence、evidence_map、warnings 和 final_answer。`AgentContext` 保存身份、run ID、配置、截止时间和回调。前者是本次图执行的可恢复状态，后者是不可直接持久化的运行依赖；跨 run 的稳定记忆另由 `context_segment` 管理。

### 7.2 跨轮上下文与 Checkpointer：区分两种持久化

不要将 `context_segment`、`AgentState` 和 `job.checkpoint` 当成同一份状态。它们有不同的生命周期：

| 状态/存储 | 读写入口 | 边界与用途 |
| --- | --- | --- |
| `context_segment.context_payload` | [segment_context.py](../backend/app/qa/segment_context.py) 的 `load_segment_context()` / `append_completed_turn()` | **跨 run** 复用当前 Segment 的已授权历史文本、业务对象及来源依赖；不保存模型 KV |
| `AgentState` | [agent.py](../backend/app/qa/agent.py) 的 `run()` / `_model_step()` / `_tools_step()` | **单次 run** 的消息、工具调用次数、证据、`business_state` 等图状态 |
| `job.checkpoint["langgraph"]` | [job_checkpointer.py](../backend/app/qa/job_checkpointer.py) 的 `JobCheckpointSaver` | **单次 QA_RUN** 的 LangGraph checkpoint 与 pending writes；支持 interrupt/resume、Worker 失败后从图节点边界恢复 |

**第一次执行新 run** 的主链：

```text
QaService.submit_run() → qa_run + QA_RUN job
  → Worker → QaService._execute_run(mode=AGENT)
  → LangGraphAgent.run()
      → JobCheckpointSaver：先检查该 run 是否有可恢复 checkpoint
      → 新 run：conversation_memory() → load_segment_context()
          → 校验 tenant / owner / segment / lease / 历史 document ACL + active build
          → 若无新格式快照，回退到 _rag_history() 读取受权旧历史
      → 将历史 user/assistant 消息与 businessState 注入 AgentState
      → model → tools → model（search_knowledge 可进入 RagRetrievalGraph）
  → QaService._verify_answer()
  → QaService._finalize_run()（成功终态事务）
      → append_completed_turn()：同事务写入当前轮及新的 Segment 快照
```

`context_payload` 的关键字段是 `messages`（仅受限的 user/assistant 历史）、`businessState`（明确对象 ID、待澄清信息与有来源的模拟工具引用）、`sourceRuns`、`throughTurn` 和 `boundaryTurn`。对应数据库列还有 `context_version`、`context_through_turn`、`context_boundary_turn`、`context_last_run_id`。每完成一轮会推进 `throughTurn`；**普通追加不递增 `context_version`，只有发生压缩替换时才递增版本并更新边界**。快照长度达到阈值后按完整轮次保留近期消息，不把旧历史文本提升成系统指令。

历史复用前，[context.py](../backend/app/qa/context.py) 的 `conversation_memory()` 优先读取快照；`load_segment_context()` 会验证来源属于同租户、同用户、同 Segment 的更早 run，逐项重新核对文档 ACL 与有效 Build，并登记 `run_dependency` / `run_source(purpose=HISTORY)`。一旦来源撤权或 Build 漂移，按 `SOURCE_CHANGED` 失败关闭；快照不是永久阅读授权。旧 run 的 `E1` 等 evidenceId 不会转成新 run 的有效证据。

Worker/HITL **恢复当前 run** 时，优先从 `JobCheckpointSaver` 取回未完成节点及 `AgentState.business_state`，遵守 `lease_epoch` fencing，而不是把整个跨轮历史重新当成一次新的 Agent 运行。外层 Checkpointer 无法恢复无持久化 Checkpointer 的内层 `RagRetrievalGraph` 任意节点，也不保证外部模型/工具调用 exactly-once。

原有数据库需要同时应用 [工具来源迁移](design/migrations/20261010_add_tool_run_sources.sql) 与 [跨轮快照迁移](design/migrations/20261010_segment_context_snapshot.sql)（按实际已应用迁移判断）。**这是一套应用层上下文持久化，不是供应商 KV Cache；真实缓存命中率、延迟与费用收益仍需实测。**

### 7.3 六个工具与本例轨迹

| 工具 | 实现路径 | 作用 |
| --- | --- | --- |
| `search_knowledge` | `_tools_step()` → `RagRetrievalGraph.retrieve()` | 在授权范围检索并登记 DOCUMENT 证据 |
| `read_section` | `_tools_step()` → `_read_section()` | 读取指定文档/build/source unit 的内容 |
| `diagnose_coupon` | `_tools_step()` → `execute_business_tool()` | 根据明确 user_id/coupon_id 返回固定模拟不可用说明 |
| `get_final_price_rules` | 同上 | 无参数的模拟到手价规则 |
| `diagnose_product_group` | 同上 | 根据 user_id/product_group_id 返回模拟未出品原因 |
| `diagnose_subsidy` | 同上 | 根据 user_id/activity_id 返回模拟未命中原因 |

教学轨迹可能是：

```text
用户：订单重试前需要检查什么？
模型：调用 search_knowledge(query="订单重试 检查事项")
程序：返回证据摘要与 E1 编号，追加 role=tool 消息
模型：若证据已足够，输出三条 claims；否则继续搜索或 read_section
程序：validate_claims 后返回 answer
```

`tool_calls` 是模型提出的请求；工具真正执行、查权限、记录来源和限制预算由 Python 程序完成。模型不能因为输出一个工具名就直接绕过权限。

搜索结果会去重，证据保存在状态中。工具消息发送压缩摘录，而完整原文仍可保存在 `evidence_map`；二者可能长度不同，不能把“系统存了完整证据”自动等同于“模型完整读过该证据”。

### 7.4 为什么循环不会无限运行

阅读 [runtime.py](../backend/app/qa/runtime.py) 的 `AgentRuntimePolicy`、`assert_runtime_budget()`、`compact_messages()`、`retry_call()`。

当前有截止时间、主循环次数、工具调用数、重复调用、模型调用和 token 使用等约束。超过限制会产生明确错误；上下文压缩尽量保留当前问题及工具消息配对。

需要审批的工具通过 LangGraph `interrupt()` 暂停；`JobCheckpointSaver` 将图状态持久化到任务相关存储。审批接口进入 `QaService.resolve_tool_approval()`，随后 `queue_resume()` 安排恢复。理解普通 model/tools 循环后，再读 [job_checkpointer.py](../backend/app/qa/job_checkpointer.py)，不必第一遍就深入 checkpoint 序列化。

### 7.5 统一 Agent 的严格验证

新运行由 `LangGraphAgent.run()` 产生结构化结果，随后由 [service.py](../backend/app/qa/service.py) 的 `_verify_answer()` 执行严格验证；旧 RAG 代码为历史兼容保留。默认标记 `verification=STRUCTURAL_ONLY`；只有 strict 开启且存在 claims，才额外调用 `judge-main` 做逐 Claim 支持性判定。无 claims 时不会调用严格裁判，也不会因此标为 `SUPPORTED`。

`_validate_strict_verdicts()` 要求返回 verdicts 列表完整覆盖全部 claim ID、每个 ID 恰好一次且状态全部为 `SUPPORTED`。存在 claims 时，空列表、漏判、重复 ID、未知 ID、`UNSUPPORTED` 或 `UNCERTAIN` 都会以 `VERIFICATION_FAILED` 拒绝；校验通过后还会复核来源权限，再标记 `verification=SUPPORTED`。模型裁判可能判断错误，这个标记不是正确性的证明。

在线严格验证检查“每条断言是否被引用资料支持”；离线 [evaluation/judge.py](../backend/app/evaluation/judge.py) 还根据完整答案、金标准事实、问题与历史评估正确性、相关性和题型预期，二者不能混用。回归入口包括 [test_migration_parity.py](../backend/tests/test_migration_parity.py) 的 `test_strict_judge_requires_complete_claim_coverage()`，以及 [审计回归](reviews/2026-09-26-audit/test_audit.py) 的 `test_strict_rag_rejects_empty_judge_verdicts()` / `test_strict_agent_actually_calls_judge()`。

**对应测试：** [test_agent.py](../backend/tests/test_agent.py) 的 `test_agent_single_tool_search_and_answer()`、`test_agent_repeated_calls_termination()`；[test_agent_runtime.py](../backend/tests/test_agent_runtime.py) 的 `test_agent_hitl_checkpoint_and_resume()`。

### 7.6 评估如何复用问答流程

阅读 [evaluation/runner.py](../backend/app/evaluation/runner.py)：

```text
estimate() → start() → 写 EVALUATE job 和全部 PENDING 结果
  → Worker._run_evaluate()
  → EvaluationRunner._execute_run()
      ├─ 检索模式 → RetrievalPipeline.retrieve()
      └─ RAG/AGENT → _run_qa_turns() → 同一会话逐轮 submit_run / get_run
  → _compute_retrieval_metrics()
  → 状态检查 + 可选 judge_answer()
  → 保存单题结果 → aggregate() → export_run()/compare()
```

对本例，金标准应有“幂等键、重试次数上限、补偿日志”三个事实组。只找到一组也可能 Hit@5=1；模型只答出一项不应算完整成功。`judgeMode=HUMAN` 默认保留待复核结果，LLM 模式才进行语义裁判。

指标、API 示例、成功率分母、快照范围、多轮执行与最后一轮评分范围、门禁详见 [RAG 评估完整说明](rag-evaluation-guide.md)。这里优先理解它怎样调用已读过的问答服务，而不是另外实现一套独立 RAG。

## 8. 第二遍阅读：追踪权限、状态和失败

### 8.1 看哪些表，不必先记全部字段

在 [models.py](../backend/app/models.py) 按下面变量名定位表定义：

| 流程 | 优先查看的表变量 | 重点字段 |
| --- | --- | --- |
| 身份 | `app_user_table`、`auth_session_table` | tenant、user、有效状态 |
| 原始资料 | `document_table`、`document_version_table` | 文档状态、版本、active_build_id |
| 解析索引 | `index_build_table`、`source_unit_table`、`chunk_table`、`chunk_span_table` | build、kind、parent_id、来源偏移 |
| 后台执行 | `job_table` | kind、target_id、status、checkpoint、lease_epoch |
| 问答 | `conversation_table`、`context_segment_table`、`qa_run_table` | owner、scope、question、result_json、turn_no |
| 跨轮记忆 | `context_segment_table` | context_payload、context_version、context_through_turn、context_boundary_turn、context_last_run_id |
| 节点恢复 | `job_table` | `checkpoint.langgraph`、lease_epoch（按单次 QA_RUN 保存） |
| 溯源与调用 | `run_source_table`、`run_event_table`、`model_call_table` | 来源关联、阶段事件、调用用途和用量 |
| 评估 | `eval_run_table`、`eval_result_table`、`eval_review_table` | manifest、单题结果、追加复核 |

`run_event` 是程序组装的阶段记录，`model_call` 是模型调用记录，模型返回 JSON 是另一种数据。不要把它们统称为模型“自己输出的完整执行轨迹”。

### 8.2 三条跨模块约束

**权限不是只在入口检查一次。** 入口认身份，检索前限制 build，外发前检查候选，读取历史时检查来源。这些调用散落在多个文件，但沿同一份证据追踪就能理解各自目的。

**任务重试不等于任意重放。** 入库重试会清理未发布 build 的部分产物并重建；新建 QA（统一 Agent）通过 `qa_execution_scope()` 和 `qa_lease_is_current()` 进行 lease fencing，Agent 还能通过 checkpoint 恢复；只有旧 REALTIME 记录还需按原执行/恢复语义处理，不能静默换成 Agent 重放。租约过期时旧执行者不能继续发布，但这些机制也不意味着模型外部调用已获得全局 exactly-once 保证。

**业务终态与工程成功分开看。** 无证据是业务结果，网关不可用是执行故障，格式正确是结构检查通过，答案正确则需要语义或人工评价。

### 8.3 遇到现象应该从哪里看

| 现象 | 建议定位顺序 |
| --- | --- |
| 上传返回了但搜不到 | job 状态 → source_unit/chunk → build READY → active_build_id → ACL → 索引 |
| 检索命中但答非所问 | hit.text → evidence → 模型 messages → claims → 金标准事实 |
| 子块命中，引用却显示一大段 | `_expand_parents()`、matchedText、parentChunkId |
| 新建问答没有继续执行 | `QA_RUN` Job → Worker / Lease → `job.checkpoint.langgraph` → Cancel / 模型服务 |
| 追问没有继承上一轮对象 | `conversation_memory()` → `load_segment_context()` → `context_payload.businessState` → `AgentState.business_state` → 本轮显式 ID |
| 下一轮因历史资料失效而失败 | `context_payload.sourceRuns` → `run_dependency` / HISTORY 来源 → ACL / active build → `SOURCE_CHANGED` |
| 同一轮被重复追加或版本冲突 | `context_last_run_id` / `context_through_turn` → `append_completed_turn()` → `VERSION_CONFLICT` / lease fencing |
| 旧 REALTIME RAG 运行未完成 | 核对旧 `qa_run.mode`、任务清点及历史恢复兼容代码，不能按 Agent 语义恢复 |
| 数据库是 SUCCEEDED，接口是 PARTIAL | `result_json.status` 与 `get_run()` 的合并 |
| 以前能读的答案现在隐藏 | `get_run()` → `_all_sources()` → `can_read_document()` |
| 评估成功率很高但大量题没审核 | judgedCases、judgmentCoverage、最新人工标签 |

## 9. 跑测试读源码：一个可操作的学习路线

### 9.1 先确认测试环境与真实环境的区别

[conftest.py](../backend/tests/conftest.py) 使用内存 SQLite，设置 test 环境并清空真实模型与 Milvus 配置。部分测试还会 monkeypatch 外部调用。这些测试可以观察完整 HTTP 调用和数据库变化，但不等于真实 MySQL、Milvus、Docling 与付费模型的联合验收。

### 9.2 第一组：只看解析和切块

在现有 IDE 的 Python 调试器里给 `parse_and_chunk_document()`、`adapt_docling_hybrid_chunks()` 打断点，运行：

```powershell
# 在仓库根目录打开 Windows 原生 PowerShell；需要已经安装 backend/.venv。
Push-Location -LiteralPath .\backend
try {
    & .\.venv\Scripts\python.exe -m pytest tests/test_ingestion.py::test_document_upload_and_preview -q
    if ($LASTEXITCODE -ne 0) { throw '入库测试失败' }
} finally {
    Pop-Location
}
```

断点要观察：`file_bytes → Docling Hybrid Chunks → units/chunks/spans → child_chunks`。重点看 Docling 返回的 headings/pageNumbers/num_tokens 如何被适配为 SourceUnit、Parent、Child 和 provenance。

### 9.3 第二组：看检索和回答

在 backend 目录运行已有测试：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_enterprise_retrieval.py::test_config_driven_rerank_parent_expansion_and_provenance tests/test_qa.py::test_qa_run_lifecycle_and_evidence -q
if ($LASTEXITCODE -ne 0) { throw '检索与问答测试失败' }
```

统一 Agent 的 `search_knowledge` 工具调用后，检索图断点按顺序放在 `RagRetrievalGraph._resolve_scope()`、`_dense_recall()` / `_bm25_recall()`、`_rrf_fusion()`、`_rerank()`、`_context_budget()`，然后追到 `QaService._execute_run()` 中生成前及 `validate_claims()`。观察 `allowed_build_ids`、`dense_routes` / `sparse_routes`、`hits`、`evidence_map` 和最终 `messages`。独立 Search API 与纯检索评测路径仍可在 `RetrievalPipeline.retrieve()` 设置断点。

运行 pytest 命令本身不会自动启用 IDE 断点；若用命令行交互调试，可在命令中加 `--trace` 从测试入口进入，或用 IDE 的 pytest 调试配置启动。

### 9.4 第三组：看 Agent 和完整用户流程

仍在 backend 目录执行：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_agent.py::test_agent_single_tool_search_and_answer tests/test_agent_runtime.py::test_agent_hitl_checkpoint_and_resume tests/test_system_e2e.py::test_e2e_full_user_and_admin_workflow -q
if ($LASTEXITCODE -ne 0) { throw 'Agent 与系统流程测试失败' }
```

观察 `_model_step()` 返回的 tool_calls，接着看 `_tools_step()` 如何写入 evidence 并追加 tool 消息；再观察审批暂停与恢复。`test_e2e_full_user_and_admin_workflow()` 可用作学习用的客户端脚本，帮助串起认证、资料和问答接口。

### 9.5 第四组：跨轮快照、恢复与权限

仍在 backend 目录执行现有定向回归：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_segment_context.py tests/test_unified_business_agent.py::test_unified_agent_segment_context_version_and_followup tests/test_unified_business_agent.py::test_segment_context_replayed_run_does_not_append_again tests/test_agent_runtime.py::test_agent_hitl_checkpoint_and_resume -q
if ($LASTEXITCODE -ne 0) { throw '会话上下文或恢复测试失败' }
```

建议断点依次放在 `conversation_memory()`、`load_segment_context()`、`LangGraphAgent.run()`、`_finalize_run()`、`append_completed_turn()`。观察第 1/2 轮的 `context_through_turn` 变化、正常追加时不变的 `context_version`、压缩时更新的 `boundaryTurn`、`sourceRuns` 及 `job.checkpoint.langgraph`。补充测试撤权、换用户、重复回放和历史 `E1` 不能冒充当前证据；这些 SQLite/fake model 回归不能代替真实 MySQL 与模型联合验收。

### 9.6 怎样判断自己已经读懂

尝试不看文档回答这些问题，然后回到对应函数验证：

1. 为什么文档入库和新建问答都进入 Durable Job？为什么要同时有 version 和 build？
2. 为什么只索引 CHILD，模型却可能看到 PARENT？
3. 文档撤权后，Milvus 里还有向量为什么也不应该被使用？
4. E1 是在哪里产生的？怎样追到 source_unit？
5. 模型输出的 claim 为什么不能直接当成正确事实？
6. `SUCCEEDED` 与答案 `PARTIAL` 为什么能同时出现？
7. Agent 是谁决定调用工具、谁真正执行工具？
8. 单元测试通过能证明什么，不能证明什么？
9. 评估里 100% 成功率为什么仍可能无法通过门禁？
10. 跨轮 `context_segment` 快照和单次 `job.checkpoint.langgraph` 各什么时候读取、写入？为什么历史 `E1` 不能跨 run 直接复用？

能沿着代码解释这十个问题，说明已经掌握项目主要过程；之后再深入删除传播、租约恢复、模型计费、复杂 ACL 和部署配置。

## 10. 与现有文档如何配合

推荐顺序是：[README 总览](../README.md) → 本源码导读 → [Agent 知识检索工具图说明](rag-langgraph-retrieval.md) → [评估完整说明](rag-evaluation-guide.md) → 按需查阅旧设计中的设计意图。

本导读区分当前实现和验证范围：同步测试不等于真实依赖验收；统一 Agent 使用严格验证，但模型裁判不保证正确；越界引用被拒绝；高级检索策略按配置执行，其效果仍需实验；评测逐轮执行，但只评分最后一轮。阅读历史设计或报告时，应保留这些边界。

文档核对不代替真实依赖端到端运行。后续修改代码时，应一起检查这里的函数链、数据字段和分支说明是否仍然成立。
