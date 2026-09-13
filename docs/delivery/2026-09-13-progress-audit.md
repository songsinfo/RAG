# 企业知识库当前进度审计

审计日期：2026-09-13。结论：主体功能代码已完成，工程验收尚未完成。下面只记录本次实际执行得到的证据，不把设计追踪、Mock 测试或数据文件存在等同于真实环境通过。

## 已验证

| 范围 | 本次结果 | 证据边界 |
| --- | --- | --- |
| Java 后端 | Maven test 成功，57 个测试，0 失败、0 错误、0 跳过 | 覆盖算法、权限、CSRF、解析、删除传播、预算、模型 HTTP 契约、问答生命周期等；未连接真实 MySQL/ES |
| 前端 | 4 个 API 契约测试通过；vue-tsc --noEmit 通过；Vite 生产构建通过 | 构建产物约 1.04 MB，存在大 chunk 警告，不影响功能验收 |
| PRD/设计 | 21 份 Markdown、122 个本地链接、81/81 AC 有追踪；DDL 39 张表、99 个外键 | 这是静态一致性检查，不是 81 项运行验收 |
| 验收脚本 | Test-EndToEnd.ps1 -SelfTest 通过；两个 PowerShell 验收脚本 AST 解析通过 | 自测证明 HTTP 状态、错误码、检索空结果不能再被误报为通过 |
| 多环境配置 | demo/evaluation/test 使用独立 Compose 项目、数据库名、端口、ES 前缀和文件根；Compose config 解析通过 | 只完成离线配置验证，容器未启动 |
| 300 题数据 | 每份 300 条、caseKey 无重复、DEV 180 / TEST 120、9 类题型数量正确、18 个来源引用 | 两份文件内容完全相同；当前是离线模板，不是可直接导入的运行题集 |

本次修复了几项会直接影响可信度的问题：

- 评估批次预算现在关联所有多轮问答和嵌套模型调用，并通过任务行锁串行核算；同时修复并发相同幂等键可能被误判为活动请求的问题。
- 父段扩展按与上下文一致的 UTF-8 上界核算，避免中文父段绕过预算。
- 评测金标准与快照记录真实环境来源，删除传播兼容历史本地来源并保留远端边界。
- Docling 轮询使用持久化总时限，任务丢失或进程重启不会刷新等待窗口；显式重试才会重置。
- 长表按完整行分组并重复表头，单行超限直接失败，避免静默发布不可控大块。
- Markdown 解析统一 CRLF/CR 换行；回答输出 token 上界扩到 2400。
- 端到端脚本已移除模型配置写入和“catch 后当成功”的逻辑，只验证无模型入库、BM25、ACL 授予/撤销与 demo 评估隔离。

## 当前阻塞

本机 Docker API 当前不可用，3307、9200、5001、8080 均未监听。Docker Desktop 日志此前给出的根因是 WSL 未安装或无法识别，因此本次不能执行以下运行验收：

- MySQL 迁移与真实约束；
- Elasticsearch 建索引、BM25 查询和 ACL 过滤；
- 无模型 HTTP 全链路；
- Docling PDF/DOCX、图片和表格的真实解析；
- 独立 evaluation 实例的快照、冻结、RETRIEVAL_ONLY 批跑；
- 真实模型问答、重写/HyDE/Multi-Query、重排与质量指标。

模型服务按当前约定暂不配置，因此没有产生真实模型调用、费用或质量数据。

## 评测集状态

本地 300 题文件使用 operator 作为逻辑 persona，以仓库相对路径作为来源引用，并用 ver-01 表示逻辑版本。后端导入要求隔离环境中真实存在的登录账号、UUID documentId 和 UUID versionId，所以必须先导入已授权资料并做运行时绑定。可以用下面的命令检查结构：

    & .\scripts\Test-EvaluationDataset.ps1 -Path .\datasets\eval_accelerator_300.jsonl -ExpectedCount 300

输出中的 runtimeBindingsReady=false 是预期提示，表示结构有效但尚未绑定。公司知识衍生题集和生成脚本已加入 .gitignore，文件仍保留在本机；不要把正文、原文引句或密钥提交到公开简历仓库。

## Docker 恢复后的执行顺序

    & .\scripts\Start-Dependencies.ps1 -Environment demo
    & .\scripts\Start-Backend.ps1 -Environment demo -Port 8080
    & .\scripts\Test-EndToEnd.ps1 -Environment demo -BaseUrl http://127.0.0.1:8080

无模型 HTTP 集成通过后，再启动隔离评测依赖：

    & .\scripts\Start-Dependencies.ps1 -Environment evaluation -IncludeDocuments
    & .\scripts\Start-Backend.ps1 -Environment evaluation -Port 8081

然后完成来源导入、18 个 sourceRef 到运行 ID 的绑定、operator 到种子账号 ops 的映射、题集冻结、快照创建和 RETRIEVAL_ONLY 批跑。真实模型联调放在这些步骤之后。

## 当前可用于简历的表述边界

可以写已实现的架构和机制，例如 Java/Spring Boot 企业知识库、文档级 ACL、结构化解析与父子分块、混合检索配置、证据追溯、评估快照和预算控制。暂时不要写“召回率 95%”“100% 可用”“支持 100+ 用户”“千万 DAU”或“报告生成 3-5 分钟”，因为当前没有真实环境报告、压测或业务数据支持这些数字。
