# 修复后复查：2026-09-21

本报告取代前轮报告的当前状态判断；原报告保留为历史证据。

| PR | 本次审查 head | 本地既有测试及适用 review 回归 | Head CI | 处理 |
|---|---|---|---|---|
| #1 | 805556c3fb5c1527484628f6b295d9581d54a4d8 | 62 passed | success / 35573382986 | 已合并 |
| #2 | 2c47c91bcad76202dfc089c78cf3de1d77b9d358 | 68 passed | success / 35574472965 | 暂不合并 |
| #3 | e9994ea9cf8170fdec1d3532fe70b675c8cfe979 | 75 passed | success / 35574527740 | 暂不合并 |
| #4 | 9ef86d2af3f687be6dc4ef14c49dc180db3b7065 | 76 passed | success / 35574566317 | 暂不合并 |

#1 的 restore 现在传入 published fence，原复现通过。已按用户此前授权合并到 feat/python-migration，merge commit 为 a116edaac1337351e726d3d07be5dfc71e1efba7。#2 已改为直接以该目标分支为 base，#3/#4 保留原依赖链。

#2 已修复 QA_RUN schema（含已有库迁移）、默认 embedded worker 隔离、过期执行者 finalize/fail 的原子 epoch 条件。#3/#4 已补 checkpoint purge 清理及防止取消后写回；#4 的 SQLite 数据库连接串行保护使本轮既有 Agent/HITL 测试通过。

## 剩余 [P1]：重试初始化发生在租约校验之前

影响 #2–#4 的 `backend/app/qa/service.py::execute_queued_run`。函数先读取 RUNNING/FAILED 状态，删除 run_source/run_event 并重置 qa_run 为 QUEUED；直到这些变更已经 commit，才进入 `qa_execution_scope(job_id, lease_epoch)` 并调用 `_execute_run`。因此新的最终提交保护并不保护入口处的清理副作用。

确定性复现（test_late_worker.py）：

1. A 以 epoch=1 领取 QA 任务，在调用业务执行入口前暂停。
2. 租约过期，B 真实 claim 到 epoch=2。
3. 建立 B 正在执行的 RAG 状态：RUNNING，1 条真实文档来源、1 条事件。
4. A 恢复，调用实际 worker._run_qa(old_job)。
5. A 先删除 B 的来源和事件、重置运行状态；后续租约校验拒绝 A，但已无法撤销刚才 commit 的破坏。

在三个被测 head 上均得到 `('QUEUED', 0, 0)`，期望 `('RUNNING', 1, 1)`。测试只模拟 B 已产生的中间状态，使用真实 claim、真实过期接管、真实旧 worker 执行路径；不依赖实际并发线程或模型服务。

除了运行状态/审计被破坏，run_source 丢失还影响读取权限与永久删除时的来源追踪。这是前轮 fencing 问题的剩余入口，不应因原 finalize 回归通过就认定整个执行链完成隔离。

建议将租约上下文和校验移到任何派生数据清理之前，并在同一事务内使用行锁或条件写保护检查到修改期间的持有权；单纯在清理前增加一次无锁 SELECT 仍有接管窗口。失去租约时应保持接管者状态、来源和事件完全不变。

## 运行

将本目录 test_late_worker.py 与 test_stack_review.py 放在同一目录。REVIEW_ROOT 指向被测 checkout，依赖安装方式同 backend/requirements.txt：

```powershell
$env:REVIEW_ROOT = 'D:\tmp\learn_ai\RAG-pr2-review'
python -m pytest ./test_late_worker.py -q -p no:cacheprovider --tb=short
```

分别指向 #2/#3/#4，均为 1 failed（该断言表达正确行为）。不计入上表既有测试通过数量。

验证边界同前轮：本地 Python 3.12 环境，LangGraph 1.2.11；langchain 1.4.0 / pymilvus 3.0.1 与 PR 锁定小版本不同；GitHub CI 使用各分支 requirements。新增失败仅涉及 SQL 执行入口和 lease，没有调用这两个包的线上服务。未运行真实 MySQL/Milvus/Docling/S3/LiteLLM 集成环境，未宣称生产端到端验证完成。
