import os
import time
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv
import httpx

rag_root = Path(__file__).resolve().parent.parent
load_dotenv(rag_root / ".runtime/demo.env")

BASE_URL = os.getenv("RAG_BASE_URL", "http://127.0.0.1:8080")
SEED_PASSWORD = os.getenv("RAG_SEED_PASSWORD", "")

client = httpx.Client(base_url=BASE_URL, timeout=60.0)

# Step 1: CSRF & Login
csrf_init = client.get("/api/v1/auth/csrf").json()["csrfToken"]
login_resp = client.post(
    "/api/v1/auth/login",
    json={"tenantCode": "demo", "loginName": "admin", "password": SEED_PASSWORD},
    headers={"X-CSRF-Token": csrf_init},
)
print("Login status:", login_resp.status_code, login_resp.json())
assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"

csrf_token = client.get("/api/v1/auth/csrf").json()["csrfToken"]
headers = {"X-CSRF-Token": csrf_token}

# Step 2: Create Knowledge Base via /api/v1/admin/knowledge-bases
kb_name = f"高并发架构规范库-{int(time.time())}"
kb_resp = client.post(
    "/api/v1/admin/knowledge-bases",
    json={"name": kb_name, "readers": [{"principalType": "ALL"}]},
    headers=headers,
)
print("Create KB status:", kb_resp.status_code)
assert kb_resp.status_code == 200 or kb_resp.status_code == 201
kb_data = kb_resp.json()
kb_id = kb_data["id"]
print(f"Created Knowledge Base: ID={kb_id}, Name={kb_name}")

# Step 3: Upload Document via /api/v1/admin/documents
doc_content = """# 高并发架构与容灾规范

## 核心分流与限流策略
在高并发场景下，企业网关接入层必须采用自适应动态限流算法。针对核心 API，单节点峰值 QPS 阈值统一设定为 5000，当超过该阈值时自动触发熔断降级。

## 数据库主从复制与双活容灾
生产环境 MySQL 必须采用半同步复制模式（Semi-sync Replication），数据丢失指标 RPO 目标严格设定为 0，故障转移时间 RTO 严格限制在 30 秒以内。跨机房双活架构下，采用 Paxos/Raft 协议同步核心账本数据。

## 向量检索与多路召回
知识库采用 Milvus 向量引擎进行混合检索，结合 Dense Vector（余弦相似度）与 BM25 稀疏向量，倒数排名融合 RRFRanker 参数 k 统一设定为 60。
"""

files = {
    "file": ("architecture_spec.md", doc_content.encode("utf-8"), "text/markdown"),
}
data = {
    "kbId": kb_id,
    "buildConfig": json.dumps({"configId": "cfg-default-retrieval"}),
}
upload_headers = dict(headers)
upload_headers["Idempotency-Key"] = str(uuid.uuid4())

upload_resp = client.post(
    "/api/v1/admin/documents",
    files=files,
    data=data,
    headers=upload_headers,
)
print("Upload status:", upload_resp.status_code, upload_resp.json())
assert upload_resp.status_code == 202
up_data = upload_resp.json()
doc_id = up_data["documentId"]
build_id = up_data.get("buildId")
print(f"Uploaded Document: ID={doc_id}, BuildID={build_id}")

# Step 4: Poll for Build Completion
print("Waiting for indexing and building to complete...")
active_build = None
for i in range(40):
    time.sleep(1)
    doc_detail = client.get(f"/api/v1/documents/{doc_id}", headers=headers).json()
    status = doc_detail.get("status")
    active_build = doc_detail.get("activeBuildId")
    print(f"Polling doc {i+1}: status={status}, activeBuildId={active_build}")
    if status == "LIVE" and active_build:
        print(f"Build completed! Document is LIVE with build: {active_build}")
        break
else:
    raise RuntimeError("Timed out waiting for document indexing")

# Step 5: Create Conversation
conv_resp = client.post(
    "/api/v1/conversations",
    json={"title": "架构规范问答测试", "scope": {"mode": "SELECTED", "kbIds": [kb_id]}},
    headers=headers,
)
assert conv_resp.status_code == 201
conv_id = conv_resp.json()["conversationId"]
print(f"\nCreated Conversation: ID={conv_id}")

