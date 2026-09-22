import io
import uuid
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

import app.db as app_db
from app.errors import ApiException
from app.identity.deps import Actor
from app.knowledge.service import KnowledgeService
from app.knowledge.imports import create_import_preview, consume_import_preview, split_wiki_package
from app.model_budget import BudgetService
from app.models import (
    document_table,
    eval_case_table,
    eval_dataset_version_table,
    eval_gold_table,
    eval_snapshot_document_table,
    eval_snapshot_table,
    chunk_table,
)
from app.qa.service import QaService
from app.retrieval.config import RetrievalConfig
from app.retrieval.pipeline import RetrievalPipeline


ACTOR = Actor(
    tenant_id="tenant-demo-1",
    user_id="user-admin-1",
    admin=True,
    security_epoch=1,
)


def test_strict_judge_requires_complete_claim_coverage():
    claims = [{"id": "c1"}, {"id": "c2"}]

    with pytest.raises(ApiException) as exc:
        QaService._validate_strict_verdicts(claims, {"verdicts": []})
    assert exc.value.code == "VERIFICATION_FAILED"

    with pytest.raises(ApiException):
        QaService._validate_strict_verdicts(
            claims,
            {"verdicts": [{"claimId": "c1", "status": "SUPPORTED"}]},
        )

    QaService._validate_strict_verdicts(
        claims,
        {
            "verdicts": [
                {"claimId": "c1", "status": "SUPPORTED"},
                {"claimId": "c2", "status": "SUPPORTED"},
            ]
        },
    )


def test_client_cannot_override_retrieval_config():
    conv = QaService.create_conversation(
        ACTOR,
        {"title": "config parity", "scope": {"mode": "ALL"}},
    )
    with pytest.raises(ApiException) as exc:
        QaService.submit_run(
            ACTOR,
            conv["conversationId"],
            {
                "question": "系统如何检索？",
                "mode": "RAG",
                "configId": "cfg-attacker-selected",
            },
            idempotency_key="config-override",
        )
    assert exc.value.code == "INVALID_INPUT"


def test_upload_idempotency_hash_includes_file_content():
    kb = KnowledgeService.create_kb(ACTOR, "Idempotency KB", [])
    kb_id = kb["id"]

    KnowledgeService.upload_document(
        ACTOR,
        kb_id,
        None,
        "same.txt",
        b"AA",
        "same-key",
        {"embedding": True},
    )

    with pytest.raises(ApiException) as exc:
        KnowledgeService.upload_document(
            ACTOR,
            kb_id,
            None,
            "same.txt",
            b"BB",
            "same-key",
            {"embedding": True},
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_parent_over_budget_falls_back_to_child():
    now = datetime.now(timezone.utc)
    build_id = "build-parent-budget"
    parent_id = "parent-budget"
    child_id = "child-budget"
    with app_db.engine.begin() as conn:
        conn.execute(
            chunk_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=parent_id,
                build_id=build_id,
                parent_id=None,
                kind="PARENT",
                ordinal=0,
                text_content="P" * 100,
                token_count=100,
                content_hash=b"p" * 32,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            chunk_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=child_id,
                build_id=build_id,
                parent_id=parent_id,
                kind="CHILD",
                ordinal=0,
                text_content="child evidence",
                token_count=3,
                content_hash=b"c" * 32,
                created_at=now,
                updated_at=now,
            )
        )

    warnings = []
    result = RetrievalPipeline._expand_parents(
        ACTOR.tenant_id,
        [
            {
                "chunkId": child_id,
                "buildId": build_id,
                "text": "child evidence",
                "score": 1.0,
            }
        ],
        RetrievalConfig(config_id="test", parent_max_chars=10),
        warnings,
    )
    assert result[0]["text"] == "child evidence"
    assert result[0].get("parentChunkId") is None
    assert "PARENT_TRIMMED" in warnings


