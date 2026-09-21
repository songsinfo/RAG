# PR #1–#4 follow-up review (2026-09-21)

结论：当前四个 PR 均不满足“没有问题才合并”的条件，保持未合并。它们是 #1 → #2 → #3 → #4 的依赖链。以下为具体阻塞项，并非仅凭 CI 状态判断。

| PR | Reviewed head | Existing local tests | Decision |
|---|---|---|---|
| #1 | `1eb005aef55da85c07455ae9953189d70511652b` | 61 passed（含此前 5 个 review 回归） | 请求修复 |
| #2 | `9db6bf66886ff76c266dfc180bf73b837c501602` | 65 passed（含此前 5 个 review 回归） | 请求修复 |
| #3 | `a51ffa4162564e40f118d3c9c99d148b0d15f41d` | 71 passed（含此前 5 个 review 回归） | 请求修复 |
| #4 | `116b4f74af16c5e97033f223bd0aaa016fa561cd` | 65 passed, 2 failed（backend/tests） | 请求修复 |

## 1. [P2] 恢复索引时遗漏 published index_epoch — PR #1

`backend/app/knowledge/service.py:1236-1243` 的 restore 调用 `upsert_chunks` 不传 `index_epoch`，默认写 0；durable ingestion 发布的 `index_build.fence` 则为 1 或更高。检索按 published fence 过滤。外部索引被清理/丢失后，执行 trash → restore，文档恢复为 LIVE，但恢复的向量全部不匹配检索过滤条件。若旧向量还在，这个问题会被掩盖。

修复：restore 读取该 build 的 fence，与 version_id 一起传给 upsert；验证索引丢失后的恢复检索。新增脚本 `test_restore_rebuild_preserves_published_epoch` 在 #1 上失败，恢复后 hits 为 []。继承到 #2–#4。

## 2. [P1] QA_RUN 不在生产 job.kind CHECK 约束中 — PR #2

`backend/app/qa/service.py:519-535` 为每个 RAG/AGENT 问答插入 `kind="QA_RUN"`，但 Compose 挂载的 `docs/design/schema.sql:345` 仅允许 INGEST/PURGE/RESTORE/EVALUATE/EXPORT/CLEANUP。按仓库标准方式初始化 MySQL 后，问答提交违反约束，无法创建任务。SQLite 测试 metadata 没有这个 CHECK，现有绿灯不能覆盖此问题。

修复：同步新建库 schema，并提供已有数据库的约束升级方式；测试使用与生产相同约束。`test_qa_submit_satisfies_production_job_kind_constraint` 从 schema 提取真实 allowlist，在 SQLite 以 trigger 等效约束后调用真实 submit_run，得到 IntegrityError。此复现不是实际 MySQL 集成测试。继承到 #3/#4。

## 3. [P1] QA lease takeover 后旧执行者仍可提交结果 — PR #2

`backend/app/runtime/worker.py` 的 `_run_qa` 不把 job id/lease_epoch 传入 `execute_queued_run`，`QaService._finalize_run` / `_fail_run` 也不验证租约。epoch=1 执行者超时被 epoch=2 接管后，旧执行者仍可以把新 job 置为 COMPLETED，写入旧结果并清除 lease。INGEST 已做的 fencing 没有覆盖新的 QA 路径；Agent 检查点同样未按执行 epoch 隔离。

修复：将 epoch 贯穿执行、结果/事件/来源和检查点写入，并在同一事务内验证持有权。`test_qa_worker_cannot_finalize_after_lease_takeover` 用真实 claim 和过期再 claim 建立 epoch 1→2，在旧回调返回时调用生产 finalize，发现 replacement job 被置为 COMPLETED。继承到 #3/#4。

## 4. [P1] 单 worker 被 EVALUATE 占用，子 QA 无人处理 — PR #2

`backend/app/main.py:47-59` 默认只有一个 embedded DurableJobWorker；该 worker 同步运行 EVALUATE。`backend/app/evaluation/runner.py:464-482` 把每道题改成入队 QA_RUN 后，在同一执行者内等待最多 30 秒。唯一 worker 无法同时领取它刚排入的 QA，评测会把仍然 QUEUED 的题当作失败，之后才有机会执行 QA。

这是代码路径检查的结论，未进行真实部署压测。#3 新增独立 QA/EVAL embedded worker pools 解决了默认嵌入部署的这个问题；因此不能仅凭 #3 的改善将 #2 单独标记可合并。独立 worker 部署也需要配置足够的 QA 执行者。

