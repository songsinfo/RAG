"""Authorized conversation history and immutable evaluation scope."""
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select

from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import assert_hits_still_readable
from app.models import document_table, qa_run_table, run_source_table
from app.qa.execution import qa_lease_is_current


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
        rows = conn.execute(select(run_source_table.c.document_id, run_source_table.c.build_id).where(and_(
            run_source_table.c.tenant_id == actor.tenant_id, run_source_table.c.run_id == run_id,
        )).distinct()).fetchall()
        assert_hits_still_readable(conn, actor, [
            {"documentId": r.document_id, "buildId": r.build_id} for r in rows
        ], document_ids=document_ids)


def conversation_memory(actor, conversation_id, current_run_id, max_turns=4):
    with get_db_connection() as conn:
        current = conn.execute(select(qa_run_table.c.segment_id, qa_run_table.c.turn_no).where(and_(
            qa_run_table.c.tenant_id == actor.tenant_id,
            qa_run_table.c.id == current_run_id,
            qa_run_table.c.conversation_id == conversation_id,
            qa_run_table.c.owner_id == actor.user_id,
        ))).first()
        if not current:
            return []
        history_filter = and_(
            qa_run_table.c.tenant_id == actor.tenant_id,
            qa_run_table.c.segment_id == current.segment_id,
            qa_run_table.c.owner_id == actor.user_id,
            qa_run_table.c.turn_no < current.turn_no,
        )
        rows = conn.execute(select(qa_run_table.c.question, qa_run_table.c.result_json).where(and_(
            history_filter, qa_run_table.c.status.in_(["SUCCEEDED", "COMPLETED"]),
        )).order_by(qa_run_table.c.turn_no.desc()).limit(max_turns)).fetchall()
        if not rows:
            return []
        # A later answer may depend on any earlier source in this segment.
        sources = conn.execute(select(run_source_table.c.document_id, run_source_table.c.build_id)
            .select_from(run_source_table.join(qa_run_table, and_(
                run_source_table.c.tenant_id == qa_run_table.c.tenant_id,
                run_source_table.c.run_id == qa_run_table.c.id,
            ))).where(history_filter).distinct()).fetchall()
        hits = [{"documentId": s.document_id, "buildId": s.build_id} for s in sources]
        try:
            assert_hits_still_readable(conn, actor, hits, document_ids=run_document_scope(conn, actor, current_run_id))
        except ApiException as exc:
            if exc.code == "SOURCE_CHANGED":
                return []
            raise
        if not qa_lease_is_current(conn, actor.tenant_id, current_run_id):
            raise ApiException(409, "JOB_LEASE_LOST", "问答任务租约已失效")
        existing = set(conn.execute(select(run_source_table.c.document_id, run_source_table.c.build_id).where(and_(
            run_source_table.c.tenant_id == actor.tenant_id, run_source_table.c.run_id == current_run_id,
        ))).tuples().all())
        now = datetime.now(timezone.utc)
        for source in sources:
            if (source.document_id, source.build_id) not in existing:
                conn.execute(run_source_table.insert().values(
                    tenant_id=actor.tenant_id, id=str(uuid.uuid4()), run_id=current_run_id,
                    document_id=source.document_id, build_id=source.build_id,
                    purpose="CONTEXT", span_json={}, payload_hash=hashlib.sha256(b"history").digest(),
                    send_status="SENT", created_at=now, updated_at=now,
                ))
        conn.commit()
    messages = []
    for row in reversed(rows):
        if row.question:
            messages.append({"role": "user", "content": str(row.question)[:2000]})
        answer = (row.result_json or {}).get("answer")
        if answer:
            messages.append({"role": "assistant", "content": str(answer)[:3000]})
    return messages
