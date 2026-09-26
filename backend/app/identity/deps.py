from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request, Depends
from sqlalchemy import and_, exists, or_, select

from app.db import get_db_connection
from app.errors import ApiException
from app.identity.security import hash_token
from app.models import (
    app_user_table,
    auth_session_table,
    document_grant_table,
    document_table,
    group_member_table,
    index_build_table,
    kb_grant_table,
    knowledge_base_table,
    tenant_table,
)


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    user_id: str
    admin: bool
    security_epoch: int = 0


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
                tenant_table.c.security_epoch,
            )
            .select_from(
                auth_session_table.join(
                    app_user_table,
                    and_(
                        app_user_table.c.tenant_id == auth_session_table.c.tenant_id,
                        app_user_table.c.id == auth_session_table.c.user_id,
                    ),
                ).join(tenant_table, tenant_table.c.id == app_user_table.c.tenant_id)
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
            security_epoch=int(row.security_epoch or 0),
        )


def require_admin(actor: Actor = Depends(get_current_actor)) -> Actor:
    if not actor.admin:
        raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
    return actor


def principal_matches(grant_table, actor: Actor):
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


def _access_predicates(actor: Actor):
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
    return user_active, kb_accessible, doc_acl_ok


def can_read_document(conn, actor: Actor, doc_id: str) -> bool:
    user_active, kb_accessible, doc_acl_ok = _access_predicates(actor)
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


def get_readable_build_records(
    conn,
    actor: Actor,
    kb_ids: Optional[List[str]] = None,
    document_ids: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    user_active, kb_accessible, doc_acl_ok = _access_predicates(actor)
    stmt = (
        select(
            document_table.c.active_build_id,
            document_table.c.id.label("document_id"),
            document_table.c.kb_id,
            index_build_table.c.version_id,
            index_build_table.c.fence,
        )
        .select_from(
            document_table.join(
                index_build_table,
                and_(
                    index_build_table.c.tenant_id == document_table.c.tenant_id,
                    index_build_table.c.id == document_table.c.active_build_id,
                ),
            )
        )
        .where(
            and_(
                document_table.c.tenant_id == actor.tenant_id,
                document_table.c.status == "LIVE",
                document_table.c.active_build_id.isnot(None),
                index_build_table.c.status == "READY",
                user_active,
                kb_accessible,
                doc_acl_ok,
            )
        )
    )
    if kb_ids:
        stmt = stmt.where(document_table.c.kb_id.in_(kb_ids))
    if document_ids is not None:
        stmt = stmt.where(document_table.c.id.in_(document_ids))

    rows = conn.execute(stmt).fetchall()
    return [
        {
            "buildId": str(r.active_build_id),
            "documentId": str(r.document_id),
            "kbId": str(r.kb_id),
            "versionId": str(r.version_id),
            "indexEpoch": int(r.fence or 0),
        }
        for r in rows
    ]


def get_readable_builds(conn, actor: Actor, kb_ids=None, document_ids=None) -> list:
    return [r["buildId"] for r in get_readable_build_records(conn, actor, kb_ids, document_ids)]


def assert_hits_still_readable(
    conn,
    actor: Actor,
    hits: List[Dict[str, Any]],
    kb_ids: Optional[List[str]] = None,
    document_ids: Optional[List[str]] = None,
) -> None:
    if not hits:
        return
    candidate_ids = list({str(h.get("documentId") or "") for h in hits})
    if document_ids is not None:
        candidate_ids = list(set(candidate_ids).intersection(document_ids))
    readable = get_readable_build_records(conn, actor, kb_ids, candidate_ids)
    allowed = {
        (r["buildId"], r["documentId"]): int(r.get("indexEpoch") or 0)
        for r in readable
    }
    for hit in hits:
        build_id = str(hit.get("buildId") or "")
        document_id = str(hit.get("documentId") or "")
        expected_epoch = allowed.get((build_id, document_id))
        hit_epoch = hit.get("indexEpoch")
        if not build_id or not document_id or expected_epoch is None:
            raise ApiException(409, "SOURCE_CHANGED", "检索来源权限或生效版本已变化，请重新提问")
        if expected_epoch > 0 and hit_epoch is not None and int(hit_epoch) != expected_epoch:
            raise ApiException(409, "SOURCE_CHANGED", "检索索引代次已变化，请重新提问")
