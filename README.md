# 企业内部知识库项目

当前阶段：F0-F4 主体代码已完成，F5 工程验收进行中。已验证后端 60 个自动化测试、前端 4 个 API 契约测试与生产构建、81 项验收标准的静态追踪；evaluation 的 MySQL/Elasticsearch、Flyway 迁移、无模型入库/检索/ACL 和一题评估链路已真实通过，Docling 与真实模型消融仍待完成。证据与边界见[当前进度审计](docs/delivery/2026-09-13-progress-audit.md)。

Java 主业务、Docling 独立解析服务，面向运营、产品和技术共用的内部知识库。前端简洁，重点是权限、文档生命周期、完整检索策略、可追溯回答、评估和运行恢复。

## 阅读顺序

1. [当前范围与交付顺序](docs/00-project-assessment.md)
2. [PRD-00 产品总览](docs/prd/PRD-00-product-overview.md)
3. [PRD-01 身份与文档权限](docs/prd/PRD-01-identity-and-access.md)
4. [PRD-02 文档解析与生命周期](docs/prd/PRD-02-document-lifecycle.md)
5. [PRD-03 检索、回答与证据](docs/prd/PRD-03-retrieval-answer-evidence.md)
6. [PRD-04 会话、取消与反馈](docs/prd/PRD-04-conversations-feedback.md)
7. [PRD-05 评估系统与策略实验](docs/prd/PRD-05-evaluation-experiments.md)
8. [PRD-06 运行管理与交付](docs/prd/PRD-06-operations-delivery.md)
9. [PRD-07 Agent 深入问答](docs/prd/PRD-07-agent-deep-qa.md)
10. [B0-B3 检索消融实验运行手册](docs/delivery/2026-09-13-retrieval-ablation-runbook.md)

上述 PRD 按 to-spec 模板组织：问题、方案、用户故事、实现决定、测试决定、范围外事项及备注。它们综合已确认访谈，不代表已实现，也没有发布到外部 Issue Tracker。

## 当前确定的边界

- 内部系统、本地账号、固定维护管理员；文档级读权限由后端执行。
- 支持 Markdown/TXT、DOCX、PDF、FAQ、Wiki 导出文件及附件；扫描 OCR、图片描述和表格结构提取纳入范围。
- 全部约定策略均需实现并对照评测，不要求每次请求全部启用。
- 先使用模拟/确认脱敏资料；不自动导入或向在线模型发送公司正文。
- 先完成 PRD，再做技术设计，最后分阶段编码。代码全部由开发代理实现，并解释实际调用链和失败案例。

## 详细技术设计

从[技术设计入口](docs/design/README.md)开始，依次阅读架构、数据库与权限、入库状态机、检索与证据、API/会话/Agent、评估和实施验收。附件含 MySQL DDL 和 Elasticsearch 映射；[验收追踪表](docs/design/08-traceability.md)逐条对应 81 项 PRD 验收。

下一步继续 F5：恢复 Docker 后运行无模型 HTTP 集成，随后在独立 evaluation 环境绑定题集来源，并按固定 B0-B3 配置完成真实模型消融评估。不要把 81 项静态追踪写成 81 项运行验收通过。

## 资料组织

当前已有 README、范围文档、PRD、技术设计、后端、前端、部署脚本和评测框架：

- docs/design：详细技术设计。
- docs/decisions：取舍与技术决策。
- docs/learning/sources、concepts、practice：来源笔记、概念笔记、实际问题与验证。
- docs/delivery：部署、验收、演示和真实项目成果。
- backend、frontend：Java 后端和功能页面。
- datasets、experiments：授权样本、题集、策略配置和脱敏报告。
- deploy：部署与初始化资料。

原始公司文件、上传数据、运行数据库、敏感日志和密钥不提交版本库。所有资料均遵循租户及文档权限。