def test_purge_scrubs_evaluation_derivatives():
    kb = KnowledgeService.create_kb(ACTOR, "Purge Eval KB", [])
    uploaded = KnowledgeService.upload_document(
        ACTOR,
        kb["id"],
        None,
        "secret.txt",
        b"secret evaluation source",
        "purge-eval-upload",
        {"embedding": True},
    )
    doc_id = uploaded["documentId"]
    now = datetime.now(timezone.utc)

    dataset_id = "dataset-purge"
    case_id = "case-purge"
    snapshot_id = "snapshot-purge"

    with app_db.engine.begin() as conn:
        conn.execute(
            eval_dataset_version_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=dataset_id,
                name="purge",
                revision=1,
                status="FROZEN",
                manifest_hash=b"d" * 32,
                frozen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            eval_case_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=case_id,
                dataset_version_id=dataset_id,
                case_key="purge-case",
                split="TEST",
                group_key="purge",
                category="FACTUAL",
                question_json={"question": "secret?"},
                persona_json={},
                expected_json={"evidenceGroups": [{"quote": "secret"}]},
                annotation_status="HUMAN_REVIEWED",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            eval_gold_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=str(uuid.uuid4()),
                case_id=case_id,
                fact_group="secret",
                alternative_no=0,
                origin_system="test",
                origin_tenant_id=ACTOR.tenant_id,
                origin_document_id=doc_id,
                version_hash=b"v" * 32,
                anchor_json={"quote": "secret"},
                relevance_grade=3,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            eval_snapshot_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=snapshot_id,
                name="purge",
                manifest_hash=b"s" * 32,
                manifest_json={"secret": "value"},
                status="READY",
                environment_id="test",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            eval_snapshot_document_table.insert().values(
                tenant_id=ACTOR.tenant_id,
                id=str(uuid.uuid4()),
                snapshot_id=snapshot_id,
                document_id=doc_id,
                version_id=uploaded["versionId"],
                origin_system="test",
                origin_tenant_id=ACTOR.tenant_id,
                origin_document_id=doc_id,
                origin_version_hash=b"v" * 32,
                created_at=now,
                updated_at=now,
            )
        )

    with app_db.engine.connect() as conn:
        row_version = conn.execute(
            select(document_table.c.row_version).where(document_table.c.id == doc_id)
        ).scalar_one()
    KnowledgeService.document_lifecycle(ACTOR, doc_id, "trash", int(row_version))

    with app_db.engine.connect() as conn:
        row_version = conn.execute(
            select(document_table.c.row_version).where(document_table.c.id == doc_id)
        ).scalar_one()
    KnowledgeService.document_lifecycle(ACTOR, doc_id, "purge", int(row_version))

    with app_db.engine.connect() as conn:
        gold = conn.execute(
            select(eval_gold_table.c.anchor_json).where(eval_gold_table.c.case_id == case_id)
        ).scalar_one()
        case = conn.execute(
            select(
                eval_case_table.c.question_json,
                eval_case_table.c.expected_json,
                eval_case_table.c.annotation_status,
            ).where(eval_case_table.c.id == case_id)
        ).one()
        dataset_status = conn.execute(
            select(eval_dataset_version_table.c.status).where(
                eval_dataset_version_table.c.id == dataset_id
            )
        ).scalar_one()
        snapshot = conn.execute(
            select(eval_snapshot_table.c.status, eval_snapshot_table.c.manifest_json).where(
                eval_snapshot_table.c.id == snapshot_id
            )
        ).one()

    assert gold == {}
    assert case.question_json == {}
    assert case.expected_json == {}
    assert case.annotation_status == "PURGED"
    assert dataset_status == "PURGED"
    assert snapshot.status == "PURGED"
    assert snapshot.manifest_json == {}



def test_directory_import_rechecks_source_hash(tmp_path, monkeypatch):
    root = tmp_path / "import-root"
    root.mkdir()
    source = root / "guide.md"
    source.write_text("# Guide\noriginal", encoding="utf-8")
    monkeypatch.setenv("RAG_IMPORT_ROOT", str(root))

    preview = create_import_preview(ACTOR.tenant_id, ACTOR.user_id, ".")
    assert preview["files"][0]["relativePath"] == "guide.md"

    source.write_text("# Guide\nchanged", encoding="utf-8")
    with pytest.raises(ApiException) as exc:
        consume_import_preview(
            ACTOR.tenant_id,
            ACTOR.user_id,
            preview["estimateId"],
            ["guide.md"],
        )
    assert exc.value.code == "SOURCE_CHANGED"


def test_wiki_import_rejects_traversal_and_remote_assets():
    def make_zip(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return buffer.getvalue()

    with pytest.raises(ApiException) as exc:
        split_wiki_package(make_zip({"../escape.md": "# bad"}))
    assert exc.value.code == "INVALID_ARCHIVE"

    with pytest.raises(ApiException) as exc:
        split_wiki_package(
            make_zip({"page.md": "![remote](https://example.invalid/image.png)"})
        )
    assert exc.value.code == "INVALID_ARCHIVE"

    pages = split_wiki_package(
        make_zip(
            {
                "page.md": "# Page\n![local](assets/p.png)",
                "assets/p.png": b"\x89PNG\r\n\x1a\n",
            }
        )
    )
    assert len(pages) == 1
    assert b"data:image/png;base64," in pages[0]["bytes"]


def test_not_sent_budget_reservation_is_released():
    call_id = str(uuid.uuid4())
    reserved = BudgetService.reserve(
        tenant_id=ACTOR.tenant_id,
        call_id=call_id,
        model_alias="chat-main",
        input_upper_bound=100,
        output_upper_bound=100,
    )
    BudgetService.settle(
        ACTOR.tenant_id,
        reserved["id"],
        "NOT_SENT",
        Decimal("0"),
    )
    summary = BudgetService.summary(ACTOR.tenant_id)
    assert summary
    assert Decimal(summary[0]["reservedAmount"]) == Decimal("0")
    assert Decimal(summary[0]["settledAmount"]) == Decimal("0")