# Step 6: Submit First Question
question1 = "生产环境 MySQL 采用什么复制模式？RPO 和 RTO 指标各是多少？"
print(f"\n=======================================================")
print(f"  [QUESTION 1]: {question1}")
print(f"=======================================================")

run_resp1 = client.post(
    f"/api/v1/conversations/{conv_id}/runs",
    json={"question": question1, "mode": "RAG"},
    headers=headers,
)
print("Submit Run 1 status:", run_resp1.status_code, run_resp1.json())
assert run_resp1.status_code == 202
run_id1 = run_resp1.json()["runId"]

# Poll Run 1
run1_data = None
for i in range(40):
    time.sleep(1)
    r = client.get(f"/api/v1/runs/{run_id1}", headers=headers)
    run1_data = r.json()
    status = run1_data.get("status")
    stage = run1_data.get("stage")
    print(f"Polling Run 1 ({i+1}s): status={status}, stage={stage}")
    if status in ("COMPLETED", "SUCCEEDED", "FAILED"):
        break

assert run1_data is not None and run1_data.get("status") in ("COMPLETED", "SUCCEEDED"), run1_data
assert run1_data.get("evidence"), run1_data
assert run1_data.get("claims"), run1_data

print("\n--- [RUN 1 RESULT] ---")
print("Run Status:", run1_data.get("status"))
print("Terminal Reason:", run1_data.get("terminalReason"))
print("Verification:", run1_data.get("verification"))
print("\n>>> Final Answer:")
print(run1_data.get("answer"))

print(f"\n>>> Retrieved Evidence Chunks ({len(run1_data.get('evidence', []))} hits):")
for ev in run1_data.get("evidence", []):
    print(f"  [{ev['evidenceId']}] (Score: {ev.get('score')}) => {ev['text']}")

print(f"\n>>> Extracted Claims & Citations ({len(run1_data.get('claims', []))} claims):")
for cl in run1_data.get("claims", []):
    print(f"  - Claim: {cl.get('text')} | Evidence: {cl.get('evidence')}")

# Step 7: Submit Second Question (Milvus retrieval params)
question2 = "知识库系统采用什么向量引擎进行检索？混合检索的融合参数 k 统一设定为多少？"
print(f"\n=======================================================")
print(f"  [QUESTION 2]: {question2}")
print(f"=======================================================")

run_resp2 = client.post(
    f"/api/v1/conversations/{conv_id}/runs",
    json={"question": question2, "mode": "RAG"},
    headers=headers,
)
assert run_resp2.status_code == 202
run_id2 = run_resp2.json()["runId"]

run2_data = None
for i in range(40):
    time.sleep(1)
    r = client.get(f"/api/v1/runs/{run_id2}", headers=headers)
    run2_data = r.json()
    status = run2_data.get("status")
    stage = run2_data.get("stage")
    print(f"Polling Run 2 ({i+1}s): status={status}, stage={stage}")
    if status in ("COMPLETED", "SUCCEEDED", "FAILED"):
        break

assert run2_data is not None and run2_data.get("status") in ("COMPLETED", "SUCCEEDED"), run2_data
assert run2_data.get("evidence"), run2_data
assert run2_data.get("claims"), run2_data

print("\n--- [RUN 2 RESULT] ---")
print("Run Status:", run2_data.get("status"))
print("Terminal Reason:", run2_data.get("terminalReason"))
print("Verification:", run2_data.get("verification"))
print("\n>>> Final Answer:")
print(run2_data.get("answer"))

print(f"\n>>> Retrieved Evidence Chunks ({len(run2_data.get('evidence', []))} hits):")
for ev in run2_data.get("evidence", []):
    print(f"  [{ev['evidenceId']}] (Score: {ev.get('score')}) => {ev['text']}")

print(f"\n>>> Extracted Claims & Citations ({len(run2_data.get('claims', []))} claims):")
for cl in run2_data.get("claims", []):
    print(f"  - Claim: {cl.get('text')} | Evidence: {cl.get('evidence')}")

print("\n=======================================================")
print("  ALL LIVE REAL DATA TESTS PASSED SUCCESSFULLY!")
print("=======================================================")
