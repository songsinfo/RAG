# B0-B3 检索消融实验运行手册

这套流程固定同一题集版本、语料快照、TEST 分区和回答模式，只改变检索路线与是否重排：

| ID | 检索路线 | Reranker |
| --- | --- | --- |
| B0 | BM25 | 关 |
| B1 | Dense | 关 |
| B2 | Hybrid + RRF | 关 |
| B3 | Hybrid + RRF | 开 |

固定配置保存在 experiments/retrieval-ablation.json。四组都关闭 Rewrite、HyDE、Multi-Query、MMR 和父段扩展，并固定 Original 表示、TopK、候选数、RRF、上下文预算。这样 B2 与 B3 的差异只有 Reranker。

## 运行前提

1. evaluation 隔离环境的 MySQL、Milvus（含 etcd/MinIO）、Docling、LiteLLM 和 Python/FastAPI 后端已启动。
2. 18 个来源已被确认是模拟资料或允许用于评测的脱敏资料，并已导入 evaluation 环境。
3. 导入构建启用 embedding=true，Embedding 模型、维度与查询模型完全一致；否则 Dense/Hybrid 发布会失败关闭。
4. .runtime/evaluation.env 已配置真实 Chat、Embedding 和 Reranker 服务。MODEL 类型配置中的 CNY 单价应改成实际服务价格，避免费用报告失真。
5. 300 题模板已完成来源引文抽查。真实实验只使用冻结后的 TEST 分区调最终结果；调参使用 DEV。

## 一、绑定 300 题到运行时记录

首次运行会在 .runtime/evaluation-bindings.json 生成绑定模板并停止：

    & .\scripts\Bind-EvaluationDataset.ps1 -InputPath .\datasets\eval_accelerator_300.jsonl

把 operator 映射为 evaluation 种子账号 ops，再把每个 sourceRef 填成对应上传结果中的 UUID documentId 和 versionId。不要修改题目、标准事实或引文。填完后再次执行同一命令，生成 .runtime/eval-bound.jsonl。脚本会做结构、账号和 UUID 完整校验；后端冻结时还会逐条确认引文确实存在于指定版本。

绑定文件、运行题集和报告都位于 .runtime，不会进入 Git。

## 二、执行 B0-B3

先启动隔离依赖和后端：

    & .\scripts\Start-Dependencies.ps1 -Environment evaluation -IncludeDocuments
    & .\scripts\Start-Backend.ps1 -Environment evaluation -Port 8081

在另一个 PowerShell 窗口执行：

    & .\scripts\Run-RetrievalAblation.ps1 -BaseUrl http://127.0.0.1:8081 -DatasetPath .\.runtime\eval-bound.jsonl -DatasetName enterprise-golden-300 -Split TEST -ApprovedSyntheticOrRedacted

-ApprovedSyntheticOrRedacted 是对本次快照资料范围的显式确认。脚本会依次完成：

1. 校验运行题集并创建、导入、冻结 Dataset Version；
2. 从金标准锚点提取文档 ID，创建同一 Snapshot；
3. 发布 B0-B3 四个不可变 Retrieval Config；
4. 每组先做费用上界估算，再启动 RAG + LLM-as-a-Judge 评测；
5. 等待全部运行进入终态，调用配对比较，并导出不含问题和原文的脱敏结果；
6. 按 nDCG@5、Recall@5、已知费用依次排序，记录选中策略和相对 B0 的增量。

单组费用上限默认 10 CNY，可通过 -MaxCostCny 调低，不能调高到 10 以上。任何运行失败、预算耗尽、超时或 manifest 不一致，脚本都会以失败结束并保留诊断报告，不会输出“优化完成”。

## 三、报告与简历证据

报告保存在 .runtime/reports/retrieval-ablation-<executionId>.json。写简历前至少核对：

- comparable=true，B0-B3 的 status 全部为 COMPLETED；
- planned=120、completionCoverage=1，并解释 retrievalDenominator 与 ndcgDenominator；
- 汇总包含 hitAt5、recallAt5、mrr、ndcgAt5；
- 逐题结果保留 LLM Judge 标签，抽样人工复核并记录复核覆盖率；
- selectedExperimentId 与报告数据一致，不能因为预期 B3 更强而改选择规则；
- 费用、P95、失败题和未知费用调用都进入报告，不能只展示质量最高的成功样本。

只有生成并核对这份真实报告后，才可以把“通过消融实验优化 Hybrid Retrieval 与 Reranker 策略”写成已完成事实。在此之前，应表述为“实现可复现的 B0-B3 消融评测框架”。
