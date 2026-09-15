from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Request, Depends, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, and_, text

from app.config import settings
from app.db import get_db_connection
from app.errors import ApiException, api_exception_handler
from app.identity.deps import Actor, get_current_actor
from app.models import tenant_table, app_user_table, user_group_table, group_member_table


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure DB connection is reachable
    try:
        with get_db_connection() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        print(f"Warning: Database startup check failed: {e}")
    yield


app = FastAPI(
    title="RAG Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_exception_handler(ApiException, api_exception_handler)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    # Only protect mutating HTTP methods on API routes
    exempt_paths = {"/api/v1/auth/csrf", "/api/v1/health", "/health", "/docs", "/openapi.json"}
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.url.path.startswith("/api/v1"):
        if request.url.path not in exempt_paths:
            # 1. Check Origin if provided
            origin = request.headers.get("origin")
            if origin:
                parsed = urlparse(origin)
                req_host = request.url.hostname
                req_port = request.url.port or (443 if request.url.scheme == "https" else 80)
                origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if parsed.hostname != req_host or origin_port != req_port or parsed.scheme != request.url.scheme:
                    return JSONResponse(status_code=403, content={"code": "CSRF_INVALID", "message": "请刷新安全凭据后重试"})

            # 2. Extract CSRF token from header and cookie
            csrf_header = request.headers.get("x-csrf-token")
            csrf_cookie = request.cookies.get("RAG_CSRF")
            if not csrf_header or not csrf_cookie or csrf_header != csrf_cookie:
                return JSONResponse(status_code=403, content={"code": "CSRF_INVALID", "message": "请刷新安全凭据后重试"})

            # 3. Determine binding
            session_token = request.cookies.get("RAG_SESSION")
            binding = session_token if session_token else request.cookies.get("RAG_NONCE", "")
            if not verify_csrf_token(csrf_header, binding, settings.RAG_CSRF_SECRET):
                return JSONResponse(status_code=403, content={"code": "CSRF_INVALID", "message": "请刷新安全凭据后重试"})

    response = await call_next(request)
    return response


from app.identity.router import router as auth_router
from app.knowledge.router import router as knowledge_router
from app.retrieval.router import router as retrieval_router
from app.identity.security import verify_csrf_token

# Include routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(retrieval_router, prefix="/api/v1")


@app.get("/api/v1/me")
def me(actor: Actor = Depends(get_current_actor)):
    with get_db_connection() as conn:
        user_stmt = select(app_user_table.c.login_name).where(
            and_(
                app_user_table.c.tenant_id == actor.tenant_id,
                app_user_table.c.id == actor.user_id,
            )
        )
        user = conn.execute(user_stmt).first()
        if not user:
            raise ApiException(401, "AUTH_REQUIRED", "用户不存在")

        epoch_stmt = select(tenant_table.c.security_epoch).where(
            tenant_table.c.id == actor.tenant_id
        )
        epoch = conn.execute(epoch_stmt).scalar() or 0

        groups_stmt = (
            select(user_group_table.c.id, user_group_table.c.name)
            .select_from(
                user_group_table.join(
                    group_member_table,
                    and_(
                        group_member_table.c.tenant_id == user_group_table.c.tenant_id,
                        group_member_table.c.group_id == user_group_table.c.id,
                    ),
                )
            )
            .where(
                and_(
                    group_member_table.c.tenant_id == actor.tenant_id,
                    group_member_table.c.user_id == actor.user_id,
                )
            )
        )
        groups = conn.execute(groups_stmt).fetchall()

        return {
            "userId": actor.user_id,
            "tenantId": actor.tenant_id,
            "loginName": user.login_name,
            "admin": actor.admin,
            "securityEpoch": epoch,
            "groups": [{"id": str(g.id), "name": str(g.name)} for g in groups],
        }


@app.get("/api/v1/health")
@app.get("/health")
def health():
    with get_db_connection() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "db": "connected"}
