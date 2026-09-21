import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
import bcrypt
from sqlalchemy import select

from app.db import get_db_connection, engine
from app.models import (
    metadata,
    tenant_table,
    app_user_table,
    user_group_table,
    group_member_table,
    config_revision_table,
)

logger = logging.getLogger(__name__)


class DemoSeed:
    @staticmethod
    def run():
        """Ensures tables exist and demo seed data (tenant, admin user, ops group, default config) is present."""
        now = datetime.now(timezone.utc)

        # 1. Ensure metadata schema tables are created
        try:
            metadata.create_all(bind=engine)
        except Exception as e:
            logger.warning("metadata.create_all check: %s", e)

        with get_db_connection() as conn:
            # 2. Seed Tenant: tenant-demo-1
            tenant = conn.execute(
                select(tenant_table).where(tenant_table.c.code == "demo")
            ).first()

            tenant_id = "tenant-demo-1"
            if not tenant:
                logger.info("Seeding default demo tenant: %s", tenant_id)
                conn.execute(
                    tenant_table.insert().values(
                        id=tenant_id,
                        code="demo",
                        name="Demo Tenant",
                        security_epoch=1,
                        active_retrieval_config_id="cfg-default-retrieval",
                        created_at=now,
                    )
                )
                conn.commit()

            else:
                tenant_id = str(tenant.id)

            # 3. Seed Admin User
            admin_user = conn.execute(
                select(app_user_table).where(
                    app_user_table.c.tenant_id == tenant_id,
                    app_user_table.c.login_name == "admin",
                )
            ).first()

            user_id = "user-admin-1"
            if not admin_user:
                seed_pwd = os.getenv("RAG_SEED_PASSWORD", "admin123")
                hashed_pwd = bcrypt.hashpw(seed_pwd.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
                logger.info("Seeding default admin user: admin")
                conn.execute(
                    app_user_table.insert().values(
                        tenant_id=tenant_id,
                        id=user_id,
                        login_name="admin",
                        password_hash=hashed_pwd,
                        is_admin=True,
                        status="ACTIVE",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()

            else:
                user_id = str(admin_user.id)

            # 4. Seed Ops Group
            ops_group = conn.execute(
                select(user_group_table).where(
                    user_group_table.c.tenant_id == tenant_id,
                    user_group_table.c.name == "Ops Group",
                )
            ).first()

            group_id = "group-ops-1"
            if not ops_group:
                logger.info("Seeding default user group: Ops Group")
                conn.execute(
                    user_group_table.insert().values(
                        tenant_id=tenant_id,
                        id=group_id,
                        name="Ops Group",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()

            else:
                group_id = str(ops_group.id)

            # 5. Seed Group Membership
            gm = conn.execute(
                select(group_member_table).where(
                    group_member_table.c.tenant_id == tenant_id,
                    group_member_table.c.group_id == group_id,
                    group_member_table.c.user_id == user_id,
                )
            ).first()
            if not gm:
                conn.execute(
                    group_member_table.insert().values(
                        tenant_id=tenant_id,
                        id=f"gm-{uuid.uuid4().hex[:8]}",
                        group_id=group_id,
                        user_id=user_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()

            # 6. Seed versioned enterprise retrieval strategy.
            enterprise_cfg_id = "cfg-enterprise-retrieval-v2"
            enterprise_cfg = conn.execute(
                select(config_revision_table).where(
                    config_revision_table.c.tenant_id == tenant_id,
                    config_revision_table.c.id == enterprise_cfg_id,
                )
            ).first()
            if not enterprise_cfg:
                enterprise_payload = {
                    "retrieval": {
                        "mode": "HYBRID",
                        "denseTopK": 30,
                        "sparseTopK": 30,
                        "fusionTopK": 20,
                        "rrfK": 60,
                        "finalTopK": 8,
                    },
                    "rerank": {
                        "enabled": True,
                        "topK": 10,
                        "model": "rerank-main",
                        "failOpen": True,
                    },
                    "parentExpansion": {
                        "enabled": True,
                        "maxChars": 6000,
                    },
                }
                payload_str = json.dumps(enterprise_payload, sort_keys=True)
                conn.execute(
                    config_revision_table.insert().values(
                        tenant_id=tenant_id,
                        id=enterprise_cfg_id,
                        kind="RETRIEVAL",
                        name="enterprise_retrieval_pipeline",
                        revision=2,
                        sha256=hashlib.sha256(payload_str.encode("utf-8")).digest(),
                        payload=enterprise_payload,
                        status="PUBLISHED",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()

            tenant_cfg = conn.execute(
                select(tenant_table.c.active_retrieval_config_id).where(
                    tenant_table.c.id == tenant_id
                )
            ).scalar()
            if not tenant_cfg or str(tenant_cfg) == "cfg-default-retrieval":
                conn.execute(
                    tenant_table.update()
                    .where(tenant_table.c.id == tenant_id)
                    .values(active_retrieval_config_id=enterprise_cfg_id)
                )
                conn.commit()

            # Keep v1 for backwards-compatible historical runs.
            # 6b. Seed Default Retrieval Strategy Config
            default_cfg = conn.execute(
                select(config_revision_table).where(
                    config_revision_table.c.tenant_id == tenant_id,
                    config_revision_table.c.id == "cfg-default-retrieval",
                )
            ).first()
            if not default_cfg:
                payload = {
                    "retrieval": {
                        "mode": "HYBRID",
                        "denseWeight": 0.5,
                        "bm25Weight": 0.5,
                        "topK": 10,
                        "rrfK": 60,
                    }
                }
                payload_str = json.dumps(payload, sort_keys=True)
                sha256_bytes = hashlib.sha256(payload_str.encode("utf-8")).digest()
                conn.execute(
                    config_revision_table.insert().values(
                        tenant_id=tenant_id,
                        id="cfg-default-retrieval",
                        kind="RETRIEVAL",
                        name="default_retrieval_strategy",
                        revision=1,
                        sha256=sha256_bytes,
                        payload=payload,
                        status="PUBLISHED",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()

            # 7. Seed model gateway aliases referenced by model_call records.
            model_configs = {
                "cfg-model-chat-main": "chat-main",
                "cfg-model-embedding-main": "embedding-main",
                "cfg-model-judge-main": "judge-main",
                "cfg-model-rerank-main": "rerank-main",
            }
            for config_id, alias in model_configs.items():
                exists_row = conn.execute(
                    select(config_revision_table.c.id).where(
                        config_revision_table.c.tenant_id == tenant_id,
                        config_revision_table.c.id == config_id,
                    )
                ).first()
                if exists_row:
                    continue
                payload = {"modelAlias": alias}
                conn.execute(
                    config_revision_table.insert().values(
                        tenant_id=tenant_id,
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
            conn.commit()
