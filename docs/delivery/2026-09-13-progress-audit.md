# 企业知识库当前进度审计

审计日期：2026-09-13。结论：主体功能代码已完成，工程验收尚未完成。下面只记录本次实际执行得到的证据，不把设计追踪、Mock 测试或数据文件存在等同于真实环境通过。

## 已验证

| 范围 | 本次结果 | 证据边界 |
| --- | --- | --- |
| Java 后端 | Maven test 成功，60 个测试，0 失败、0 错误、0 跳过 | 覆盖算法、权限、CSRF、解析、删除传播、预算、模型 HTTP 契约、问答生命周期等；未连接真实 MySQL/ES |
| 前端 | 4 个 API 契约测试通过；vue-tsc --noEmit 通过；Vite 生产构建通过 | 构建产物约 1.04 MB，存在大 chunk 警告，不影响功能验收 |
| PRD/设计 | 23 份 Markdown、125 个本地链接、81/81 AC 有追踪；DDL 39 张表、99 个外键 | 这是静态一致性检查，不是 81 项运行验收 |
| 验收脚本 | evaluation 真实无模型 HTTP 集成通过；端到端和消融脚本 SelfTest 通过；300 题结构校验和运行 ID 绑定合成测试通过 | HTTP 实测覆盖登录、入库、BM25、双层 ACL 和撤权；B0-B3 真实模型尚未运行 |
| 多环境配置 | evaluation 的 MySQL 8.4 与 Elasticsearch 容器健康，Flyway v1-v2 迁移成功；三个环境的 Compose config 均解析通过 | evaluation 基础依赖已实测；Docling 首次镜像拉取尚未完成 |
| 300 题数据 | 每份 300 条、caseKey 无重复、DEV 180 / TEST 120、9 类题型数量正确、18 个来源引用 | 两份文件内容完全相同；当前是离线模板，不是可直接导入的运行题集 |
| 评估真实链路 | 一题合成数据完成 Dataset 冻结、Snapshot、RETRIEVAL_ONLY 和汇总 | completionCoverage=1，Hit@5/Recall@5/MRR/nDCG@5 均为 1，modelCalls=0；只证明链路，不代表 300 题质量 |

本次修复了几项会直接影响可信度的问题：

- 评估批次预算现在关联所有多轮问答和嵌套模型调用，并通过任务行锁串行核算；同时修复并发相同幂等键可能被误判为活动请求的问题。
- 父段扩展按与上下文一致的 UTF-8 上界核算，避免中文父段绕过预算。
- 评测金标准与快照记录真实环境来源，删除传播兼容历史本地来源并保留远端边界。
- Docling 轮询使用持久化总时限，任务丢失或进程重启不会刷新等待窗口；显式重试才会重置。
- 长表按完整行分组并重复表头，单行超限直接失败，避免静默发布不可控大块。
- Markdown 解析统一 CRLF/CR 换行；回答输出 token 上界扩到 2400。
- 端到端脚本已移除模型配置写入和“catch 后当成功”的逻辑，只验证无模型入库、BM25、ACL 授予/撤销与 demo 评估隔离。
- nDCG 已从独立数学函数接入逐题检索指标与宏平均汇总，相关等级取金标准 relevanceGrade，同一事实组只计首次命中。
- 已增加固定 B0-B3 配置、300 题运行时 ID 绑定和批跑脚本；脚本先估价，失败关闭，并只落盘脱敏逐题结果。

## 当前阻塞

Docker Server 29.7.2 已恢复。evaluation 的 MySQL、Elasticsearch、后端和无模型 HTTP 链路已运行通过，结果保存在本机 .runtime/reports/http-integration.json。仍未完成：

- Docling CPU 镜像首次拉取未完成，因此 PDF/DOCX、图片和表格的真实解析尚未验收；
- 18 个授权来源尚未导入 evaluation 环境，本地绑定模板中 18 个 documentId/versionId 仍为空；
- 模型服务按当前约定尚未配置，因此没有真实 Chat、Embedding、Reranker、LLM Judge 调用和费用；
- 300 题 B0-B3 配对实验尚未执行，不能从一题链路冒烟结果推断质量提升。

## 评测集状态

本地 300 题文件使用 operator 作为逻辑 persona，以仓库相对路径作为来源引用，并用 ver-01 表示逻辑版本。后端导入要求隔离环境中真实存在的登录账号、UUID documentId 和 UUID versionId，所以必须先导入已授权资料并做运行时绑定。可以用下面的命令检查结构：

    & .\scripts\Test-EvaluationDataset.ps1 -Path .\datasets\eval_accelerator_300.jsonl -ExpectedCount 300

输出中的 runtimeBindingsReady=false 是预期提示，表示结构有效但尚未绑定。公司知识衍生题集和生成脚本已加入 .gitignore，文件仍保留在本机；不要把正文、原文引句或密钥提交到公开简历仓库。

## 后续执行顺序

evaluation 的基础依赖、后端迁移和无模型 HTTP 集成已经完成。下一步续传 Docling 镜像，导入确认可用于评测的 18 个来源；本地 .runtime/evaluation-bindings.json 已生成并自动填入 operator 到 ops 的映射。

来源导入完成后，按照 [B0-B3 检索消融实验运行手册](2026-09-13-retrieval-ablation-runbook.md) 填入 18 组运行 ID、配置模型服务并执行四组配对批跑。脚本已经具备，真实消融报告仍依赖这些外部输入。

## 当前可用于简历的表述边界

可以写已实现的架构和机制，例如 Java/Spring Boot 企业知识库、文档级 ACL、结构化解析与父子分块、混合检索配置、证据追溯、评估快照、预算控制，以及可复现的 B0-B3 消融评测框架。暂时不要把“通过消融实验优化”写成已完成事实，也不要写“召回率 95%”“100% 可用”“支持 100+ 用户”“千万 DAU”或“报告生成 3-5 分钟”，因为当前没有真实环境报告、压测或业务数据支持这些结论。
