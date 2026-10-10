# 知识库前端

Vue 3 + TypeScript + Element Plus；前端仅请求真实 `/api/v1`，不自行伪造请求成功；统一 Agent 的四个业务工具数据在**后端**是固定模拟 fixture，页面必须显式告知用户。服务端必须启动在 `127.0.0.1:8080`。开发代理保留同源 Cookie 与 Origin，写请求先获取 CSRF。部署时同一站点将 `/api` 反向代理到后端，并把 `dist` 作为静态目录。

Windows PowerShell（按实际 Node 安装路径调整）：

```powershell
& 'C:\nvm4w\nodejs\node.exe' 'C:\nvm4w\nodejs\node_modules\npm\bin\npm-cli.js' install --ignore-scripts
& 'C:\nvm4w\nodejs\node.exe' node_modules/vue-tsc/bin/vue-tsc.js --noEmit
& 'C:\nvm4w\nodejs\node.exe' node_modules/vite/bin/vite.js build
& 'C:\nvm4w\nodejs\node.exe' node_modules/vite/bin/vite.js --host 127.0.0.1
```

打开命令输出的本地地址，使用后端配置的演示账号。普通用户查看问答与资料；管理员还可维护资料、ACL、任务、配置、反馈和评测。ACL、标注、配置等低频复杂操作用 JSON 表单，服务端仍须检查所有权限和版本号。

当前 `main`（PR #14 已合并）中，新提问页面不再显示 RAG / Agent 模式选择，提交请求不带 `mode`；服务端规范化为统一 Agent，并创建 `QA_RUN` Durable Job。旧 `REALTIME` 分支仅为了旧运行兼容而保留。前端使用 EventSource 订阅 `/api/v1/runs/{runId}/events` 获取阶段进度，SSE 断线或不可用时回退 GET `/runs/{runId}` 轮询，终态再读取受保护的历史和证据。当前不是逐 Token 流式生成，也不展示未经验证的模型正文。切换范围会调用 segments，取消会调用 cancel。读取失败不无限重试，用户可主动刷新；组件卸载关闭 SSE 并停止轮询。文档来源和 `TOOL` 来源分开显示；工具来源的回答/证据卡片必须由后端 `mock` 字段显示“模拟结果，未查询真实业务系统”，查看详情时展示对象 ID、查询时间、fixtureVersion 等信息，不生成文档链接。来源、模型文本与 JSON 均按纯文本渲染，禁止执行来源 HTML。

评估先选冻结题集版本/快照/配置，再估算与启动；切换输入使旧估算失效。结果显示原始指标和逐题记录，不将 HTTP 200 当业务成功。高级表单中使用 API 返回的真实 UUID。实际后端能力尚未部署时会显示明确错误，不将页面可构建等同端到端验收。

集成验收：登录 → 上传 Markdown → 任务成功 → 文档预览 → 提问并查看引用 → 提交反馈 → 撤销来源权限后刷新历史 → 评估题集/冻结/估算/运行/对照。使用受限账号验证管理 API 拒绝，不仅检查按钮是否隐藏。
