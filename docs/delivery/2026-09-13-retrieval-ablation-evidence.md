# 检索消融实验事实记录

本记录总结 2026-09-13 的本地脱敏报告 [retrieval-ablation-d3e668c7db334cde87301f5d3db8feea.json](../../.runtime/reports/retrieval-ablation-d3e668c7db334cde87301f5d3db8feea.json)，不包含问题、原文或密钥。2026-10-08 复核了报告字段与生成脚本；该历史报告未记录代码提交或后端类型，不能直接归属于当前 Python/Milvus 版本。

## 可比性

- 数据集共 300 条，DEV 180 / TEST 120；本次运行固定 TEST 分区、同一数据集版本、同一语料快照和 RAG 模式。
- 四组报告状态均为 `COMPLETED`，比较结果为 `MATCHED_MANIFEST`，表示报告中已比较的清单字段一致；不代表未记录的代码、真实模型及依赖版本也一致，也不代表每题成功。
- 120 道 TEST 题中有 108 道包含检索金标准，因此 Hit@5、Recall@5、MRR 和 nDCG@5 的分母均为 108。

## 检索指标

| 实验 | 路线 | Hit@5 | Recall@5 | MRR | nDCG@5 |
|---|---|---:|---:|---:|---:|
| B0 | BM25 | 0.500 | 0.458 | 0.371 | 0.332 |
| B1 | Dense | 0.648 | 0.610 | 0.398 | 0.386 |
| B2 | Hybrid + RRF | 0.685 | 0.645 | 0.393 | 0.385 |
| B3 | Hybrid + RRF + Reranker | 0.917 | 0.886 | 0.800 | 0.699 |

报告按照 nDCG@5、Recall@5、已知费用的固定顺序选择 B3。相对 BM25 基线，B3 的 Recall@5 提升 42.7 个百分点，nDCG@5 提升 0.368，MRR 提升 0.429。

## 证据边界

这些数值记录了该历史运行的检索层离线比较，报告按既定规则选择 B3；它们不能证明当前 Python/Milvus 版本复现了相同效果。它们不能证明 120 道题的端到端回答全部成功：B0 只有 14 道完成生成，B1、B2、B3 没有完成生成，其余主要因 `MODEL_PROVIDER_ERROR` 失败；LLM Judge 与人工复核覆盖率也不足。因此不得表述为“生产环境落地”“端到端准确率提升”或“120 道题全部通过”。

另一个文件 `artifacts/baseline-report.json` 不能作为实测证据：[Generate-BaselineReport.ps1](../../scripts/Generate-BaselineReport.ps1) 直接写入逐题成功标志、延迟和固定汇总指标，没有调用被测检索/问答链路。不要将其与本历史消融报告混用。

## 建议简历表述

> 在 2026-09-13 的历史离线实验中，对 TEST 分区 108 道含检索金标准的问题比较 BM25、Dense、Hybrid-RRF、Hybrid-RRF+Reranker；报告记录 BM25 与混合检索加重排的 Recall@5 分别为 45.8% 和 88.6%，nDCG@5 为 0.332 和 0.699，MRR 为 0.371 和 0.800。该结果限于历史检索层比较，未验证端到端回答质量，也不能直接归为当前 Python/Milvus 版本的成绩。
