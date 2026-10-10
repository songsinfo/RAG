# RAG Python Backend (FastAPI + Milvus + LiteLLM)

企业内部知识库 RAG 系统的 Python 3.12 模块化单体后端。

## 技术栈与架构

- **Web 框架**: FastAPI / Starlette, Uvicorn, Pydantic
- **数据存储**: MySQL 8.4 (关系数据 / 权限 / 生命周期 / 任务审计), SQLAlchemy 2.0
- **向量检索与混合检索**: Milvus 2.5 (Dense Vector + Sparse BM25 / RRF 混合融合)
- **文档解析**: Docling 独立服务
- **大模型网关**: LiteLLM AI Gateway (持有真实供应商 API 密钥，对外统一提供 OpenAI 兼容端点)
- **问答与编排**: LangGraph 统一 Agent 主循环与嵌套检索图，支持文档/模拟工具双来源、严格校验和 Durable Job（PR #14 已合并 main，生产联合验收仍需单独进行）

## 模块划分

```
backend/app/
├── config.py             # 配置加载与校验
├── db.py                 # 数据库引擎与连接管理
├── errors.py             # 统一异常处理与敏感信息脱敏
├── models.py             # SQLAlchemy Core 表模型 (与生产 MySQL 模式 100% 兼容)
├── main.py               # FastAPI 应用入口与中间件
├── identity/             # 身份认证、租户隔离、CSRF 与管理接口
├── knowledge/            # 文档解析、入库、版本生命周期与审计
├── retrieval/            # Milvus 混合检索与 RRF 融合
├── qa/                   # 问答服务、LangGraph Agent、断言验证与模型网关
└── evaluation/           # 评测体系与消融实验指标计算
```

## 统一 Agent 代码与运行边界（PR #14）

- `app/qa/agent.py`：外层 Agent `model → tools → model`，由 `JobCheckpointSaver` 持久化状态；`search_knowledge` 委托 `RagRetrievalGraph.retrieve()`，`read_section` 从可读 SourceUnit 获取文档原文。
- `app/qa/business_tools.py`：`diagnose_coupon(user_id,coupon_id)`、`get_final_price_rules()`、`diagnose_product_group(user_id,product_group_id)`、`diagnose_subsidy(user_id,activity_id)`。**均为合成 fixture，不访问 Java 或真实业务系统**。缺必填 ID 不能编造，模拟失败条件不是实际资格计算结果。
- `app/models.py` 的 `run_source` 使用 DOCUMENT / TOOL 来源类型；`TOOL` 保存结构化快照且无 `document_id/build_id`，文档依然逐次执行 ACL/build 复核，工具来源不可冒充文档引用。
- `QaService.submit_run()` 将新建问答规范化为 `mode=AGENT`、`executionMode=DURABLE`；历史旧运行按原模式读取，未排空的旧 REALTIME 分支保留，不能用新逻辑重放旧排队任务。
- `app/qa/segment_context.py` 负责跨 run 的 `context_payload`、版本、压缩边界和来源权限复核；`app/qa/business_context.py` 保存显式业务对象与带来源的模拟工具引用。与单次 Agent 的 `app/qa/job_checkpointer.py`（写入 `job.checkpoint.langgraph`）分离，不能把历史 E1 当成本轮证据。
- `app/qa/runtime.py` 当前在 Agent run 内进行完整轮次裁剪与硬上限检查；`AGENT_MODEL_CONTEXT_TOKENS` 默认按保守 8192 窗口估算，**不是供应商实际上下文容量保证**。跨提问的授权上下文快照/压缩版本已经通过 `context_segment` 持久化；真实供应商 KV Cache 命中和收益仍未实测。

详见[统一业务 Agent 规格](../docs/design/10-unified-business-agent-spec.md)与[当前实现状态](../docs/delivery/unified-business-agent-implementation-status.md)。

## 运行日志

API 与独立 Worker 使用统一的 JSON 行日志，默认级别为 `INFO`，可用 `LOG_LEVEL` 调整。
从项目根目录通过 `scripts/Start-Backend.ps1 -Environment demo` 启动时，同时输出到终端与
`.runtime/demo/logs/backend.log`；文件达到 10 MiB 自动轮转，最多保留 5 个备份。
直接启动 Uvicorn 或独立 Worker 时，可设置 `RAG_LOG_FILE` 指定日志文件，否则仅输出到终端。
多个进程应使用不同日志文件；容器部署可只输出到终端，由平台集中采集与管理保留期限。

在项目根目录查看实时日志：

```powershell
Get-Content -LiteralPath .runtime/demo/logs/backend.log -Tail 50 -Wait
```

用一次问答的 `runId` 筛选日志：

```powershell
Select-String -LiteralPath .runtime/demo/logs/backend.log -SimpleMatch '替换为你的 runId'
```

请求日志包含 `request_id`、HTTP 状态与耗时，响应头 `X-Request-ID` 可用于定位提交失败。
`run_submitted` 关联请求、`run_id` 与 `job_id`；Worker 日志记录领取、结束及重试。
`run_stage` / `ingest_stage` 的 `duration_ms` 表示上一阶段耗时，并通过 `previous_stage` 标识；
`job_finished` 和模型调用日志的 `duration_ms` 分别表示一次任务尝试和一次模型调用的耗时。
模型日志包含别名、用途与 Token 用量；`usage_reported=false` 表示用量为估算值。
日志不记录请求正文、提示词、文档正文、模型输出和 Cookie。异常保留类型与堆栈位置，
省略可能携带 SQL 参数或模型内容的原始异常消息；环境中的密钥与密码值会被脱敏。
现有数据库 `run_event` 与网页的受控 Trace 保持原有功能；`run_id` 是业务运行 ID，并非分布式 `traceId`。

## 本地开发与运行

已有 MySQL 数据库若 `job.kind` 约束尚未允许 `QA_RUN`，需先应用
`docs/design/migrations/20260921_add_qa_run_job_kind.sql`。已有数据库升级到 PR #14 后，需要备份并按实际迁移记录应用 `docs/design/migrations/20261010_add_tool_run_sources.sql` 与 `docs/design/migrations/20261010_segment_context_snapshot.sql`；新数据库使用当前 `schema.sql`。旧程序回滚时不得删除已保存的 TOOL 快照、Segment 上下文列，也不得恢复 source 列的非空约束。
Embedding 批量由 `EMBEDDING_BATCH_SIZE` 控制，配置模板设为 20，以适配当前供应商上限。
Docling 的页数限制为 400；实际处理能力仍取决于文档复杂度和容器内存。

### 1. 虚拟环境与依赖
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
```

### 2. 运行服务
```powershell
$env:PYTHONPATH = "$PWD"
.\.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 3. 执行测试
```powershell
$env:PYTHONPATH = "$PWD"
.\.venv\Scripts\pytest -v
```
