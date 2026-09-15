from dataclasses import dataclass
from datetime import datetime, timezone
from fastapi import Request, Depends
from sqlalchemy import select, and_
from sqlalchemy.sql import func
from app.db import get_db_connection
from app.errors import ApiException
from app.models import auth_session_table, app_user_table
from app.identity.security import hash_token


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    user_id: str
    admin: bool


def get_current_actor(request: Request) -> Actor:
    session_token = request.cookies.get("RAG_SESSION")
    if not session_token:
        raise ApiException(401, "AUTH_REQUIRED", "请先登录")

    token_h = hash_token(session_token)

    with get_db_connection() as conn:
        stmt = (
            select(
                app_user_table.c.tenant_id,
                app_user_table.c.id.label("user_id"),
                app_user_table.c.is_admin,
            )
            .select_from(
                auth_session_table.join(
                    app_user_table,
                    and_(
                        app_user_table.c.tenant_id == auth_session_table.c.tenant_id,
                        app_user_table.c.id == auth_session_table.c.user_id,
                    ),
                )
            )
            .where(
                and_(
                    auth_session_table.c.token_hash == token_h,
                    auth_session_table.c.revoked_at.is_(None),
                    auth_session_table.c.expires_at > datetime.now(timezone.utc),
                    app_user_table.c.status == "ACTIVE",
                )
            )
        )
        row = conn.execute(stmt).first()
        if not row:
            raise ApiException(401, "AUTH_REQUIRED", "登录状态已失效")

        return Actor(
            tenant_id=str(row.tenant_id),
            user_id=str(row.user_id),
            admin=bool(row.is_admin),
        )


def require_admin(actor: Actor = Depends(get_current_actor)) -> Actor:
    if not actor.admin:
        raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
    return actor


def principal_matches(grant_table, actor: Actor):
    from sqlalchemy import or_, exists
    from app.models import group_member_table

    return or_(
        grant_table.c.principal_type == "ALL",
        grant_table.c.user_id == actor.user_id,
        exists().where(
            and_(
                group_member_table.c.tenant_id == actor.tenant_id,
                group_member_table.c.user_id == actor.user_id,
                group_member_table.c.group_id == grant_table.c.group_id,
            )
        ),
    )


def can_read_document(conn, actor: Actor, doc_id: str) -> bool:
    from sqlalchemy import or_, exists
    from app.models import (
        document_table,
        knowledge_base_table,
        kb_grant_table,
        document_grant_table,
    )

    user_active = exists().where(
        and_(
            app_user_table.c.tenant_id == actor.tenant_id,
            app_user_table.c.id == actor.user_id,
            app_user_table.c.status == "ACTIVE",
        )
    )
    kb_accessible = exists().where(
        and_(
            knowledge_base_table.c.tenant_id == actor.tenant_id,
            knowledge_base_table.c.id == document_table.c.kb_id,
            knowledge_base_table.c.status == "LIVE",
            exists().where(
                and_(
                    kb_grant_table.c.tenant_id == actor.tenant_id,
                    kb_grant_table.c.kb_id == knowledge_base_table.c.id,
                    principal_matches(kb_grant_table, actor),
                )
            ),
        )
    )
    doc_acl_ok = or_(
        document_table.c.acl_mode == "INHERIT",
        exists().where(
            and_(
                document_grant_table.c.tenant_id == actor.tenant_id,
                document_grant_table.c.document_id == document_table.c.id,
                principal_matches(document_grant_table, actor),
            )
        ),
    )
    stmt = select(document_table.c.id).where(
        and_(
            document_table.c.tenant_id == actor.tenant_id,
            document_table.c.id == doc_id,
            document_table.c.status == "LIVE",
            user_active,
            kb_accessible,
            doc_acl_ok,
        )
    )
    return conn.execute(stmt).first() is not None


def get_readable_builds(conn, actor: Actor, kb_ids=None) -> list:
    from sqlalchemy import or_, exists
    from app.models import (
        document_table,
        knowledge_base_table,
        kb_grant_table,
        document_grant_table,
    )

    user_active = exists().where(
        and_(
            app_user_table.c.tenant_id == actor.tenant_id,
            app_user_table.c.id == actor.user_id,
            app_user_table.c.status == "ACTIVE",
        )
    )
    kb_accessible = exists().where(
        and_(
            knowledge_base_table.c.tenant_id == actor.tenant_id,
            knowledge_base_table.c.id == document_table.c.kb_id,
            knowledge_base_table.c.status == "LIVE",
            exists().where(
                and_(
                    kb_grant_table.c.tenant_id == actor.tenant_id,
                    kb_grant_table.c.kb_id == knowledge_base_table.c.id,
                    principal_matches(kb_grant_table, actor),
                )
            ),
        )
    )
    doc_acl_ok = or_(
        document_table.c.acl_mode == "INHERIT",
        exists().where(
            and_(
                document_grant_table.c.tenant_id == actor.tenant_id,
                document_grant_table.c.document_id == document_table.c.id,
                principal_matches(document_grant_table, actor),
            )
        ),
    )
    stmt = select(document_table.c.active_build_id).where(
        and_(
            document_table.c.tenant_id == actor.tenant_id,
            document_table.c.status == "LIVE",
            document_table.c.active_build_id.isnot(None),
            user_active,
            kb_accessible,
            doc_acl_ok,
        )
    )
    if kb_ids:
        stmt = stmt.where(document_table.c.kb_id.in_(kb_ids))

    return [str(r.active_build_id) for r in conn.execute(stmt).fetchall()]