## 5. [P1] 永久删除没有清理 Agent checkpoint 原文 — PR #3，#4 仍存在

#3 `backend/app/qa/checkpoint.py:53-71` 把 messages/evidence 原文持久化到 job.checkpoint；#4 `backend/app/qa/job_checkpointer.py` 改为序列化保存同样数据。`KnowledgeService._execute_purge` 清理 qa_run/run_event/run_source 等，却从未清除关联 QA job 的 checkpoint，checkpointer.delete_thread 也未接入 purge。

`test_purge_removes_document_from_agent_checkpoint` 使用生产 checkpoint writer 保存包含文档原文的状态，登记 run_source，执行真实 trash + purge，确认文档为 PURGED、问答为 SOURCE_PURGED 后，仍能通过 checkpoint reader 解出原文。#3/#4 均复现。base64 编码不等于删除。

修复：将相关 checkpoint/history/pending writes/resume payload 纳入永久删除范围，并防止仍在运行或租约已失效的执行者重新写回。需要覆盖 purge 与 checkpoint write 的竞态。

## 6. [P1] 原生 checkpointer 引入并发 SQLite 回归，最新 CI 失败 — PR #4

当前 head 的 [Actions run 35570205339](https://github.com/songsinfo/RAG/actions/runs/35570205339) backend job 106240046372 失败：`test_agent_hitl_checkpoint_and_resume` 得到 FAILED，日志显示 `JobCheckpointSaver.put_writes` 的数据库操作触发 `sqlite3.InterfaceError: bad parameter or other API misuse`。CI 日志 checkout SHA 与上表 #4 一致。

本地完整 backend/tests 得到 65 passed / 2 failed：HITL 测试和 `test_agent_single_tool_search_and_answer`，同类 checkpoint 数据库错误。测试使用 StaticPool 的同一 SQLite 连接，LangGraph 的 checkpoint/pending-write 后台工作与应用数据库访问仍存在并发；设置 `durability="sync"` 并未使所有数据库操作串行。此前某次 CI 通过不能替代当前 head 的结果。

修复：为测试/SQLite 路径采用安全的连接与事务并发方案，并验证 native checkpoint、tool 和 HITL resume；不能吞掉异常或放宽终态断言。这里证明的是测试/SQLite 路径回归，没有将该 SQLite 异常泛化成已验证的 MySQL 故障。

## Reproduction

脚本只使用 SQLite、mock Milvus 和伪造的无敏感测试原文。默认 fixture 每个用例重建测试数据库。需要被测 checkout 的 backend requirements 和已有 docs/reviews/pr-1/test_review.py。

```powershell
$env:REVIEW_ROOT = 'D:\tmp\learn_ai\RAG-pr1-review'
python -m pytest ./test_stack_review.py -q -p no:cacheprovider -k restore --tb=short
$env:REVIEW_ROOT = 'D:\tmp\learn_ai\RAG-pr2-review'
python -m pytest ./test_stack_review.py -q -p no:cacheprovider -k 'production or takeover' --tb=short
$env:REVIEW_ROOT = 'D:\tmp\learn_ai\RAG-pr3-review'
python -m pytest ./test_stack_review.py -q -p no:cacheprovider -k purge --tb=short
$env:REVIEW_ROOT = 'D:\tmp\learn_ai\RAG-pr4-review'
python -m pytest ./test_stack_review.py -q -p no:cacheprovider -k purge --tb=short
```

以上新增断言在被测 head 上均失败，符合列出的缺陷；它们未加入现有 CI。既有 suite：#1/#2/#3 执行 `python -m pytest -q -p no:cacheprovider tests ../docs/reviews/pr-1/test_review.py --tb=short`，#4 执行 `python -m pytest -q -p no:cacheprovider tests --tb=short`，工作目录均为各 checkout/backend。

CI #2 run 35564722855 与 #3 run 35568272827 的 frontend/backend jobs 均成功。没有启动真实 MySQL、Milvus、Docling、S3、LiteLLM 的端到端环境；因此报告不宣称这些集成已经通过生产验证。

本地复用了现有 Python 3.12 虚拟环境：langchain 1.4.0、langchain-core 1.6.3、langchain-openai 1.6.2、langgraph 1.2.11、pymilvus 3.0.1；与 PR #2–#4 锁定的 langchain 1.4.2/pymilvus 3.0.2 存在差异。SQL/epoch/purge 复现不调用真实 Milvus 或 langchain agent API；#4 同类失败另由按 requirements 安装的 GitHub CI 独立确认。

