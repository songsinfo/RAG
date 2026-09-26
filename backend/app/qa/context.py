"""Authorized conversation history and immutable evaluation scope."""

from sqlalchemy import and_, select

from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import assert_hits_still_readable, can_read_document
from app.models import document_table, qa_run_table, run_source_table


def run_document_scope(conn, actor, run_id):
    budget = conn.execute(select(qa_run_table.c.budget_json).where(and_(
        qa_run_table.c.tenant_id == actor.tenant_id, qa_run_table.c.id == run_id,
    ))).scalar() or {}
    scope = budget.get("evaluationScope")
    if scope is None:
        return None
    documents = scope.get("documents", [])
    expected = {str(d["documentId"]): str(d["buildId"]) for d in documents}
    rows = conn.execute(select(document_table.c.id, document_table.c.active_build_id).where(and_(
        document_table.c.tenant_id == actor.tenant_id,
        document_table.c.id.in_(expected), document_table.c.status == "LIVE",
    ))).fetchall()
    if not expected or {str(r.id): str(r.active_build_id) for r in rows} != expected:
        raise ApiException(409, "SOURCE_CHANGED", "评测快照的文档或生效构建已变化")
    return list(expected)


def assert_run_sources_readable(actor, run_id):
    with get_db_connection() as conn:
        document_ids = run_document_scope(conn, actor, run_id)
        rows = conn.execute(select(run_source_table.c.document_id, run_source_table.c.build_id, run_source_table.c.purpose).where(and_(
            run_source_table.c.tenant_id == actor.tenant_id, run_source_table.c.run_id == run_id,
        )).distinct()).fetchall()
        for row in rows:
            if row.purpose == "HISTORY" and not can_read_document(conn, actor, str(row.document_id)):
                raise ApiException(409, "SOURCE_CHANGED", "历史来源权限已变化")
        assert_hits_still_readable(conn, actor, [
            {"documentId": r.document_id, "buildId": r.build_id} for r in rows
            if r.purpose != "HISTORY" or document_ids is not None
        ], document_ids=document_ids)


def conversation_memory(actor, conversation_id, current_run_id, max_turns=4):
    from app.qa.service import QaService
    with get_db_connection() as conn:
        segment_id = conn.execute(select(qa_run_table.c.segment_id).where(and_(
            qa_run_table.c.tenant_id == actor.tenant_id,
            qa_run_table.c.id == current_run_id,
            qa_run_table.c.conversation_id == conversation_id,
            qa_run_table.c.owner_id == actor.user_id,
        ))).scalar()
    if not segment_id:
        return []
    return QaService._rag_history(actor, str(segment_id), current_run_id)
