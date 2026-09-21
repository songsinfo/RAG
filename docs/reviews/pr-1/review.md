# PR #1 验收与代码审查

审查日期：2026-09-21。结论：未完全实现，建议 Request changes。

- PR：https://github.com/songsinfo/RAG/pull/1
- Base：0813791509f629634accaa06322978bfa2abcabf
- Head：75803c27d24fcf2399b363db813cfeafb9a8f6ff
- 范围：27 个改动文件，结合相关调用链、原会话全部 6 轮内容、现有测试、定向复现和 GitHub CI 日志。
- 此报告针对上述 Head 的业务代码；后续提交仅增加审查材料。复现脚本位于同目录 test_review.py，不纳入默认 backend/tests 测试收集，需按下方命令显式运行。

## 原始需求核对

原会话最后要求“Observability 之前的建议……按照建议的修复，然后 1234 也都实现”。明确的 1–4 对应：统一 ACL/外发检查/真实 buildId、Docling、Reranker/Parent Expansion、配置控制流水线。以下将 Observability 视为后续范围；如果原意也包含 Observability，则该项完全未实现。

| 要求 | 结果 | 证据与缺口 |
|---|---|---|
| README 架构图、入库图、问答图及 Mermaid 修复 | 图和修复已在源码中 | PR 继续更新图；未在浏览器重新渲染，Worker Retry/Recovery 等描述超过实现能力 |
| /search、RAG、Agent search_knowledge 共用 ACL | 已接入 | 三者使用 RetrievalPipeline 和 get_readable_build_records |
| 所有模型外发前重新校验来源 | 部分 | ANSWER 和 Agent 下一轮有复核；Reranker 外发在复核之前；普通 RAG 的 Strict Judge 外发前也没有新复核 |
| Evidence 真实 buildId/versionId | 已修正主要错误 | 不再使用 allowed_build_ids[0]；Parent 扩展后的完整来源映射仍有缺口 |
| Docling PDF/DOCX/OCR/Table | 有实际 HTTP 接入 | binary → /v1/convert/file → Markdown → source units；新增测试是 FakeClient，不能证明真实 PDF/DOCX/OCR 服务跑通；Markdown 行号也不是原 PDF 页码 |
| RRF → Reranker → Parent Expansion | 已接入，尚有正确性问题 | 排序/扩展真实执行；外发安全和扩展后 provenance 需修 |
| Retrieval Config 真正控制流水线 | 部分 | 候选数、rrfK、rerank、parent、finalTopK 能控制；Dense-only/BM25-only/RRF 开关未实现；MMR 未实现 |
| B0–B3 消融、Evaluation 切配置 | 未形成完整闭环 | B0 无法实现；evaluation/runner.py:393 仍直接 search(top_k=5)，449 提交完整 QA 时未传评测 configId |
| 持久化任务、租约、重试、恢复 | 部分且不可靠 | 只迁移 INGEST/PURGE；QA/Evaluation 仍使用线程池；恢复不幂等，业务写入未受 lease_epoch 保护；FAILED 不自动重试 |
| S3/MinIO 对象存储 | 存取接入，删除不可靠 | S3 删除异常被吞掉；旧 local 路径删除回归；没有已有本地文件迁移工具 |
| CI/Docker | 基线已添加但 CI 失败 | 前端通过；后端测试收集失败；尚无 lint/type-check 后端、安全扫描、镜像构建、迁移测试等完整工程门禁 |
| Alembic、灾备/恢复演练、K8s/Helm | 未实现 | 这些出现在较宽泛的企业化建议中，不能因本 PR 已加 Dockerfile 而视为完成 |

## Findings

### 1. [P1] 在 Reranker 外发之前复核 ACL

位置：backend/app/retrieval/pipeline.py:50–59，复核实际在 73–74。

触发：Milvus 检索过程中/返回时撤销用户权限、删除来源或切换 active build。当前代码先将 hits 文本传给 rerank_hits → ModelGateway.rerank → 外部模型网关，之后才检查 MySQL。最终返回 SOURCE_CHANGED 不能撤回已经发送的文档。

复现：test_revoked_hits_must_not_reach_reranker 在 search 返回前把来源改为 TRASHED；最终确实抛 ApiException，但 Reranker 已收到 `Milvus private company knowledge.`。需要对所有待发送候选在 rerank 前复核，而非仅最终 Top-K。普通 RAG 的 Judge 外发也应纳入同一边界。

### 2. [P1] 将租约 epoch 传入业务执行并保护发布

位置：backend/app/runtime/worker.py:122–125；相关 backend/app/knowledge/service.py:811–825。

Worker 领取任务会增加 lease_epoch，心跳和 _fail 有部分校验，但 _execute_ingestion_job 完全不接收 epoch。旧 Worker 因暂停/心跳失败失去租约后，仍可以写入 source/chunk、更新 active_build_id 和把新 Worker 的任务标成 COMPLETED/FAILED。心跳停止不会停止业务线程。PURGE 也未带租约。

复现：test_old_worker_cannot_publish_after_lease_loss 在解析阶段将 DB epoch 从 1 改成 2；epoch=1 的 Worker 仍发布了 Build。必须在副作用与最终发布事务中进行所有权/fencing 校验。

### 3. [P1] 恢复入库任务前保证阶段幂等

位置：backend/app/runtime/worker.py:118–125；相关 backend/app/knowledge/service.py:724–750。

