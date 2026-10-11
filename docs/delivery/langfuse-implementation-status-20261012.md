# Langfuse 首期实施与合成验收（2026-10-12）

对应 [Spec](../design/11-langfuse-observability-spec.md) 和 [Tickets](langfuse-observability-tickets.md)。LF-T01～06 已实现并通过本地合成验收；EOBS-01～03 保持后续任务。默认关闭，仅支持 METADATA_ONLY，没有引入 Ragas、托管 Judge 或额外在线模型调用。

## 实现范围

- API、内嵌和独立 Worker 复用幂等初始化入口及 OTel provider。缺配置禁用；观测故障不捕获或重放业务异常。退出由有界 daemon 执行 SDK shutdown，先注销 SDK 的阻塞 atexit hook；应用创建的 provider 不另注册阻塞退出 hook。
- QA 每个物理 lease 创建独立 root，HITL 等待结束当前批次，恢复生成新 trace。本地 OBSERVABILITY_TRACE 沿用租约 fencing，保留历史批次；评分只取最新有效批次，未采样不回退旧 trace。标识使用独立、版本化、环境和租户隔离的 HMAC。
- 实际 invoke_tracked 外发对应一条 generation，重试/fallback 独立。usage 来源、缺缓存/真实零、结算未知明确标记；CNY 使用已有账本结算值的 metadata，不写未经核对的原生 cost。
- 检索图显式传播线程 context；QA 图及纯检索评估保存分路、融合、重排、MMR、parent、预算及最终候选的有序私有诊断。空阶段也保留，超限标记不完整。
- 外发前另存 MODEL_CONTEXT：描述实际发送的结构化证据、消息组和保留区间。已知 guardrail 包装可映射；任意变换、重复匹配、历史无法归因时保持 UNKNOWN，整个历史 complete=false。制品不上传 Langfuse，读取重新校验管理员、persona、ACL、build 和 epoch。
- 显式同步已有自动/人工评分：稳定 ID 支持重跑，人工 revision 分开，null 不补零。ENQUEUED_NOT_CONFIRMED 只表示 SDK 入队，不表示服务器确认。

## 锁定的本地私有环境

部署文件：[langfuse.compose.yml](../../deploy/langfuse.compose.yml)。独立项目、数据库、ClickHouse、Redis、对象存储和新生成凭据。web 127.0.0.1:3031，对象存储 127.0.0.1:9092，其余服务不发布主机端口。禁用公开注册、服务遥测和批量导出；容器日志轮转 10 MiB × 3。开发数据保留在独立 named volumes，停机不删除；本配置不定义生产保留期。

| 组件 | 验证版本 / 镜像摘要 |
| --- | --- |
| Python / Langfuse SDK | 3.12.10 / 4.16.0；requirements 与 pyproject 同步 |
| Langfuse web | health 返回 4.56.0；b1948c7c0931f277975b985e30e73536b1bca5822705ddd9a89ba7c1ed79b3ee |
| Langfuse worker | 3f7571ff3a725ebdcdf4c35e06bdf8c3f8325770691c673793ad61b328265e13 |
| ClickHouse 25.12.11.4 | 8a790dd3468db22b1d4e7b18a176f378ff5ff6053b9c48dd4ea1fa71a24c5ba6 |
| PostgreSQL 17.6 | 00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929 |
| Redis 7.4.7 | 976ab0f0939b78c06d802bcd3bea44c5142ea9a756a57b861e08819817f768c8 |
| Chainguard MinIO | 7df55d002b698e4494a131b848dd553361bbe58b7e19f62d9623c29af1ba9ccf |

