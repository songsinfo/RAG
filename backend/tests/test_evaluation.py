import json
import os
import pytest
from datetime import datetime, timezone
from sqlalchemy import select, and_, delete

from app.config import settings
from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor
from app.evaluation.metrics import EvaluationMetrics
from app.evaluation.catalog import EvaluationCatalog, hash_payload, obj, maps
from app.evaluation.runner import EvaluationRunner, check_no_model, number
from app.models import (
    tenant_table,
    app_user_table,
    knowledge_base_table,
    document_table,
    document_version_table,
    index_build_table,
    source_unit_table,
    chunk_table,
    eval_dataset_version_table,
    eval_case_table,
    eval_gold_table,
    eval_snapshot_table,
    eval_snapshot_document_table,
    eval_run_table,
    eval_result_table,
    eval_review_table,
    config_revision_table,
)


# ==============================================================================
# 1. Algorithmic Metric Tests (Java Parity)
# ==============================================================================

def test_one_of_three_facts_is_a_hit_but_only_third_recall():
    m = EvaluationMetrics.retrieval(
        groups=[{"a"}, {"b"}, {"c"}],
        ranked=[{"a"}],
        k=5,
    )
    assert m["hit"] == 1.0
    assert abs(m["recall"] - 1.0 / 3) < 1e-6
    assert m["mrr"] == 1.0


def test_duplicate_chunks_and_alternative_evidence_do_not_invent_coverage():
    m = EvaluationMetrics.retrieval(
        groups=[{"a", "a2"}, {"b"}],
        ranked=[{"a2"}, {"a"}, {"a2"}],
        k=5,
    )
    assert m["recall"] == 0.5


def test_empty_gold_is_not_perfect_and_failed_answerable_question_is_zero():
    assert EvaluationMetrics.retrieval(groups=[], ranked=[], k=5)["recall"] is None
    assert EvaluationMetrics.retrieval(groups=[{"a"}], ranked=[], k=5)["recall"] == 0.0


def test_zero_claims_cannot_score_perfect_faithfulness():
    assert EvaluationMetrics.ratio(0, 0) is None
    assert EvaluationMetrics.ratio(5, 10) == 0.5


def test_dcg_uses_graded_relevance_and_rejects_unknown_grades():
    assert abs(EvaluationMetrics.ndcg([3, 2, 0], [3, 2, 0], 3) - 1.0) < 1e-9
    assert EvaluationMetrics.ndcg([0, 2, 3], [3, 2, 0], 3) < 1.0
    assert EvaluationMetrics.ndcg([0], [0], 1) is None

    with pytest.raises(ValueError, match="Grade must be 0..3"):
        EvaluationMetrics.ndcg([-1], [3], 1)

    with pytest.raises(ValueError, match="Grade must be 0..3"):
        EvaluationMetrics.ndcg([4], [3], 1)


def test_retrieval_result_includes_ndcg_for_the_same_ranked_candidates():
    m = EvaluationMetrics.retrieval(
        groups=[{"a"}, {"b"}],
        ranked=[{"b"}, {"a"}],
        k=2,
        ranked_grades=[1, 3],
        ideal_grades=[3, 1],
    )
    assert m["recall"] == 1.0
    assert m["ndcg"] is not None
    assert 0.0 < m["ndcg"] < 1.0


def test_nearest_rank_percentile_uses_all_observed_durations():
    assert EvaluationMetrics.percentile([1, 2, 3, 1000], 0.95) == 1000
    assert EvaluationMetrics.percentile([100, 200, 300, 400], 0.5) == 200
    assert EvaluationMetrics.percentile([], 0.5) is None


