import json
import time
import uuid
from datetime import datetime, timezone
import pytest
from starlette.testclient import TestClient

from app.models import (
    knowledge_base_table,
    document_table,
    index_build_table,
    chunk_table,
    source_unit_table,
    qa_run_table,
)
import app.db as app_db
from app.qa.model_gateway import ModelGateway
from app.retrieval.milvus_service import MilvusHybridService


def get_auth_context(client: TestClient, login_name: str = "admin", password: str = "admin123"):
    csrf_resp = client.get("/api/v1/auth/csrf")
    csrf_token = csrf_resp.json()["csrfToken"]
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"tenantCode": "demo", "loginName": login_name, "password": password},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert login_resp.status_code == 200
    csrf_after = client.get("/api/v1/auth/csrf")
    return csrf_after.json()["csrfToken"]


@pytest.fixture(autouse=True)
def reset_agent_mock():
    ModelGateway._agent_mock_handler = None
    yield
    ModelGateway._agent_mock_handler = None


def test_agent_single_tool_search_and_answer(client):
    csrf = get_auth_context(client, "admin", "admin123")
    now = datetime.now(timezone.utc)
    tenant_id = "tenant-demo-1"

    # Setup a live KB, Doc, Chunk
    kb_id = "kb-agent-1"
    doc_id = "doc-agent-1"
    build_id = "build-agent-1"

    with app_db.engine.begin() as conn:
        conn.execute(knowledge_base_table.insert().values(tenant_id=tenant_id, id=kb_id, name="Agent KB", status="LIVE", row_version=1, created_at=now, updated_at=now))
        conn.execute(
            document_table.insert().values(
                tenant_id=tenant_id,
                id=doc_id,
                kb_id=kb_id,
                title="Agent Architecture",
                acl_mode="RESTRICT",
                status="LIVE",
                active_build_id=build_id,
                lifecycle_epoch=0,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            index_build_table.insert().values(
                tenant_id=tenant_id,
                id=build_id,
                document_id=doc_id,
                version_id="ver-agent-1",
                build_no=1,
                config_id="cfg-agent-1",
                status="READY",
                capabilities={},
                fence=0,
                published_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            chunk_table.insert().values(
                tenant_id=tenant_id,
                id="chunk-agent-1",
                build_id=build_id,
                parent_id=None,
                kind="CHILD",
                ordinal=1,
                text_content="LangGraph StateGraph coordinates agent tools and multi-round reasoning.",
                token_count=12,
                content_hash=b"hashagent",
                created_at=now,
                updated_at=now,
            )
        )

    MilvusHybridService.rebuild_from_mysql(tenant_id)

    # 1. Create conversation
    conv_resp = client.post(
        "/api/v1/conversations",
        json={"title": "Agent Test Conv", "scope": {"mode": "ALL"}},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = conv_resp.json()["conversationId"]

    # 2. Submit AGENT run
    run_resp = client.post(
        f"/api/v1/conversations/{conv_id}/runs",
        json={"question": "StateGraph 如何工作？", "mode": "AGENT"},
        headers={"X-CSRF-Token": csrf},
    )
    assert run_resp.status_code == 202
    run_id = run_resp.json()["runId"]

    # 3. Poll until completion
    run_data = None
    for _ in range(60):
        time.sleep(0.1)
        r = client.get(f"/api/v1/runs/{run_id}")
        run_data = r.json()
        if run_data["status"] in ("SUCCEEDED", "COMPLETED", "FAILED"):
            break

    assert run_data is not None
    assert run_data["status"] in ("SUCCEEDED", "COMPLETED")
    assert run_data["visibility"] == "VISIBLE"
    assert len(run_data["evidence"]) > 0
    assert len(run_data["claims"]) > 0


def test_agent_read_section_tool(client):
    csrf = get_auth_context(client, "admin", "admin123")
    now = datetime.now(timezone.utc)
    tenant_id = "tenant-demo-1"

    kb_id = "kb-agent-sec"
    doc_id = "doc-agent-sec"
    build_id = "build-agent-sec"
    unit_id = "sec-unit-1"

    with app_db.engine.begin() as conn:
        conn.execute(knowledge_base_table.insert().values(tenant_id=tenant_id, id=kb_id, name="Sec KB", status="LIVE", row_version=1, created_at=now, updated_at=now))
        conn.execute(
            document_table.insert().values(
                tenant_id=tenant_id,
                id=doc_id,
                kb_id=kb_id,
                title="Detailed Section Spec",
                acl_mode="RESTRICT",
                status="LIVE",
                active_build_id=build_id,
                lifecycle_epoch=0,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            index_build_table.insert().values(
                tenant_id=tenant_id,
                id=build_id,
                document_id=doc_id,
                version_id="ver-agent-sec",
                build_no=1,
                config_id="cfg-agent-sec",
                status="READY",
                capabilities={},
                fence=0,
                published_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            source_unit_table.insert().values(
                tenant_id=tenant_id,
                id=unit_id,
                build_id=build_id,
                document_id=doc_id,
                unit_key="sec-1",
                kind="PARAGRAPH",
                text_content="Section detailed spec text for deep reading in LangGraph Agent.",
                location={},
                machine_generated=False,
                artifact_id=None,
                content_hash=b"sec_hash",
                created_at=now,
                updated_at=now,
            )
        )

    # Inject mock handler to test read_section tool
    def mock_agent(messages):
        # If tool response received, finish with claim
        if any(m.get("role") == "tool" for m in messages):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "status": "COMPLETED",
                    "claims": [
                        {
                            "id": "c1",
                            "text": "章节精读完成",
                            "evidence": [{"evidenceId": "E1", "start": 0, "end": 10}],
                        }
                    ],
                    "gaps": [],
                    "conflicts": [],
                }, ensure_ascii=False),
                "tool_calls": [],
            }
        # First round: call read_section
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_sec_1",
                    "type": "function",
                    "function": {
                        "name": "read_section",
                        "arguments": json.dumps({
                            "documentId": doc_id,
                            "buildId": build_id,
                            "sectionId": unit_id,
                        }),
                    },
                }
            ],
        }

    ModelGateway._agent_mock_handler = mock_agent

    conv_resp = client.post(
        "/api/v1/conversations",
        json={"title": "Agent Read Section", "scope": {"mode": "ALL"}},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = conv_resp.json()["conversationId"]

    run_resp = client.post(
        f"/api/v1/conversations/{conv_id}/runs",
        json={"question": "精读章节1", "mode": "AGENT"},
        headers={"X-CSRF-Token": csrf},
    )
    run_id = run_resp.json()["runId"]

    run_data = None
    for _ in range(60):
        time.sleep(0.1)
        r = client.get(f"/api/v1/runs/{run_id}")
        run_data = r.json()
        if run_data["status"] in ("SUCCEEDED", "COMPLETED", "FAILED"):
            break

    assert run_data is not None
    assert run_data["status"] in ("SUCCEEDED", "COMPLETED")
    assert any(e.get("unitId") == unit_id for e in run_data["evidence"])


def test_agent_repeated_calls_termination(client):
    csrf = get_auth_context(client, "admin", "admin123")

    # Mock handler that always calls the exact same tool
    def mock_repeated(messages):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_rep_{len(messages)}",
                    "type": "function",
                    "function": {
                        "name": "search_knowledge",
                        "arguments": json.dumps({"query": "same query", "topK": 5}),
                    },
                }
            ],
        }

    ModelGateway._agent_mock_handler = mock_repeated

    conv_resp = client.post(
        "/api/v1/conversations",
        json={"title": "Repeated Calls Test"},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = conv_resp.json()["conversationId"]

    run_resp = client.post(
        f"/api/v1/conversations/{conv_id}/runs",
        json={"question": "测试循环调用", "mode": "AGENT"},
        headers={"X-CSRF-Token": csrf},
    )
    run_id = run_resp.json()["runId"]

    run_data = None
    for _ in range(60):
        time.sleep(0.1)
        r = client.get(f"/api/v1/runs/{run_id}")
        run_data = r.json()
        if run_data["status"] in ("SUCCEEDED", "COMPLETED", "FAILED"):
            break

    assert run_data is not None
    assert run_data["status"] == "FAILED"
    assert run_data["terminalReason"] == "REPEATED_CALLS"


def test_agent_unknown_tool_handled(client):
    csrf = get_auth_context(client, "admin", "admin123")

    # Round 1: call unknown tool. Round 2: finish
    def mock_unknown(messages):
        if any(m.get("role") == "tool" for m in messages):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "status": "NO_EVIDENCE",
                    "claims": [],
                    "gaps": [],
                    "conflicts": [],
                }),
                "tool_calls": [],
            }
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_bad_1",
                    "type": "function",
                    "function": {
                        "name": "non_existent_tool",
                        "arguments": json.dumps({"param": 1}),
                    },
                }
            ],
        }

    ModelGateway._agent_mock_handler = mock_unknown

    conv_resp = client.post(
        "/api/v1/conversations",
        json={"title": "Unknown Tool Test"},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = conv_resp.json()["conversationId"]

    run_resp = client.post(
        f"/api/v1/conversations/{conv_id}/runs",
        json={"question": "测试未知工具", "mode": "AGENT"},
        headers={"X-CSRF-Token": csrf},
    )
    run_id = run_resp.json()["runId"]

    run_data = None
    for _ in range(60):
        time.sleep(0.1)
        r = client.get(f"/api/v1/runs/{run_id}")
        run_data = r.json()
        if run_data["status"] in ("SUCCEEDED", "COMPLETED", "FAILED"):
            break

    assert run_data is not None
    assert run_data["status"] in ("SUCCEEDED", "COMPLETED", "NO_EVIDENCE")
    assert run_data["terminalReason"] == "COMPLETED"
