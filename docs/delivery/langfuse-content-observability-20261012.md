# Langfuse 可配置输入输出（2026-10-12）

基于 `main@4a367d2`，按用户要求补齐模型、检索、工具及 QA 根节点输入输出。使用锁定的 SDK 4.16.0 和现有私有服务 4.56.0，不启用自动 callbacks，不新增模型调用、数据库表或正文运行事件。首期与 EOBS 的历史实验记录保留。

## 配置及使用

默认 `RAG_LANGFUSE_ENABLED=false`。启用 Langfuse 后，默认 `RAG_LANGFUSE_DATA_MODE=METADATA_ONLY`，输入输出仍为空。

| 配置 | 默认 | CONTENT 下的作用 |
| --- | --- | --- |
| RAG_LANGFUSE_DATA_MODE | METADATA_ONLY | CONTENT 开启正文，METADATA_ONLY/未知值关闭全部正文 |
| RAG_LANGFUSE_CAPTURE_MODEL | true | model.* generation 和 agent.model |
| RAG_LANGFUSE_CAPTURE_RETRIEVAL | true | rag.retrieve 和每个真实执行的检索图阶段 |
| RAG_LANGFUSE_CAPTURE_TOOL | true | agent.tool，包括拒绝、参数错误和 mock 标记 |
| RAG_LANGFUSE_CAPTURE_QA | true | qa.worker.attempt 与 agent.run |

分类 true/1/yes 启用，其他值关闭；开关在进程环境配置，修改后重启 API/独立 Worker。正文开关不影响元数据开关、采样、原模型预算/计费或业务结果。

在仓库根目录、日常启动后端的同一个 PowerShell 中：

```powershell
& .\scripts\Import-Environment.ps1 -Path .\.local\langfuse-eval.env
$env:RAG_LANGFUSE_DATA_MODE = 'CONTENT'
$env:RAG_LANGFUSE_CAPTURE_MODEL = 'true'
$env:RAG_LANGFUSE_CAPTURE_RETRIEVAL = 'true'
$env:RAG_LANGFUSE_CAPTURE_TOOL = 'true'
$env:RAG_LANGFUSE_CAPTURE_QA = 'true'
& .\scripts\Start-Backend.ps1 -Environment demo
```

也可把这些非秘密开关存于忽略的 `.runtime/demo.env`；如果该文件已有同名开关，Start-Backend 导入时以该文件为准。凭据保持私有后端文件，不复制到前端。现有实例位于 `http://127.0.0.1:3031`，账号/密码位置见 backend README。然后正常发起新问答，在追踪列表打开 `qa.worker.attempt`。

若关闭工具节点正文，用 `RAG_LANGFUSE_CAPTURE_TOOL=false`；关闭所有正文用 `RAG_LANGFUSE_DATA_MODE=METADATA_ONLY`。分类关闭仅作用于该类节点：模型消息可能含检索/工具结果，最终答案也可能包含资料。若不希望任何工具/资料正文继续上传，应关闭所有可能承载它的分类或使用总开关。

## 字段与实现边界

- MODEL：记录现有 tracked 调用边界的 payload（messages、tools，或 rerank query/documents）与响应正文。LangChain 回复保留 content、tool_calls 和 invalid_tool_calls，HTTP 只取 JSON body；不导出 headers、API 密钥配置或 response_metadata。不是供应商内部思考或完整 HTTP 报文。已有独立 Embedding 入口未新增向量正文。
- RETRIEVAL：总节点记录 query/history/过滤范围与最终 hits/config；阶段输入为实际进入节点的查询/状态，输出为该节点真实更新字段。Dense/BM25 路线、融合顺序、hydrate、parent 与 context budget 都可以比较；不会将被裁掉的文本回填成“模型已收到”。模型实际上下文以对应 generation 的消息为准。
- TOOL：参数与实际返回给模型的 result_payload，保留 mock/notice、拒绝及参数错误。工具 excerpt 已经过原流程裁剪/guard，观测按实际返回形态保存。
- QA：根节点保存问题与最终通过当前权限读取的答案；agent.run 是最终应用校验前的 Agent 返回结果，HITL/未产生最终答案显示明确状态。

所有内容先由明确类型边界序列化，再写标准 `langfuse.observation.input/output` JSON 属性。只允许已知节点类别，导出和 masking 继续执行分类开关、scope、异常/属性白名单检查。源权限或 job 状态不允许时跳过对应正文，不将观测失败变成业务失败。

原有 OTLP destination 收到的 span 副本移除新增 input/output，保持其余元数据及父子关联；不是把 collector 地址改成 Langfuse。普通日志不新增 payload 或响应，checkpoint 不持有 SDK/span 对象。

一个 input/output 上限 1 MiB，超限写 `captureStatus=OMITTED_TOO_LARGE`、originalBytes、limitBytes；不可序列化则跳过该字段并记录安全错误码日志。失败节点没有实际响应时 output 可为空；开启正文并不伪造失败输出。旧 trace 不回填。关闭开关不删除已上传副本，访问与保留由私有实例管理。

属性映射已对照[官方 OTel 文档](https://langfuse.com/integrations/native/opentelemetry)及本地 SDK。锁定私有服务的 observations API 返回 JSON 字符串时，验收脚本解码后逐字段核对；不是仅检查节点存在。

## 验证

新增回归覆盖默认/未知模式、四类开关、导出时关闭、未采样、真实 SDK 结构化消息/tool calls、超限、不可序列化、观测异常、OTLP 正文隔离，以及完整合成 Worker 的模型/检索/mock 工具/QA 节点。权限拒绝的检索正文不外发，原结果保持不变。

私有服务合成验收：

```powershell
pwsh -File scripts/check_langfuse_local.ps1 -Content
pwsh -File scripts/check_langfuse_local.ps1
```

两者只调用合成供应商夹具，真实经过 Worker、预算、SDK 和目标服务，不产生付费模型调用。CONTENT 收据写 `.local/langfuse-content-receipt.json`，默认模式保留 `.local/langfuse-receipt.json`。

本次完整后端 **233 passed / 3 skipped**（私有/付费 opt-in）；历史审查回归 **32 passed**。CONTENT 和 METADATA_ONLY 私有服务验收各 **1 passed**：前者确认四类节点及 mock 工具正文，后者确认全部节点 input/output 为空。两者都收到 2 个 generation、4 个幂等 score，正常/对照/HITL 模型调用次数保持不变。编译、PowerShell 语法与 diff 检查通过。

机器可读记录见[收件验收](langfuse-content-acceptance-20261012.json)。CONTENT 合成串行对照每组 12 条，p50 从 off 746.99 ms 到 on 1143.68 ms，包含正文序列化与新增权限查询；该结果不能当作生产延迟或容量结论。未发起新的付费调用或上传公司资料。