def test_retrieval_only_permits_bm25_without_model_transforms_or_reranking():
    valid_bm25 = {
        "retrieval": {"mode": "BM25"},
        "query": {"rewrite": False, "hyde": False, "multiQuery": False},
        "post": {"rerank": False, "mmr": False},
    }
    check_no_model(valid_bm25)

    with pytest.raises(ApiException) as exc1:
        check_no_model({"retrieval": {"mode": "DENSE"}})
    assert exc1.value.code == "MODEL_REQUIRED"

    with pytest.raises(ApiException) as exc2:
        check_no_model({"retrieval": {"mode": "BM25"}, "query": {"rewrite": True}})
    assert exc2.value.code == "MODEL_REQUIRED"

    with pytest.raises(ApiException) as exc3:
        check_no_model({"retrieval": {"mode": "BM25"}, "post": {"rerank": True}})
    assert exc3.value.code == "MODEL_REQUIRED"


def test_number_validation():
    assert number("3.14") == 3.14
    assert number(10) == 10.0
    with pytest.raises(ApiException):
        number("abc")
    with pytest.raises(ApiException):
        number("NaN")
    with pytest.raises(ApiException):
        number("Infinity")


def test_deterministic_hash_matches_identical_content():
    m1 = {"caseKey": "K1", "split": "DEV"}
    m2 = {"caseKey": "K1", "split": "DEV"}
    assert hash_payload(m1) == hash_payload(m2)


def test_object_and_map_parsers_validate_structure():
    assert obj(None) == {}
    assert maps(None) == []
    with pytest.raises(ApiException):
        maps("not-a-list")


# ==============================================================================
# 2. Lifecycle, Split Leakage & Anchor Validation Tests
# ==============================================================================