source_unit 在 CHUNKING 前单独提交。此后进程崩溃，Worker 再次领取过期任务时从 parse 开始，给相同 build/unit_key 生成新 UUID 并再次 INSERT。生产 schema 的 uq_unit_key(tenant_id,build_id,unit_key) 会冲突；执行器吞掉异常，把 Job 标为 PARSER_FAILED，之后也不会自动领取 FAILED 任务。

复现：test_expired_lease_restart_is_idempotent 使用 SQLite 加入 schema.sql:246 的同等唯一索引；在 chunk_units 入口模拟进程退出，再让 lease 过期、重新领取。epoch 增到 2，但任务最终 FAILED。应使用阶段 checkpoint + 幂等写入，或在有租约保护的前提下安全重建未发布数据。

### 4. [P1] 恢复旧格式 local artifact 的删除路径

位置：backend/app/knowledge/artifacts.py:104–110。

PR 删除了原有 legacy fallback。三段 storage_key 对应的真实路径包含额外 sha[:2] 目录，save/read 仍支持它，delete 却只尝试 root/storage_key。Purge 会成功标记数据库，但原始文件保留且可读。

证据：现有 test_document_lifecycle_trash_restore_purge 在 test_lifecycle.py:533 失败；base 中对应删除 fallback 存在，本 PR 移除了它。

### 5. [P1] 不要将 S3 删除失败视为清除成功

位置：backend/app/knowledge/artifacts.py:98–103。

delete_object 的 AccessDenied、网络错误等全部被 except Exception/pass 吞掉。_execute_purge 随后仍将 artifact/document 标为 PURGED、deletion_record 标为 COMPLETE。这会留下原始敏感文件且无法通过任务状态发现。

复现：test_s3_delete_failure_must_propagate 的假 S3 返回 AccessDenied，删除函数正常返回。应传播异常并让任务保留失败/可重试状态，只有确认对象删除成功才完成清除流程。

### 6. [P2] Parent 扩展后同步扩展 Evidence 的来源映射

位置：backend/app/retrieval/pipeline.py:157–173、190–192。

_expand_parents 把 text 换成完整 Parent，_attach_provenance 却只按原命中 Child 的 chunkId 查询 spans。Parent 若包含另一个 Child 的 source unit，模型可以引用那段文本，返回的 unitId/location/sourceUnits 却不包含真实来源，无法按证据链定位。

复现：test_parent_context_provenance_covers_all_units 用两段各 400 多字符文本实际入库，生成一个 Parent、两个 Child；扩展后 text 有两段，sourceUnits 只有第一段。需要构建完整 Parent spans，并保留到 context 字符区间的映射。

### 7. [P2] 支持实际的检索模式切换，才能兑现 B0–B3

位置：backend/app/retrieval/config.py:54–66；相关 milvus_service.py:192–214。

配置解析忽略 retrieval.mode 和 rrf.enabled，候选数最小强制为 1；Milvus 始终创建 Dense + Sparse 两个请求并执行 RRFRanker。传入 mode=DENSE、sparseTopK=0、rrf.enabled=false 后，探针显示 sparse_top_k=1、rrf_k=60。B0 仍混入 BM25，不能用来做所承诺的 Dense-only 消融。

此外 Evaluation 仍使用另一条调用路径：RETRIEVAL_ONLY 固定 top_k=5；完整 QA 没传评测配置。该遗漏不单独冒充本 PR 新引入 bug，但属于需求未完成。

### 8. [P2] 修正 CI 的 pytest 导入入口

位置：.github/workflows/ci.yml:22–24。

workflow 只安装 requirements，然后直接运行 pytest -q，项目未安装且未设置 PYTHONPATH。在当前 GitHub runner 中收集 conftest 就报 ModuleNotFoundError: No module named 'app'，所有后端测试都没开始。使用 python -m pytest 或先正确安装项目可以修复入口；修复后还必须处理第 4 项测试失败。

远端证据：https://github.com/songsinfo/RAG/actions/runs/35556538225/job/106201138813

## 验证记录与边界

1. 在 PR Head 独立目录，借用已有 Python 3.12 venv 运行 `python -m pytest -q -p no:cacheprovider`：54 passed、1 failed；失败为上述旧路径删除回归。
2. 新增独立审查脚本 test_review.py 的 5 个预期正确行为断言：5 failed，分别复现上述 1、3、2、6、5；具体断言和触发方式见同目录 test_review.py。这些是揭示缺陷的回归检查，失败不是修复后的验收结果。
3. Worker 恢复检查在 SQLite 中复刻相关唯一约束；未宣称验证了真实 MySQL SKIP LOCKED、多进程锁竞争或网络分区。
4. Reranker/S3 的网络边界使用假实现，未实际发送企业文档或调用收费模型。
5. GitHub CI run 35556538225：frontend check/test/build 成功；backend compile 成功、pytest 收集失败。原会话“连接器没有触发 Actions”的说明已经过时。
6. 未进行真实 Docling/OCR、Milvus、LiteLLM rerank、S3/MinIO 的端到端联调；新增 PR 测试只有两个函数，不能替代上述联调或故障恢复测试。

运行定向检查（PowerShell）：

```powershell
Set-Location backend
python -m pytest ../docs/reviews/pr-1/test_review.py -q -p no:cacheprovider --tb=short
```

建议先修权限外发、租约/恢复、删除语义，再补 provenance/config/evaluation 闭环并让 CI 通过；当前不建议合并。
