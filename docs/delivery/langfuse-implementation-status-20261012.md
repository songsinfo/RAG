# Langfuse 实施记录（2026-10-12）

对应规范：`docs/design/11-langfuse-observability-spec.md`。

## 已提交但尚未完成实际验收

- 增加 `langfuse==4.16.0`，实现可选 SDK 初始化、OTel provider 复用、API 和独立 Worker 生命周期、超时退出配置。
- 默认 `RAG_LANGFUSE_ENABLED=false`；缺密钥时禁用 Langfuse；导出限 `rag.agent` 和手动 SDK generation；业务 ID 以版本化环境域 HMAC 表示。
- QA Worker 每个 lease 批次生成 trace，追加 `OBSERVABILITY_TRACE`（含真正 traceId / 是否采样 / job attempt / lease epoch / 目标实例），复用原有事件 fencing；恢复后的旧批次事件保留。
- 模型外发在 `invoke_tracked` 建立手动 generation，每次重试及 fallback 分配独立 call_id；provider usage 的未知/0 区分仍遵循原有实现。
- 添加只依赖本地逻辑的基础回归测试。

## 未实现 / 未验证（不得视为满足 LF-AC01～10）

- 尚未针对 SDK 4.16.0 运行安装后的兼容性/真实序列化载荷测试；现有实现不能证明 SDK 产生的内部 attributes、events 和资源字段完全符合白名单；生产环境不可直接开启。
- 尚未完成 `rag_graph` 全阶段独立 span、并行 context 显式传播、受控评估的候选排名与真实消息区间快照（LF-T04）。
- 尚未完成已存自动/人工评分的显式幂等同步（LF-T05），因此无 Langfuse 评分收件证据。
- `SYNTHETIC_EVAL` 内容模式禁止使用，当前仅实现元数据模式。
- 未部署私有 Langfuse 服务及其 ClickHouse/Postgres/对象存储；缺服务器镜像版本、权限隔离、收件 trace 实例、负载与故障 P50/P95 对照（LF-T06）。
- 未运行线上模型或调用付费供应商；不能声称兼容性、性能、安全隐私端到端验收已通过。

## 开发验证

```bash
cd backend
python -m pip install -e .
pytest -q tests/test_langfuse_observability.py
```

优先补齐对 SDK exporter 的 fake-exporter 载荷检查及故障隔离，再在合成私有环境启用；
禁止用真实公司数据、prompt、工具 payload 进行联调。

开启需由管理员注入专用 Secret（不能和业务密钥复用），设置
`LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、
`RAG_LANGFUSE_ID_HMAC_KEY`（>=32 bytes）、`RAG_LANGFUSE_TARGET_ID`、
`RAG_LANGFUSE_SAMPLE_RATE` 及 `RAG_LANGFUSE_ENABLED=true`。
回滚仅将 `RAG_LANGFUSE_ENABLED=false` 并重启各进程；旧 Langfuse 内容不自动删除。
