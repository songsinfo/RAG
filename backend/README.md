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

### EOBS 评估增强与独立断言实验

评估结果增加私有 `judge.evaluationSample`（eobs-v1）、指标状态 `metrics.metricDiagnostics`、阶段覆盖 `metrics.stageCoverage`；聚合增加 `metricCoverage`。区分 canonical 候选、检索输出与实际发送的结构化文档区间；未知、失败和空 Gold 不补零。旧 correctness、成功率、v1/v2 Judge 与门禁保持原义。

独立断言实验为显式管理员动作：`POST /api/v1/admin/eval/runs/{id}/assertion-diagnostics`，提交 resultIds、正数 maxCostCny 和 Idempotency-Key。仅 HUMAN_REVIEWED 且来源当前可读的完成问答可执行。独立 EVALUATE job 两阶段调用模型，第一阶段不接收原 claims；不默认增加在线模型调用，不改原评估结果。

读取 `GET /api/v1/admin/eval/assertion-diagnostics/{jobId}`，逐断言人工标签通过对应 `/calibration` POST 提交，保留人工修订及真实比较分母。Judge 等待上限为 60 秒，调用 deadline 仍优先限制；超时用量保留未知。

本次 30 条合成候选已获用户核对确认，一个真实漏提反例完成 pilot；没有充分校准或生产收益证明。[实施、API 与付费验收边界](../docs/delivery/evaluation-observability-implementation-20261012.md)包含完整证据及已发生失败，不将供应商账单未知当零。

### Langfuse 可选元数据观测

默认关闭。开启后，QA 的每个 Worker lease 是一条独立 trace；恢复/HITL 重新创建，原始 run ID 与关联事件保留在应用数据库。只导出白名单元数据和 HMAC 标识，模型消息、正文、工具参数及诊断制品不上传。已有 MySQL 权限、预算、评估报告与门禁仍是权威。

从仓库根目录启动独立合成实例（Docker Desktop Linux containers；PowerShell）：

```powershell
pwsh -File scripts/init_langfuse_local.ps1
docker compose --env-file .local/langfuse-eval.env -f deploy/langfuse.compose.yml up -d
```

初始化脚本生成新的专用凭据到忽略的 `.local/langfuse-eval.env`，已有文件会拒绝覆盖。等 `http://127.0.0.1:3031/api/public/health` 返回 OK 后，可访问 `http://127.0.0.1:3031`；唯一初始维护用户是 `synthetic@localhost.invalid`，密码为该私有文件的 `LFD_USER_PASSWORD`。不允许公开注册，数据库等端口只在容器网络可见。安装后端 requirements 到 `backend/.venv` 后运行：

```powershell
pwsh -File scripts/check_langfuse_local.ps1
pwsh -File scripts/check_langfuse_local.ps1 -Outage
```

验收运行在 child process，替换供应商响应为合成夹具，真实经过 Worker/预算/Agent/检索/SDK/私有服务；不调用付费模型。第一条检查收件、评分幂等、撤权、HITL 和 12 对开关性能，输出 `.local/langfuse-receipt.json`；第二条暂停该独立实例的 web/worker，检查业务完成及有界退出，输出 `.local/langfuse-outage.json`，finally 恢复服务。普通 CI 跳过此私有测试。合成验收不是生产供应商或 MySQL/Milvus 联合压测。

应用 API 与独立 Worker 均需注入 `RAG_LANGFUSE_ENABLED=true`、`LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY`、至少 32 bytes 的专用 `RAG_LANGFUSE_ID_HMAC_KEY`、`RAG_LANGFUSE_TARGET_ID`、采样率及环境名。凭据只能注入后端进程；用 `.env.example` 核对变量，不提交密钥。首期仅支持 METADATA_ONLY，设置其他 DATA_MODE 也不会打开正文上传。切换配置需重启相应进程。

管理员可显式调用 `POST /api/v1/admin/eval/runs/{id}/langfuse-scores` 同步已有评分，计数为 SDK 入队，确认收件要查询目标实例。管理员经 `GET /api/v1/admin/eval/qa-runs/{id}/retrieval-diagnostics` 读取本地评估诊断，读取时仍检查实际 persona 和来源。实际上下文里的未知历史、歧义/转换区间不会被标为精确完整。

回滚设置 `RAG_LANGFUSE_ENABLED=false` 并重启 API/Worker；停止可选实例：

```powershell
docker compose --env-file .local/langfuse-eval.env -f deploy/langfuse.compose.yml down
```

该命令保留独立 volumes 和旧数据。旧 Langfuse 数据不会随应用撤权或开关自动删除；生产保留期、访问成员与容量需另行配置。锁定镜像、逐项证据及已知限制见[首期实施记录](../docs/delivery/langfuse-implementation-status-20261012.md)。

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
