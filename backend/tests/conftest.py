import os
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
import bcrypt

# Ensure test environment
os.environ["RAG_ENVIRONMENT"] = "test"
os.environ["RAG_CSRF_SECRET"] = "test-secret-key-12345"
os.environ["LITELLM_BASE_URL"] = ""
os.environ["MILVUS_URI"] = ""

from app.config import settings
settings.ENVIRONMENT = "test"
settings.LITELLM_BASE_URL = ""
settings.MILVUS_URI = ""

from app.models import metadata, tenant_table, app_user_table, user_group_table, group_member_table, config_revision_table
import app.db as app_db
from app.main import app


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    # In-memory SQLite for fast, reproducible tests
    from app.identity.security import _login_attempts
    _login_attempts.clear()

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(test_engine)

    now = datetime.now(timezone.utc)
    hashed_pwd = bcrypt.hashpw(b"admin123", bcrypt.gensalt(rounds=4)).decode("utf-8")
    with test_engine.begin() as conn:
        conn.execute(
            tenant_table.insert().values(
                id="tenant-demo-1",
                code="demo",
                name="Demo Tenant",
                security_epoch=1,
                created_at=now,
            )
        )
        conn.execute(
            app_user_table.insert().values(
                tenant_id="tenant-demo-1",
                id="user-admin-1",
                login_name="admin",
                password_hash=hashed_pwd,
                is_admin=True,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            app_user_table.insert().values(
                tenant_id="tenant-demo-1",
                id="user-disabled-1",
                login_name="disabled_user",
                password_hash=hashed_pwd,
                is_admin=False,
                status="DISABLED",
                created_at=now,
                updated_at=now,
            )
        )
        for config_id, alias in {
            "cfg-model-chat-main": "chat-main",
            "cfg-model-embedding-main": "embedding-main",
            "cfg-model-judge-main": "judge-main",
            "cfg-model-rerank-main": "rerank-main",
        }.items():
            payload = {
                "modelAlias": alias,
                "currency": "CNY",
                "inputPricePerMillion": 0,
                "outputPricePerMillion": 0,
            }
            import hashlib, json
            conn.execute(
                config_revision_table.insert().values(
                    tenant_id="tenant-demo-1",
                    id=config_id,
                    kind="MODEL",
                    name=alias,
                    revision=1,
                    sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).digest(),
                    payload=payload,
                    status="PUBLISHED",
                    created_at=now,
                    updated_at=now,
                )
            )

        conn.execute(
            user_group_table.insert().values(
                tenant_id="tenant-demo-1",
                id="group-ops-1",
                name="Ops Group",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            group_member_table.insert().values(
                tenant_id="tenant-demo-1",
                id="gm-1",
                group_id="group-ops-1",
                user_id="user-admin-1",
                created_at=now,
                updated_at=now,
            )
        )

    # Patch app.db.engine
    orig_engine = app_db.engine
    app_db.engine = test_engine

    # Ingestion tests stay hermetic while production always calls Docling.
    # Dedicated parser tests below exercise the real HTTP contract separately.
    from app.knowledge.chunker import adapt_docling_hybrid_chunks
    import app.knowledge.service as knowledge_service

    def fake_parse_and_chunk_document(file_bytes, filename, build_config=None):
        try:
            text = file_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = f"binary document {filename}"
        if not text:
            text = filename
        items = [{
            "filename": filename,
            "chunk_index": 0,
            "text": text,
            "raw_text": text,
            "num_tokens": max(1, len(text) // 4),
            "headings": [],
            "captions": [],
            "doc_items": [f"#/texts/{filename}"],
            "page_numbers": [1],
            "metadata": {},
        }]
        return adapt_docling_hybrid_chunks(items, parent_max_tokens=1200)

    monkeypatch.setattr(
        knowledge_service,
        "parse_and_chunk_document",
        fake_parse_and_chunk_document,
    )
    yield test_engine
    app_db.engine = orig_engine


@pytest.fixture
def client():
    return TestClient(app)
