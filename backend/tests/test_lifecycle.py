import json
import time
import uuid
import os
from datetime import datetime, timezone
import pytest
from starlette.testclient import TestClient

from app.models import (
    tenant_table,
    app_user_table,
    knowledge_base_table,
    kb_grant_table,
    document_table,
    document_grant_table,
    index_build_table,
    chunk_table,
    source_unit_table,
    qa_run_table,
    run_source_table,
    run_event_table,
    feedback_table,
    audit_event_table,
    deletion_record_table,
    artifact_table,
)
import app.db as app_db
from app.knowledge.artifacts import save_artifact, read_artifact, delete_artifact
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


def test_feedback_lifecycle(client):
    csrf = get_auth_context(client, "admin", "admin123")
    now = datetime.now(timezone.utc)
    tenant_id = "tenant-demo-1"

    kb_id = "kb-fb-1"
    doc_id = "doc-fb-1"
    build_id = "build-fb-1"

    with app_db.engine.begin() as conn:
        conn.execute(knowledge_base_table.insert().values(tenant_id=tenant_id, id=kb_id, name="Feedback KB", status="LIVE", row_version=1, created_at=now, updated_at=now))
        conn.execute(
            kb_grant_table.insert().values(
                tenant_id=tenant_id,
                id=str(uuid.uuid4()),
                kb_id=kb_id,
                principal_type="USER",
                user_id="user-admin-1",
                group_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            document_table.insert().values(
                tenant_id=tenant_id,
                id=doc_id,
                kb_id=kb_id,
                title="Feedback Doc",
                acl_mode="INHERIT",
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
                version_id="ver-fb-1",
                build_no=1,
                config_id="cfg-fb-1",
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
                id="chunk-fb-1",
                build_id=build_id,
                parent_id=None,
                kind="CHILD",
                ordinal=1,
                text_content="User feedback and evaluation loop ensures answer quality.",
                token_count=10,
                content_hash=b"hashfb",
                created_at=now,
                updated_at=now,
            )
        )

    MilvusHybridService.rebuild_from_mysql(tenant_id)

    # 1. Create conversation and run
    conv_resp = client.post(
        "/api/v1/conversations",
        json={"title": "Feedback Conversation", "scope": {"mode": "ALL"}},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = conv_resp.json()["conversationId"]
    segment_id = conv_resp.json()["currentSegmentId"]

    run_resp = client.post(
        f"/api/v1/conversations/{conv_id}/runs",
        json={"question": "如何保证问答质量？", "segmentId": segment_id, "mode": "RAG"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())},
    )
    run_id = run_resp.json()["runId"]

    # Poll run
    for _ in range(60):
        time.sleep(0.1)
        r = client.get(f"/api/v1/runs/{run_id}")
        if r.json()["status"] in ("SUCCEEDED", "COMPLETED", "FAILED"):
            break

    run_detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert run_detail["status"] in ("SUCCEEDED", "COMPLETED")
    assert run_detail["visibility"] == "VISIBLE"

    # 2. Submit feedback
    fb_resp = client.put(
        f"/api/v1/runs/{run_id}/feedback",
        json={"rating": "USEFUL", "issueType": "", "comment": "回答很清晰"},
        headers={"X-CSRF-Token": csrf},
    )
    assert fb_resp.status_code == 200
    assert fb_resp.json()["status"] == "OPEN"

    # 3. Update feedback (upsert)
    fb_update_resp = client.put(
        f"/api/v1/runs/{run_id}/feedback",
        json={"rating": "USELESS", "issueType": "ANSWER_ERROR", "comment": "回答有误"},
        headers={"X-CSRF-Token": csrf},
    )
    assert fb_update_resp.status_code == 200

    # 4. Admin feedback list & detail
    admin_list = client.get("/api/v1/admin/feedback")
    assert admin_list.status_code == 200
    assert len(admin_list.json()) >= 1
    found_item = next(item for item in admin_list.json() if item.get("runId") == run_id)
    fb_id = found_item["id"]
    assert found_item["rating"] == "USELESS"
    assert found_item["issueType"] == "ANSWER_ERROR"

    admin_detail = client.get(f"/api/v1/admin/feedback/{fb_id}")
    assert admin_detail.status_code == 200
    assert admin_detail.json()["comment"] == "回答有误"

    # 5. Admin feedback update status
    patch_resp = client.patch(
        f"/api/v1/admin/feedback/{fb_id}",
        json={"status": "RESOLVED", "resolution": "已修复知识库数据"},
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "RESOLVED"

    # Verify updated detail
    admin_detail2 = client.get(f"/api/v1/admin/feedback/{fb_id}")
    assert admin_detail2.json()["status"] == "RESOLVED"
    assert admin_detail2.json()["resolution"] == "已修复知识库数据"


def test_acl_optimistic_locking_and_audit(client):
    csrf = get_auth_context(client, "admin", "admin123")
    now = datetime.now(timezone.utc)
    tenant_id = "tenant-demo-1"

    kb_id = "kb-acl-1"
    doc_id = "doc-acl-1"

    with app_db.engine.begin() as conn:
        conn.execute(knowledge_base_table.insert().values(tenant_id=tenant_id, id=kb_id, name="ACL KB", status="LIVE", row_version=1, created_at=now, updated_at=now))
        conn.execute(
            document_table.insert().values(
                tenant_id=tenant_id,
                id=doc_id,
                kb_id=kb_id,
                title="ACL Document",
                acl_mode="RESTRICT",
                status="LIVE",
                active_build_id=None,
                lifecycle_epoch=0,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )

    # 1. KB ACL conflict on wrong expectedVersion
    bad_kb_resp = client.put(
        f"/api/v1/admin/knowledge-bases/{kb_id}/acl",
        json={"expectedVersion": 99, "readers": [{"type": "USER", "userId": "user-admin-1"}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_kb_resp.status_code == 409
    assert bad_kb_resp.json()["code"] == "CONCURRENT_CONFLICT"

    # 2. KB ACL success
    kb_resp = client.put(
        f"/api/v1/admin/knowledge-bases/{kb_id}/acl",
        json={"expectedVersion": 1, "readers": [{"type": "USER", "userId": "user-admin-1"}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert kb_resp.status_code == 200
    assert kb_resp.json()["rowVersion"] == 2

    # 3. Document ACL conflict on wrong expectedVersion
    bad_doc_resp = client.put(
        f"/api/v1/admin/documents/{doc_id}/acl",
        json={"expectedVersion": 99, "mode": "RESTRICT", "readers": [{"type": "USER", "userId": "user-admin-1"}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_doc_resp.status_code == 409

    # 4. Document ACL success
    doc_resp = client.put(
        f"/api/v1/admin/documents/{doc_id}/acl",
        json={"expectedVersion": 1, "mode": "RESTRICT", "readers": [{"type": "USER", "userId": "user-admin-1"}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert doc_resp.status_code == 200
    assert doc_resp.json()["rowVersion"] == 2
    assert doc_resp.json()["mode"] == "RESTRICT"

    # 5. Check audit events generated
    with app_db.engine.begin() as conn:
        audits = conn.execute(
            audit_event_table.select().where(audit_event_table.c.tenant_id == tenant_id)
        ).fetchall()
        actions = [a.action for a in audits]
        assert "ACL_UPDATE" in actions


def test_revocation_desensitization(client):
    csrf = get_auth_context(client, "admin", "admin123")
    now = datetime.now(timezone.utc)
    tenant_id = "tenant-demo-1"

    kb_id = "kb-revoke-1"
    doc_id = "doc-revoke-1"
    build_id = "build-revoke-1"

    with app_db.engine.begin() as conn:
        conn.execute(knowledge_base_table.insert().values(tenant_id=tenant_id, id=kb_id, name="Revoke KB", status="LIVE", row_version=1, created_at=now, updated_at=now))
        conn.execute(
            kb_grant_table.insert().values(
                tenant_id=tenant_id,
                id=str(uuid.uuid4()),
                kb_id=kb_id,
                principal_type="USER",
                user_id="user-admin-1",
                group_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            document_table.insert().values(
                tenant_id=tenant_id,
                id=doc_id,
                kb_id=kb_id,
                title="Confidential Product Roadmap",
                acl_mode="INHERIT",
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
                version_id="ver-revoke-1",
                build_no=1,
                config_id="cfg-revoke-1",
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
                id="chunk-revoke-1",
                build_id=build_id,
                parent_id=None,
                kind="CHILD",
                ordinal=1,
                text_content="Confidential project details: Secret Project Apollo launches in Q4.",
                token_count=12,
                content_hash=b"hashrevoke",
                created_at=now,
                updated_at=now,
            )
        )

    MilvusHybridService.rebuild_from_mysql(tenant_id)

    # 1. Create conversation and run
    conv_resp = client.post(
        "/api/v1/conversations",
        json={"title": "Project Apollo Query", "scope": {"mode": "ALL"}},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = conv_resp.json()["conversationId"]
    segment_id = conv_resp.json()["currentSegmentId"]

    run_resp = client.post(
        f"/api/v1/conversations/{conv_id}/runs",
        json={"question": "Apollo 项目什么时候发布？", "segmentId": segment_id, "mode": "RAG"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())},
    )
    run_id = run_resp.json()["runId"]

    for _ in range(60):
        time.sleep(0.1)
        r = client.get(f"/api/v1/runs/{run_id}")
        if r.json()["status"] in ("SUCCEEDED", "COMPLETED", "FAILED"):
            break

    run_data = client.get(f"/api/v1/runs/{run_id}").json()
    assert run_data["status"] in ("SUCCEEDED", "COMPLETED")
    assert run_data["visibility"] == "VISIBLE"
    assert "Apollo" in run_data["question"]
    assert "answer" in run_data

    # 2. Revoke KB grant by updating KB ACL to an unrelated user
    client.put(
        f"/api/v1/admin/knowledge-bases/{kb_id}/acl",
        json={"expectedVersion": 1, "readers": [{"type": "USER", "userId": "user-disabled-1"}]},
        headers={"X-CSRF-Token": csrf},
    )

    # 3. Verify Run desensitization: visibility == "HIDDEN", NO question, NO answer
    run_after_revoke = client.get(f"/api/v1/runs/{run_id}").json()
    assert run_after_revoke["visibility"] == "HIDDEN"
    assert "question" not in run_after_revoke
    assert "answer" not in run_after_revoke
    assert "notice" in run_after_revoke

    # 4. Verify conversation title is masked (None)
    conv_after = client.get(f"/api/v1/conversations/{conv_id}").json()
    assert conv_after["title"] is None

    conv_list = client.get("/api/v1/conversations").json()
    target_conv = next(c for c in conv_list if c["id"] == conv_id)
    assert target_conv["title"] is None

    # 5. Verify evidence endpoint returns 404
    ev_resp = client.get(f"/api/v1/runs/{run_id}/evidence/E1")
    assert ev_resp.status_code == 404

    # 6. Verify feedback submission on hidden run fails with 404
    fb_fail = client.put(
        f"/api/v1/runs/{run_id}/feedback",
        json={"rating": "USEFUL"},
        headers={"X-CSRF-Token": csrf},
    )
    assert fb_fail.status_code == 404


def test_document_lifecycle_trash_restore_purge(client):
    csrf = get_auth_context(client, "admin", "admin123")
    now = datetime.now(timezone.utc)
    tenant_id = "tenant-demo-1"

    kb_id = "kb-life-1"
    doc_id = "doc-life-1"
    build_id = "build-life-1"

    # Save physical artifact file
    dummy_bytes = b"Artifact content for lifecycle verification test."
    storage_key, _, _ = save_artifact(tenant_id, "test_doc.txt", dummy_bytes)

    with app_db.engine.begin() as conn:
        conn.execute(knowledge_base_table.insert().values(tenant_id=tenant_id, id=kb_id, name="Lifecycle KB", status="LIVE", row_version=1, created_at=now, updated_at=now))
        conn.execute(
            kb_grant_table.insert().values(
                tenant_id=tenant_id,
                id=str(uuid.uuid4()),
                kb_id=kb_id,
                principal_type="USER",
                user_id="user-admin-1",
                group_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            document_table.insert().values(
                tenant_id=tenant_id,
                id=doc_id,
                kb_id=kb_id,
                title="Lifecycle Target Document",
                acl_mode="INHERIT",
                status="LIVE",
                active_build_id=build_id,
                lifecycle_epoch=0,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            artifact_table.insert().values(
                tenant_id=tenant_id,
                id=str(uuid.uuid4()),
                document_id=doc_id,
                kind="SOURCE",
                storage_key=storage_key,
                sha256=b"art_hash",
                size_bytes=len(dummy_bytes),
                status="READY",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            index_build_table.insert().values(
                tenant_id=tenant_id,
                id=build_id,
                document_id=doc_id,
                version_id="ver-life-1",
                build_no=1,
                config_id="cfg-life-1",
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
                id="chunk-life-1",
                build_id=build_id,
                parent_id=None,
                kind="CHILD",
                ordinal=1,
                text_content="Lifecycle testing: trash, restore, and local purge mechanics.",
                token_count=10,
                content_hash=b"hashlife",
                created_at=now,
                updated_at=now,
            )
        )

    MilvusHybridService.rebuild_from_mysql(tenant_id)

    # 1. Search works initially
    s_resp = client.get("/api/v1/search?query=Lifecycle")
    assert any(e["chunkId"] == "chunk-life-1" for e in s_resp.json()["evidence"])

    # 2. Trash document
    trash_resp = client.post(
        f"/api/v1/admin/documents/{doc_id}/trash",
        json={"expectedVersion": 1},
        headers={"X-CSRF-Token": csrf},
    )
    assert trash_resp.status_code == 200
    assert trash_resp.json()["status"] == "TRASHED"

    # Search should no longer return trashed document
    s_trashed = client.get("/api/v1/search?query=Lifecycle")
    assert not any(e["chunkId"] == "chunk-life-1" for e in s_trashed.json()["evidence"])

    # 3. Restore document
    # Note: doc row_version was bumped to 2 on trash
    restore_resp = client.post(
        f"/api/v1/admin/documents/{doc_id}/restore",
        json={"expectedVersion": 2},
        headers={"X-CSRF-Token": csrf},
    )
    assert restore_resp.status_code == 200
    assert restore_resp.json()["status"] == "LIVE"

    # Search works again after restore
    s_restored = client.get("/api/v1/search?query=Lifecycle")
    assert any(e["chunkId"] == "chunk-life-1" for e in s_restored.json()["evidence"])

    # 4. Trash again before purge (purge requires TRASHED state)
    # doc row_version bumped to 3 on restore
    client.post(
        f"/api/v1/admin/documents/{doc_id}/trash",
        json={"expectedVersion": 3},
        headers={"X-CSRF-Token": csrf},
    )

    # 5. Purge document
    # doc row_version bumped to 4 on second trash
    purge_resp = client.post(
        f"/api/v1/admin/documents/{doc_id}/purge",
        json={"expectedVersion": 4},
        headers={"X-CSRF-Token": csrf},
    )
    assert purge_resp.status_code == 200
    assert purge_resp.json()["status"] == "PURGED"

    # Verify physical file deleted
    with pytest.raises(Exception):
        read_artifact(storage_key)

    # Verify MySQL data scrubbed
    with app_db.engine.begin() as conn:
        doc_row = conn.execute(document_table.select().where(document_table.c.id == doc_id)).first()
        assert doc_row.status == "PURGED"
        assert doc_row.title is None
        assert doc_row.active_build_id is None

        chunks = conn.execute(chunk_table.select().where(chunk_table.c.build_id == build_id)).fetchall()
        for c in chunks:
            assert c.text_content == ""

        del_rec = conn.execute(deletion_record_table.select().where(deletion_record_table.c.document_id == doc_id)).first()
        assert del_rec is not None
        assert del_rec.status == "COMPLETE"
