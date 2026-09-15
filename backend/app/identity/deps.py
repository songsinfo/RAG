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