@pytest.fixture
def eval_env():
    """Setup isolated test tenant, admin actor, and dummy seeded documents."""
    t_id = "eval-test-tenant"
    admin_id = "eval-admin-user"
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        # Clean up existing test data
        conn.execute(delete(eval_review_table).where(eval_review_table.c.tenant_id == t_id))
        conn.execute(delete(eval_result_table).where(eval_result_table.c.tenant_id == t_id))
        conn.execute(delete(eval_run_table).where(eval_run_table.c.tenant_id == t_id))
        conn.execute(delete(eval_snapshot_document_table).where(eval_snapshot_document_table.c.tenant_id == t_id))
        conn.execute(delete(eval_snapshot_table).where(eval_snapshot_table.c.tenant_id == t_id))
        conn.execute(delete(eval_gold_table).where(eval_gold_table.c.tenant_id == t_id))
        conn.execute(delete(eval_case_table).where(eval_case_table.c.tenant_id == t_id))
        conn.execute(delete(eval_dataset_version_table).where(eval_dataset_version_table.c.tenant_id == t_id))
        conn.execute(delete(config_revision_table).where(config_revision_table.c.tenant_id == t_id))
        conn.execute(delete(chunk_table).where(chunk_table.c.tenant_id == t_id))
        conn.execute(delete(source_unit_table).where(source_unit_table.c.tenant_id == t_id))
        conn.execute(delete(index_build_table).where(index_build_table.c.tenant_id == t_id))
        conn.execute(delete(document_version_table).where(document_version_table.c.tenant_id == t_id))
        conn.execute(delete(document_table).where(document_table.c.tenant_id == t_id))
        from app.models import kb_grant_table
        conn.execute(delete(kb_grant_table).where(kb_grant_table.c.tenant_id == t_id))
        conn.execute(delete(knowledge_base_table).where(knowledge_base_table.c.tenant_id == t_id))
        conn.execute(delete(app_user_table).where(app_user_table.c.tenant_id == t_id))
        conn.execute(delete(tenant_table).where(tenant_table.c.id == t_id))

        # Insert tenant
        conn.execute(
            tenant_table.insert().values(
                id=t_id,
                code="eval_test",
                name="Eval Test Tenant",
                security_epoch=1,
                active_retrieval_config_id=None,
                created_at=now,
            )
        )
        # Insert admin user
        conn.execute(
            app_user_table.insert().values(
                tenant_id=t_id,
                id=admin_id,
                login_name="eval_admin",
                password_hash="mock",
                is_admin=True,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )
        # Insert operator user
        conn.execute(
            app_user_table.insert().values(
                tenant_id=t_id,
                id="eval_operator_id",
                login_name="operator",
                password_hash="mock",
                is_admin=False,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )
        # Insert knowledge base
        kb_id = "eval-kb-01"
        conn.execute(
            knowledge_base_table.insert().values(
                tenant_id=t_id,
                id=kb_id,
                name="Eval Knowledge Base",
                status="LIVE",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            kb_grant_table.insert().values(
                tenant_id=t_id,
                id="grant-kb-01",
                kb_id=kb_id,
                principal_type="ALL",
                user_id=None,
                group_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        # Insert document & build
        doc_id = "doc-eval-01"
        ver_id = "ver-eval-01"
        build_id = "build-eval-01"
        conn.execute(
            document_table.insert().values(
                tenant_id=t_id,
                id=doc_id,
                kb_id=kb_id,
                title="Evaluation Spec Document",
                acl_mode="INHERIT",
                status="LIVE",
                active_build_id=build_id,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            document_version_table.insert().values(
                tenant_id=t_id,
                id=ver_id,
                document_id=doc_id,
                version_no=1,
                source_artifact_id=None,
                source_hash=b"mock-source-hash-32-bytes-long!!",
                original_name="spec.md",
                media_type="text/markdown",
                created_by=admin_id,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            index_build_table.insert().values(
                tenant_id=t_id,
                id=build_id,
                document_id=doc_id,
                version_id=ver_id,
                build_no=1,
                config_id="cfg-default",
                status="READY",
                embedding_dims=1024,
                capabilities={"bm25": True, "dense": True},
                manifest_hash=b"0" * 32,
                fence=0,
                created_at=now,
                updated_at=now,
            )
        )
        # Insert source_unit with quote
        conn.execute(
            source_unit_table.insert().values(
                tenant_id=t_id,
                id="unit-eval-01",
                build_id=build_id,
                document_id=doc_id,
                unit_key="u1",
                kind="PARAGRAPH",
                text_content="知识库系统采用结构化分块策略保留Markdown标题层级，提供端到端严谨召回保证。",
                location={},
                machine_generated=False,
                content_hash=b"0" * 32,
                created_at=now,
                updated_at=now,
            )
        )
        # Insert chunk with quote
        chunk_id = "chunk-eval-01"
        chunk_text = "知识库系统采用结构化分块策略保留Markdown标题层级，提供端到端严谨召回保证。"
        conn.execute(
            chunk_table.insert().values(
                tenant_id=t_id,
                id=chunk_id,
                build_id=build_id,
                parent_id=None,
                kind="PARAGRAPH",
                ordinal=1,
                text_content=chunk_text,
                token_count=50,
                content_hash=b"0" * 32,
                created_at=now,
                updated_at=now,
            )
        )
        conn.commit()

    from app.retrieval.milvus_service import MilvusHybridService
    MilvusHybridService.upsert_chunks(
        tenant_id=t_id,
        build_id=build_id,
        chunks=[{"id": chunk_id, "text_content": chunk_text}],
        kb_id=kb_id,
        doc_id=doc_id,
        version_id=ver_id,
    )

    admin = Actor(tenant_id=t_id, user_id=admin_id, admin=True)
    return {
        "admin": admin,
        "doc_id": doc_id,
        "ver_id": ver_id,
        "quote": "结构化分块策略保留Markdown标题层级",
    }


def test_dataset_lifecycle_and_freeze_validation(eval_env):
    admin = eval_env["admin"]
    doc_id = eval_env["doc_id"]
    ver_id = eval_env["ver_id"]
    quote = eval_env["quote"]

    # 1. Create dataset
    ds = EvaluationCatalog.create_dataset(admin, {"name": "Smoke Dataset"})
    assert ds["name"] == "Smoke Dataset"
    assert ds["revision"] == 1
    assert ds["status"] == "DRAFT"
    ds_id = ds["id"]

    # 2. Import cases
    cases_payload = [
        {
            "caseKey": "case-01",
            "groupKey": "grp-01",
            "split": "DEV",
            "category": "FACT",
            "question": {"text": "系统分块策略是什么？"},
            "persona": {"loginName": "operator"},
            "expected": {
                "status": "ANSWER",
                "evidenceGroups": [
                    {
                        "fact": "分块策略",
                        "alternatives": [
                            {
                                "documentId": doc_id,
                                "versionId": ver_id,
                                "quote": quote,
                                "relevanceGrade": 3,
                            }
                        ],
                    }
                ],
            },
            "annotationStatus": "DRAFT",
        }
    ]
    res = EvaluationCatalog.save_cases(admin, ds_id, cases_payload)
    assert res["count"] == 1

    # 3. Attempt freeze with DRAFT annotationStatus -> should fail with ANNOTATION_REQUIRED
    with pytest.raises(ApiException) as exc:
        EvaluationCatalog.freeze(admin, ds_id)
    assert exc.value.code == "ANNOTATION_REQUIRED"

    # 4. Attempt import with split leakage -> should fail with SPLIT_LEAKAGE
    leak_case = {
        "caseKey": "case-02-leak",
        "groupKey": "grp-01",  # Same groupKey as case-01
        "split": "TEST",       # Different split!
        "category": "FACT",
        "question": {"text": "系统分块策略在盲测里的表现？"},
        "persona": {"loginName": "operator"},
        "expected": {
            "status": "ANSWER",
            "evidenceGroups": [
                {
                    "fact": "分块策略",
                    "alternatives": [
                        {
                            "documentId": doc_id,
                            "versionId": ver_id,
                            "quote": quote,
                            "relevanceGrade": 3,
                        }
                    ],
                }
            ],
        },
        "annotationStatus": "AUTO_REVIEWED",
    }
    # Update case-01 to AUTO_REVIEWED first
    EvaluationCatalog.update_case(admin, EvaluationCatalog.cases(admin, ds_id)[0]["id"], {"annotationStatus": "AUTO_REVIEWED"})
    # Save leak case
    EvaluationCatalog.save_cases(admin, ds_id, [leak_case])

    # Now freeze should detect SPLIT_LEAKAGE
    with pytest.raises(ApiException) as exc_leak:
        EvaluationCatalog.freeze(admin, ds_id)
    assert exc_leak.value.code == "SPLIT_LEAKAGE"

    # Remove the leaking case
    leak_case_id = [c["id"] for c in EvaluationCatalog.cases(admin, ds_id) if c["caseKey"] == "case-02-leak"][0]
    EvaluationCatalog.delete_case(admin, leak_case_id)

    # 5. Freeze with invalid quote -> should fail with ANCHOR_UNRESOLVED
    invalid_quote_case = {
        "caseKey": "case-03-bad-quote",
        "groupKey": "grp-03",
        "split": "DEV",
        "category": "FACT",
        "question": {"text": "不存在的引用？"},
        "persona": {"loginName": "operator"},
        "expected": {
            "status": "ANSWER",
            "evidenceGroups": [
                {
                    "fact": "假事实",
                    "alternatives": [
                        {
                            "documentId": doc_id,
                            "versionId": ver_id,
                            "quote": "这是一段在原文中完全不存在的虚构引文内容",
                            "relevanceGrade": 3,
                        }
                    ],
                }
            ],
        },
        "annotationStatus": "AUTO_REVIEWED",
    }
    EvaluationCatalog.save_cases(admin, ds_id, [invalid_quote_case])
    with pytest.raises(ApiException) as exc_anchor:
        EvaluationCatalog.freeze(admin, ds_id)
    assert exc_anchor.value.code == "ANCHOR_UNRESOLVED"

    # Remove invalid quote case
    bad_id = [c["id"] for c in EvaluationCatalog.cases(admin, ds_id) if c["caseKey"] == "case-03-bad-quote"][0]
    EvaluationCatalog.delete_case(admin, bad_id)

    # 6. Successful freeze
    frozen_ds = EvaluationCatalog.freeze(admin, ds_id)
    assert frozen_ds["status"] == "FROZEN"
    assert frozen_ds["frozenAt"] is not None

    # 7. Modify frozen dataset -> should fail with DATASET_FROZEN
    with pytest.raises(ApiException) as exc_mod:
        EvaluationCatalog.save_cases(admin, ds_id, cases_payload)
    assert exc_mod.value.code == "DATASET_FROZEN"

    # 8. Copy version -> creates new DRAFT dataset
    copied = EvaluationCatalog.copy_version(admin, ds_id)
    assert copied["revision"] == 2
    assert copied["status"] == "DRAFT"
    copied_cases = EvaluationCatalog.cases(admin, copied["id"])
    assert len(copied_cases) == 1
    assert copied_cases[0]["caseKey"] == "case-01"


# ==============================================================================
# 3. Snapshot, Config & Runner Integration Tests
# ==============================================================================

def test_snapshot_config_and_evaluation_run(eval_env):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.identity.security import issue_csrf_token

    client = TestClient(app)
    admin = eval_env["admin"]
    doc_id = eval_env["doc_id"]
    ver_id = eval_env["ver_id"]
    quote = eval_env["quote"]

    # Generate test auth session & CSRF
    session_token = "test-eval-session-token"
    with get_db_connection() as conn:
        from app.models import auth_session_table
        from app.identity.security import hash_token
        import uuid
        now = datetime.now(timezone.utc)
        conn.execute(
            auth_session_table.insert().values(
                tenant_id=admin.tenant_id,
                id=str(uuid.uuid4()),
                user_id=admin.user_id,
                token_hash=hash_token(session_token),
                expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                revoked_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        conn.commit()

    csrf = issue_csrf_token(session_token, settings.RAG_CSRF_SECRET)
    headers = {"x-csrf-token": csrf}
    cookies = {"RAG_SESSION": session_token, "RAG_CSRF": csrf}

    # 1. Create and publish RETRIEVAL config via /api/v1/admin/configs
    cfg_resp = client.post(
        "/api/v1/admin/configs",
        json={
            "kind": "RETRIEVAL",
            "name": "eval_bm25_cfg",
            "payload": {
                "retrieval": {"mode": "BM25"},
                "query": {"rewrite": False, "hyde": False, "multiQuery": False},
                "post": {"rerank": False, "mmr": False},
            },
        },
        headers=headers,
        cookies=cookies,
    )
    assert cfg_resp.status_code == 201
    cfg_id = cfg_resp.json()["id"]

    pub_resp = client.post(
        f"/api/v1/admin/configs/{cfg_id}/publish",
        headers=headers,
        cookies=cookies,
    )
    assert pub_resp.status_code == 200
    assert pub_resp.json()["status"] == "PUBLISHED"

    # List configs
    list_cfg = client.get("/api/v1/admin/configs", cookies=cookies)
    assert list_cfg.status_code == 200
    assert any(c["id"] == cfg_id for c in list_cfg.json())

    # 2. Create snapshot
    snap_resp = client.post(
        "/api/v1/admin/eval/snapshots",
        json={
            "name": "Eval Baseline Snapshot",
            "documentIds": [doc_id],
            "approvedSyntheticOrRedacted": True,
        },
        headers=headers,
        cookies=cookies,
    )
    assert snap_resp.status_code == 201
    snap_id = snap_resp.json()["id"]

    # 3. Create and freeze dataset
    ds = EvaluationCatalog.create_dataset(admin, {"name": "Eval Test Suite"})
    ds_id = ds["id"]
    EvaluationCatalog.save_cases(
        admin,
        ds_id,
        [
            {
                "caseKey": "smoke-qa-01",
                "groupKey": "grp-smoke-01",
                "split": "TEST",
                "category": "FACT",
                "question": {"text": "知识库系统采用什么分块策略？"},
                "persona": {"loginName": "operator"},
                "expected": {
                    "status": "ANSWER",
                    "evidenceGroups": [
                        {
                            "fact": "分块策略",
                            "alternatives": [
                                {
                                    "documentId": doc_id,
                                    "versionId": ver_id,
                                    "quote": quote,
                                    "relevanceGrade": 3,
                                }
                            ],
                        }
                    ],
                },
                "annotationStatus": "AUTO_REVIEWED",
            },
            {
                "caseKey": "smoke-qa-02",
                "groupKey": "grp-smoke-02",
                "split": "TEST",
                "category": "FACT",
                "question": {"text": "不可回答的问题？"},
                "persona": {"loginName": "operator"},
                "expected": {
                    "status": "ABSTAIN",
                    "evidenceGroups": [],
                },
                "annotationStatus": "AUTO_REVIEWED",
            },
        ],
    )
    EvaluationCatalog.freeze(admin, ds_id)

    # 4. Estimate evaluation cost
    est_resp = client.post(
        "/api/v1/admin/eval/estimates",
        json={
            "datasetVersionId": ds_id,
            "snapshotId": snap_id,
            "configId": cfg_id,
            "split": "TEST",
            "repeats": 1,
            "mode": "RETRIEVAL_ONLY",
        },
        headers=headers,
        cookies=cookies,
    )
    assert est_resp.status_code == 200
    est = est_resp.json()
    assert est["plannedQuestions"] == 2
    assert est["costStatus"] == "NO_MODEL_CALLS"
    assert est["estimateId"] is not None

    # 5. Start evaluation run
    start_resp = client.post(
        "/api/v1/admin/eval/runs",
        json={
            "datasetVersionId": ds_id,
            "snapshotId": snap_id,
            "configId": cfg_id,
            "split": "TEST",
            "repeats": 1,
            "mode": "RETRIEVAL_ONLY",
            "estimateId": est["estimateId"],
            "maxCostCny": 5,
        },
        headers={"x-csrf-token": csrf, "Idempotency-Key": "eval-key-001"},
        cookies=cookies,
    )
    assert start_resp.status_code == 201
    run_id = start_resp.json()["runId"]

    # Poll run until completed
    import time
    for _ in range(60):
        run_detail = client.get(f"/api/v1/admin/eval/runs/{run_id}", cookies=cookies).json()
        if run_detail.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.5)

    assert run_detail["status"] == "COMPLETED"
    metrics = run_detail["metrics"]
    assert metrics["planned"] == 2
    assert metrics["completed"] == 2
    assert metrics["failed"] == 0

    # 6. Check per-case results
    results_resp = client.get(f"/api/v1/admin/eval/runs/{run_id}/results", cookies=cookies)
    assert results_resp.status_code == 200
    cases_res = results_resp.json()
    assert len(cases_res) == 2
    res_keys = {c["caseKey"] for c in cases_res}
    assert "smoke-qa-01" in res_keys
    assert "smoke-qa-02" in res_keys

    # 7. Add human review to result
    res_id = cases_res[0]["id"]
    rev_resp = client.post(
        f"/api/v1/admin/eval/results/{res_id}/reviews",
        json={"labels": {"correctness": "CORRECT"}, "reason": "人工核验通过"},
        headers=headers,
        cookies=cookies,
    )
    assert rev_resp.status_code == 201

    # Verify review attached in results
    recheck_res = client.get(f"/api/v1/admin/eval/runs/{run_id}/results", cookies=cookies).json()
    r0 = [r for r in recheck_res if r["id"] == res_id][0]
    assert len(r0["reviews"]) == 1
    assert r0["reviews"][0]["labels"]["correctness"] == "CORRECT"

    # 8. Test export
    exp_resp = client.post(
        f"/api/v1/admin/eval/runs/{run_id}/exports",
        json={"mode": "REDACTED"},
        headers=headers,
        cookies=cookies,
    )
    assert exp_resp.status_code == 200
    assert exp_resp.json()["mode"] == "REDACTED"
    assert len(exp_resp.json()["cases"]) == 2

    # 9. Test comparisons
    # Start second run with same params
    start_resp2 = client.post(
        "/api/v1/admin/eval/runs",
        json={
            "datasetVersionId": ds_id,
            "snapshotId": snap_id,
            "configId": cfg_id,
            "split": "TEST",
            "repeats": 1,
            "mode": "RETRIEVAL_ONLY",
            "estimateId": est["estimateId"],
            "maxCostCny": 5,
        },
        headers={"x-csrf-token": csrf, "Idempotency-Key": "eval-key-002"},
        cookies=cookies,
    )
    run_id2 = start_resp2.json()["runId"]
    for _ in range(20):
        r2 = client.get(f"/api/v1/admin/eval/runs/{run_id2}", cookies=cookies).json()
        if r2.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.5)

    comp_resp = client.get(f"/api/v1/admin/eval/comparisons?runIds={run_id},{run_id2}", cookies=cookies)
    assert comp_resp.status_code == 200
    comp = comp_resp.json()
    assert comp["comparable"] is True
    assert len(comp["runs"]) == 2

    # 10. Test baseline diff comparison against artifacts/baseline-report.json
    diff_resp = client.get(f"/api/v1/admin/eval/runs/{run_id}/baseline-diff", cookies=cookies)
    assert diff_resp.status_code == 200
    diff = diff_resp.json()
    assert diff["runId"] == run_id
    assert diff["baselineBackend"] == "java-spring-boot-elasticsearch"
    assert "metricDeltas" in diff
    assert "configurationDifferences" in diff
    assert "recallAt5" in diff["metricDeltas"]


def test_eval_smoke_30_dataset_conformance():
    """Verify datasets/eval_smoke_30.jsonl conformance: 30 cases, Chinese text, codes, and paths."""
    cur = os.path.abspath(__file__)
    smoke_file = ""
    for _ in range(6):
        cur = os.path.dirname(cur)
        candidate = os.path.join(cur, "datasets", "eval_smoke_30.jsonl")
        if os.path.exists(candidate):
            smoke_file = candidate
            break

    assert os.path.exists(smoke_file), f"Smoke dataset not found at {smoke_file}"

    with open(smoke_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    assert len(lines) == 30

    case_keys = set()
    group_splits = {}
    has_chinese = False
    has_code_or_path = False

    for idx, line in enumerate(lines, start=1):
        c = json.loads(line)
        for req in ["caseKey", "groupKey", "split", "category", "question", "persona", "expected", "annotationStatus"]:
            assert req in c, f"Line {idx} missing {req}"

        k = c["caseKey"]
        assert k not in case_keys
        case_keys.add(k)

        split = c["split"].upper()
        assert split in {"DEV", "TEST"}

        grp = c["groupKey"]
        old_split = group_splits.setdefault(grp, split)
        assert old_split == split, f"Group {grp} leaks across splits"

        full_text = json.dumps(c, ensure_ascii=False)
        if any("\u4e00" <= ch <= "\u9fff" for ch in full_text):
            has_chinese = True
        if any(term in full_text for term in ["CSRF", "API", "H1-H6", "TRASH", "1024", "doc-sys"]):
            has_code_or_path = True

    assert has_chinese
    assert has_code_or_path


def test_single_case_failure_retention(eval_env):
    """Verify that single case failure is retained as FAILED with errorCode and never disguised."""
    admin = eval_env["admin"]
    doc_id = eval_env["doc_id"]
    ver_id = eval_env["ver_id"]
    quote = eval_env["quote"]

    # 1. Create dataset and snapshot
    ds = EvaluationCatalog.create_dataset(admin, {"name": "Failure Retention Test Suite"})
    ds_id = ds["id"]
    EvaluationCatalog.save_cases(
        admin,
        ds_id,
        [
            {
                "caseKey": "good-case",
                "groupKey": "grp-f-01",
                "split": "TEST",
                "category": "FACT",
                "question": {"text": "结构化分块策略"},
                "persona": {"loginName": "operator"},
                "expected": {
                    "status": "ANSWER",
                    "evidenceGroups": [
                        {
                            "fact": "正常事实",
                            "alternatives": [
                                {
                                    "documentId": doc_id,
                                    "versionId": ver_id,
                                    "quote": quote,
                                    "relevanceGrade": 3,
                                }
                            ],
                        }
                    ],
                },
                "annotationStatus": "AUTO_REVIEWED",
            },
            {
                "caseKey": "failing-case",
                "groupKey": "grp-f-02",
                "split": "TEST",
                "category": "FACT",
                "question": {"text": "将会失败的题目？"},
                "persona": {"loginName": "operator"},
                "expected": {
                    "status": "ANSWER",
                    "evidenceGroups": [
                        {
                            "fact": "失败事实",
                            "alternatives": [
                                {
                                    "documentId": doc_id,
                                    "versionId": ver_id,
                                    "quote": quote,
                                    "relevanceGrade": 3,
                                }
                            ],
                        }
                    ],
                },
                "annotationStatus": "AUTO_REVIEWED",
            },
        ],
    )
    EvaluationCatalog.freeze(admin, ds_id)
    snap = EvaluationCatalog.create_snapshot(admin, {"name": "Fail Test Snap", "documentIds": [doc_id], "approvedSyntheticOrRedacted": True})
    snap_id = snap["id"]

    # Mock RETRIEVAL config
    with get_db_connection() as conn:
        cfg_id = "cfg-fail-retention-test"
        now = datetime.now(timezone.utc)
        conn.execute(
            config_revision_table.insert().values(
                tenant_id=admin.tenant_id,
                id=cfg_id,
                kind="RETRIEVAL",
                name="fail_test_cfg",
                revision=1,
                sha256=b"0" * 32,
                payload={
                    "retrieval": {"mode": "BM25"},
                    "query": {"rewrite": False, "hyde": False, "multiQuery": False},
                    "post": {"rerank": False, "mmr": False},
                },
                status="PUBLISHED",
                created_at=now,
                updated_at=now,
            )
        )
        conn.commit()

    est = EvaluationRunner.estimate(admin, {
        "datasetVersionId": ds_id,
        "snapshotId": snap_id,
        "configId": cfg_id,
        "split": "TEST",
        "mode": "RETRIEVAL_ONLY",
    })

    # Mock Milvus search failure specifically for 'failing-case'
    from unittest.mock import patch
    from app.retrieval.milvus_service import MilvusHybridService

    orig_search = MilvusHybridService.search

    def mock_search(*args, **kwargs):
        query = kwargs.get("query", "")
        if "将会失败" in query:
            raise RuntimeError("SIMULATED_INDEX_TIMEOUT")
        return orig_search(*args, **kwargs)

    with patch.object(MilvusHybridService, "search", side_effect=mock_search):
        run_info = EvaluationRunner.start(admin, {
            "datasetVersionId": ds_id,
            "snapshotId": snap_id,
            "configId": cfg_id,
            "split": "TEST",
            "mode": "RETRIEVAL_ONLY",
            "estimateId": est["estimateId"],
        }, "fail-retention-key-01")

        run_id = run_info["runId"]

        import time
        for _ in range(20):
            r = EvaluationRunner.get_run(admin, run_id)
            if r.get("status") in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.3)

    # Check results
    res_list = EvaluationRunner.results(admin, run_id)
    assert len(res_list) == 2

    good_res = [r for r in res_list if r["caseKey"] == "good-case"][0]
    bad_res = [r for r in res_list if r["caseKey"] == "failing-case"][0]

    assert good_res["status"] == "COMPLETED"
    assert good_res["errorCode"] is None

    # CRITICAL: bad_res MUST be retained as FAILED, with errorCode
    assert bad_res["status"] == "FAILED"
    assert bad_res["errorCode"] == "RETRIEVAL_FAILED"
    assert "error" in bad_res["judge"]
    assert "SIMULATED_INDEX_TIMEOUT" in bad_res["judge"]["error"]

    # Check run aggregate
    run_detail = EvaluationRunner.get_run(admin, run_id)
    m = run_detail["metrics"]
    assert m["planned"] == 2
    assert m["completed"] == 1
    assert m["failed"] == 1
    assert m["taskSuccessRate"] == 1.0 / 2

