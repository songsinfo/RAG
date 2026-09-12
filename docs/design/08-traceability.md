# PRD 验收追踪表

版本：1.0 · 状态：设计覆盖检查，不是测试通过报告。下表每项未来用例 ID 采用 IT-加 AC 编号，例如 IT-01-01；实现阶段在报告中填写真实测试位置、命令与结果。API 集成测试是主入口，数学口径用小型确定性用例补充。

## PRD-00

需求来源：[PRD-00-product-overview](../prd/PRD-00-product-overview.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-00-01 | [01 §5](01-architecture.md) | C1–C5 | 完整上传授权问答引用演示 |
| AC-00-02 | [02 §3–5](02-data-and-access.md) | C1–C5 | 跨入口与跨租户拒绝读取 |
| AC-00-03 | [03 §5–6](03-ingestion.md) | C3 | 新版失败保持旧版且可恢复 |
| AC-00-04 | [02 §5](02-data-and-access.md) | C2 | 撤权与回收后历史隐藏 |
| AC-00-05 | [06 §5](06-evaluation.md) | C4–C5 | 每个策略开关与对照结果 |
| AC-00-06 | [06 §2–4](06-evaluation.md) | C4 | 冻结配置逐题报告含失败 |
| AC-00-07 | [07 §4–5](07-delivery.md) | C5 | 干净部署及实际备份恢复 |

## PRD-01

需求来源：[PRD-01-identity-and-access](../prd/PRD-01-identity-and-access.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-01-01 | [02 §3–5](02-data-and-access.md) | C1–C3 | 精确标题/图/预览已知 ID 不泄露 |
| AC-01-02 | [02 §1–3](02-data-and-access.md) | C1 | 伪造租户请求与跨租户外键失败 |
| AC-01-03 | [02 §3–4](02-data-and-access.md) | C1 | 移除组成员后全部新读失效 |
| AC-01-04 | [02 §3](02-data-and-access.md) | C1 | 文档允许不突破 KB 范围 |
| AC-01-05 | [05 §2](05-api-conversation-agent.md) | C1–C4 | 普通用户直接调用维护 API 被拒 |
| AC-01-06 | [02 §5](02-data-and-access.md) | C2 | 未展示引用的输入被撤权也隐藏答案 |
| AC-01-07 | [02 §3/5](02-data-and-access.md) | C1–C4 | 管理员元数据权限不授予正文 |
| AC-01-08 | [02 §4](02-data-and-access.md) | C2 | 请求体提交前后撤权竞态 |
| AC-01-09 | [05 §3](05-api-conversation-agent.md) | C2–C3 | 旧引用固定版本并标注非当前 |

## PRD-02

需求来源：[PRD-02-document-lifecycle](../prd/PRD-02-document-lifecycle.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-02-01 | [03 §5–6](03-ingestion.md) | C1 | 上传返回任务且不提前声称入库 |
| AC-02-02 | [03 §3–4](03-ingestion.md) | C3 | 条件例外与父子来源可恢复 |
| AC-02-03 | [03 §1/3/4](03-ingestion.md) | C3 | 扫描/图/表分别抽检与定位 |
| AC-02-04 | [03 §3](03-ingestion.md) | C3 | DOCX 无页号使用真实段落定位 |
| AC-02-05 | [03 §5](03-ingestion.md) | C3 | 关键页告警不自动发布 |
| AC-02-06 | [03 §5–6](03-ingestion.md) | C1/C3 | 任务重启幂等与 ES 写入恢复 |
| AC-02-07 | [03 §4–5](03-ingestion.md) | C3 | 缺问题表示不能通过完整构建 |
| AC-02-08 | [03 §5](03-ingestion.md) | C3 | 较旧完成不覆盖新生效版本 |
| AC-02-09 | [04 §4–6](04-retrieval-and-evidence.md) | C3 | 摘要/问题命中回原文引用 |
| AC-02-10 | [03 §5/7](03-ingestion.md) | C3 | 删除 epoch 阻止迟到发布 |
| AC-02-11 | [03 §7](03-ingestion.md) | C3 | 恢复检查 ACL 及索引并原子切状态 |
| AC-02-12 | [07 §5](07-delivery.md) | C3/C5 | 全部内容副本清理及备份状态 |
| AC-02-13 | [03 §2](03-ingestion.md) | C3 | ZIP 路径/外链/目录越界拒绝 |

## PRD-03

需求来源：[PRD-03-retrieval-answer-evidence](../prd/PRD-03-retrieval-answer-evidence.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-03-01 | [06 §4–5](06-evaluation.md) | C2 | 错误码及同义问题三检索基线 |
| AC-03-02 | [04 §1–2](04-retrieval-and-evidence.md) | C4 | 查询增强开关及调用/查询记录 |
| AC-03-03 | [04 §4](04-retrieval-and-evidence.md) | C3 | 同路多表示只计一个 canonical |
| AC-03-04 | [04 §4](04-retrieval-and-evidence.md) | C4 | MMR 分数归一化与选择代价 |
| AC-03-05 | [04 §5](04-retrieval-and-evidence.md) | C3 | 父扩展去重/预算/实际来源一致 |
| AC-03-06 | [04 §6](04-retrieval-and-evidence.md) | C2 | 未知 evidence/额外事实不能通过 |
| AC-03-07 | [06 §4/6](06-evaluation.md) | C4 | 支持/不支持/未知与人工复核 |
| AC-03-08 | [04 §3/6](04-retrieval-and-evidence.md) | C2 | 空授权证据固定拒答且不透露存在 |
| AC-03-09 | [04 §6–7](04-retrieval-and-evidence.md) | C2/C4 | 冲突双来源与条件缺口 |
| AC-03-10 | [04 §2/6](04-retrieval-and-evidence.md) | C4 | HyDE 假想事实不能作为 evidence |
| AC-03-11 | [04 §4/7](04-retrieval-and-evidence.md) | C2 | 可配置重排降级及权限失败关闭 |
| AC-03-12 | [04 §6](04-retrieval-and-evidence.md) | C4 | 严格验证失败不提前显示正文 |

## PRD-04

需求来源：[PRD-04-conversations-feedback](../prd/PRD-04-conversations-feedback.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-04-01 | [05 §2/5](05-api-conversation-agent.md) | C2 | 猜会话 ID 不能读/清除/取消别人资源 |
| AC-04-02 | [05 §5](05-api-conversation-agent.md) | C2 | 重启保留已完成合法历史 |
| AC-04-03 | [05 §5](05-api-conversation-agent.md) | C2 | 切范围新段不传旧助手上下文 |
| AC-04-04 | [02 §5](02-data-and-access.md) | C2 | 撤权隐藏本条与后续派生回答 |
| AC-04-05 | [01 §5](01-architecture.md) | C2/C3 | 历史旧版可见但追问检索当前 |
| AC-04-06 | [05 §5](05-api-conversation-agent.md) | C2 | 取消先提交则迟到结果不复活 |
| AC-04-07 | [05 §5](05-api-conversation-agent.md) | C2 | 幂等及活动唯一约束下无双回复 |
| AC-04-08 | [05 §2](05-api-conversation-agent.md) | C4 | 反馈定位运行且详情仍授权 |
| AC-04-09 | [06 §1/6](06-evaluation.md) | C4 | 反馈未经核验不修改冻结标准 |

## PRD-05

需求来源：[PRD-05-evaluation-experiments](../prd/PRD-05-evaluation-experiments.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-05-01 | [06 §1/4](06-evaluation.md) | C1/C4 | 一组三事实命中区分 Hit 与 Recall |
| AC-05-02 | [06 §1–2](06-evaluation.md) | C4 | 冻结后修改创建新版本 |
| AC-05-03 | [06 §2](06-evaluation.md) | C3/C4 | 不同 chunk 映射相同原文锚点 |
| AC-05-04 | [04 §1](04-retrieval-and-evidence.md) | C3/C4 | 表示能力缺失拒绝实验 |
| AC-05-05 | [06 §3–4](06-evaluation.md) | C4 | 超时失败不从计划分母消失 |
| AC-05-06 | [06 §4/6](06-evaluation.md) | C4 | 相关/引用合法/语义支持分离 |
| AC-05-07 | [06 §2](06-evaluation.md) | C4 | 管理员启动运营 persona 仍受限 |
| AC-05-08 | [07 §3](07-delivery.md) | C4 | 评估预算耗尽保留未完成 |
| AC-05-09 | [06 §5–6](06-evaluation.md) | C4 | 同快照逐题配对质量/成本 |
| AC-05-10 | [06 §2/7](06-evaluation.md) | C4 | 评测详情与导出当前权限复核 |
| AC-05-11 | [06 §4](06-evaluation.md) | C4 | 全拒答不因零分母拿满分 |
| AC-05-12 | [06 §6](06-evaluation.md) | C4 | 文档注入裁判指令仍作为数据 |

## PRD-06

需求来源：[PRD-06-operations-delivery](../prd/PRD-06-operations-delivery.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-06-01 | [05 §2–4](05-api-conversation-agent.md) | C2 | 失败 trace 定位阶段/配置/降级 |
| AC-06-02 | [03 §6](03-ingestion.md) | C2 | 429 有限重试且累计用量 |
| AC-06-03 | [07 §3](07-delivery.md) | C2/C4 | 并发共享预算预留与未知费用 |
| AC-06-04 | [07 §3](07-delivery.md) | C2 | 真实服务故障不替换 Mock |
| AC-06-05 | [03 §6](03-ingestion.md) | C1/C4 | 持久入库/评估恢复及幂等 |
| AC-06-06 | [07 §5](07-delivery.md) | C2–C5 | 日志无密钥正文且详情权限守卫 |
| AC-06-07 | [07 §5](07-delivery.md) | C5 | 旧备份恢复先重放删除清单 |
| AC-06-08 | [07 §4](07-delivery.md) | C1 | 未隔离测试启动被阻断 |
| AC-06-09 | [07 §1/4/5](07-delivery.md) | C5 | 空白环境无手工改库完整演示 |
| AC-06-10 | [06 §4](06-evaluation.md) | C5 | 搜索/问答吞吐与失败超时分别报 |

## PRD-07

需求来源：[PRD-07-agent-deep-qa](../prd/PRD-07-agent-deep-qa.md)。

| 验收项 | 设计落点 | 实现阶段 | 未来验证场景 |
|---|---|---|---|
| AC-07-01 | [05 §6](05-api-conversation-agent.md) | C5 | 中间实体两步搜索与普通配对 |
| AC-07-02 | [05 §6](05-api-conversation-agent.md) | C5 | 未知工具/非法参数/越权不执行 |
| AC-07-03 | [07 §3](07-delivery.md) | C5 | 工具增强计入同一全局预算 |
| AC-07-04 | [05 §6](05-api-conversation-agent.md) | C5 | 重复规范参数第三次终止 |
| AC-07-05 | [04 §6](04-retrieval-and-evidence.md) | C5 | 自然停止仍校验伪造引用 |
| AC-07-06 | [05 §5–6](05-api-conversation-agent.md) | C5 | 取消后不执行下一工具与回复 |
| AC-07-07 | [05 §6](05-api-conversation-agent.md) | C5 | 版本变化或撤权中止读取生成 |
| AC-07-08 | [05 §6](05-api-conversation-agent.md) | C5 | 耗尽预算无额外总结调用 |
| AC-07-09 | [06 §5](06-evaluation.md) | C5 | 简单/多跳题质量费用终止分布 |

## 当前验证边界

已检查验收编号覆盖与本地链接。SQL 只做表/列/外键和约束名结构核对，JSON 只做语法及关键字段检查；未启动 MySQL/ES/Docling，未进行真实模型调用，未执行上述工程验收。任何 C 阶段均尚未完成。

