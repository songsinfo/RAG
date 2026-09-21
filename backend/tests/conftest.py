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

from app.models import metadata, tenant_table, app_user_table, user_group_table, group_member_table
import app.db as app_db
from app.main import app


@pytest.fixture(autouse=True)
def setup_test_db():
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
    yield test_engine
    app_db.engine = orig_engine


@pytest.fixture
def client():
    return TestClient(app)