配置参考 [官方 Docker Compose](https://langfuse.com/self-hosting/deployment/docker-compose)。本次 v4 服务的收件检查使用 SDK api.observations.get_many / api.scores_v3.get_many_v3；不使用该实例已停用的旧 /api/public/traces 路径，见 [迁移说明](https://langfuse.com/faq/all/deprecated-api-migration)。

## 验收证据

机器可读记录：[合成验收结果](langfuse-synthetic-acceptance-20261012.json)。凭据、原始正文、模型消息和私有 artifact 没有提交。

| 验收 | 直接证据 |
| --- | --- |
| LF-AC01 | 初始化测试：关闭/缺配置不建 Langfuse processor，重复调用只追加一个；API/独立 Worker 使用同入口；完整后端回归 |
| LF-AC02 | 私有真实 Worker 正常运行、HITL 等待/恢复两条不同 trace/递增 epoch；tenant/attempt 批次测试；已有恢复与 late-worker fencing 回归 |
| LF-AC03 | tracked 合成主模型两次失败、fallback 成功：3 次发送、3 个 generation、3 条独立账本；私有 Worker 正常题 2 次调用/2 个 generation |
| LF-AC04 | 真实 SDK → HTTP OTLP receiver 解压/protobuf 检查：正文、密钥、路径、标题、原始 ID、异常/SQL canary 不出现；不合规观测丢弃，合规父子保留 |
| LF-AC05 | 24 路并发 retrieval / 48 个并行子节点不串父节点；私有服务收到 Agent/检索/模型观测 |
| LF-AC06 | 0/缺失 cache normalization、provider usage、微小 CNY 结算 metadata；服务 generation input=30/output=10；未知不补零 |
| LF-AC07 | 重排丢证据、预算丢证据两个 pipeline 合成反例；关闭/开启/诊断异常的排名一致；外发片段、guardrail prefix、parent、歧义映射及超限测试 |
| LF-AC08 | 4 条自动/人工 score 重复入队后服务仍为 4 个 ID；两次人工 revision 和 UNDETERMINED 保留，不发送 null；persona 停用跳过、ACL 撤销拒绝，SDK 失败不改 metrics |
| LF-AC09 | SDK queue/timeout/setup fault、业务异常只执行一次、有界关闭；诊断 I/O 期间取消后不发送；实际停止 web/worker，同题仍 COMPLETED/2 次发送，0.214 秒返回关闭等待；服务已恢复 |
| LF-AC10 | 精确摘要、独立存储/凭据、回环端口、禁公开注册；仅元数据，没有内容模式启用入口 |

本地后端 205 passed / 1 skipped，历史审查回归 32 passed；私有收件及停机测试各 1 passed，普通 CI 跳过 opt-in 私有测试。编译与 diff 检查通过。最终 CI 以 PR 当前 head 为准。

## 性能对照的范围

Windows 原生 Python 3.12，SQLite 内存数据库、合成模型响应、本地 Langfuse。相同问题串行，交替开/关顺序，预热一对后每组 12 条，采样率 1；每条答案/状态一致，均恰好两次 tracked 调用。耗时包含本地诊断，开启组不等待服务收件；两组都启用 tracemalloc。

| 开关 | 样本 / 并发 | P50 ms | P95 ms | 最大 Python 分配峰值 bytes |
| --- | --- | --- | --- | --- |
| 关闭 | 12 / 1 | 643.57 | 765.65 | 456763 |
| 开启 | 12 / 1 | 633.61 | 788.70 | 503474 |

小样本波动不能证明开启更快，也不能推出零开销。分配峰值不是进程 RSS、容器资源或生产容量；并发父子测试是 context 隔离测试，不是负载基准。没有部署 SLO，本次不声称性能达标。

## 使用与边界

初始化、启动、验证与回滚见 [backend README](../../backend/README.md#langfuse-可选元数据观测)。验收只替换供应商响应，真实执行 Worker/权限/预算/Agent/检索/诊断/评分/SDK/网络和私有服务；没有调用付费模型或真实业务工具。生产 MySQL + Milvus + 供应商联合压测、保留期与访问成员由部署验收确定。

SYNTHETIC_EVAL 正文上传没有实现；设置 DATA_MODE 不会开启正文。完整历史及任意压缩内容无法恢复精确 provenance，保持未知；阶段覆盖指标和独立断言 Judge 仍属 EOBS 后续。停机可能丢观测，没有 outbox 或保证送达。Langfuse 采样统计不能替代 MySQL 完整报告、预算与门禁。
