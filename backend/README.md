# RAG Python Backend (FastAPI + Milvus + LiteLLM)

企业内部知识库 RAG 系统的 Python 3.12 模块化单体后端。

## 技术栈与架构

- **Web 框架**: FastAPI / Starlette, Uvicorn, Pydantic
- **数据存储**: MySQL 8.4 (关系数据 / 权限 / 生命周期 / 任务审计), SQLAlchemy 2.0
- **向量检索与混合检索**: Milvus 2.5 (Dense Vector + Sparse BM25 / RRF 混合融合)
- **文档解析**: Docling 独立服务
- **大模型网关**: LiteLLM AI Gateway (持有真实供应商 API 密钥，对外统一提供 OpenAI 兼容端点)
- **问答与编排**: LangChain / LangGraph 状态机，支持严格模式、证据绑定与事实断言校验

## 模块划分

```
backend/app/
├── config.py             # 配置加载与校验
├── db.py                 # 数据库引擎与连接管理
├── errors.py             # 统一异常处理与敏感信息脱敏
├── models.py             # SQLAlchemy Core 表模型 (与生产 MySQL 模式 100% 兼容)
├── main.py               # FastAPI 应用入口与中间件
├── identity/             # 身份认证、租户隔离、CSRF 与管理接口
├── knowledge/            # 文档解析、入库、版本生命周期与审计
├── retrieval/            # Milvus 混合检索与 RRF 融合
├── qa/                   # 问答服务、LangGraph Agent、断言验证与模型网关
└── evaluation/           # 评测体系与消融实验指标计算
```

## 本地开发与运行

### 1. 虚拟环境与依赖
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
```

### 2. 运行服务
```powershell
$env:PYTHONPATH = "$PWD"
.\.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 3. 执行测试
```powershell
$env:PYTHONPATH = "$PWD"
.\.venv\Scripts\pytest -v
```
