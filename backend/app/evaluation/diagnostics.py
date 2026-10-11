"""Private, evaluation-only retrieval diagnostics.

Artifacts are not sent to Langfuse or regular logs; the event stores only the
opaque storage key. All retrieval IDs are reauthorized before capture and read.
"""
import json
import logging
import os
from typing import Any

from sqlalchemy import and_, select

from app.db import get_db_connection
from app.identity.deps import assert_hits_still_readable
from app.knowledge.artifacts import save_artifact, read_artifact
from app.models import qa_run_table, run_event_table

logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 1


def candidate_snapshot(stage: str, candidates: list[dict], *, limit: int,
                       retrieval_index: int, context: bool = False) -> dict:
    """Canonical stable order; missing offsets remain explicitly unknown."""
    selected = candidates[:max(0, limit)]
    items = []
    for rank, hit in enumerate(selected, 1):
        units = hit.get("sourceUnits") or []
        items.append({
            "rank": rank,
            "chunkId": hit.get("chunkId"),
            "buildId": hit.get("buildId"),
            "parentChunkId": hit.get("parentChunkId"),
            "sourceType": hit.get("sourceType", "DOCUMENT"),
            "sourceUnits": [
                {"unitId": unit.get("unitId"), "contextStart": unit.get("contextStart"),
                 "contextEnd": unit.get("contextEnd"), "contextMapped": unit.get("contextMapped", False),
                 "location": unit.get("location")}
                for unit in units if isinstance(unit, dict)
            ],
        })
    return {"schemaVersion": _SCHEMA_VERSION, "stage": stage,
            "retrievalIndex": retrieval_index, "candidateCount": len(candidates),
            "capturedCount": len(items), "complete": len(candidates) <= limit,
            "items": items, "contextState": "RETRIEVAL_OUTPUT" if context else "CANDIDATE_STAGE"}


def capture_eval_stage(ctx: Any, stage: str, candidates: list[dict],
                       limit: int, *, context: bool = False):
    """Best-effort side path: never mutates search ordering, ACL, or QA status."""
    if not ctx.run_id or not candidates:
        return
    try:
        with get_db_connection() as conn:
            run = conn.execute(select(qa_run_table.c.owner_id, qa_run_table.c.budget_json).where(and_(
                qa_run_table.c.tenant_id == ctx.actor.tenant_id,
                qa_run_table.c.id == ctx.run_id))).first()
            budget = (run.budget_json or {}) if run else {}
            scope = budget.get("evaluationScope") or {}
            if not scope or not run or str(run.owner_id) != ctx.actor.user_id:
                return
        from app.evaluation.catalog import EvaluationCatalog
        EvaluationCatalog.check_snapshot(ctx.actor, scope["snapshotId"], check_current=True)
        # Do not persist an unapproved candidate (including orphan or stale builds).
        with get_db_connection() as conn:
            assert_hits_still_readable(conn, ctx.actor, candidates)
        data = candidate_snapshot(stage, candidates, limit=limit,
                                  retrieval_index=ctx.retrieval_index, context=context)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        key, _, _ = save_artifact(ctx.actor.tenant_id, "eval-retrieval-diagnostic.json", encoded)
        # No source ID or text appears in run_event; just an opaque private artifact locator.
        if ctx.record_event_fn:
            ctx.record_event_fn(ctx.actor.tenant_id, ctx.run_id, "EVAL_DIAG_ARTIFACT", {
                "schemaVersion": _SCHEMA_VERSION, "retrievalIndex": ctx.retrieval_index,
                "stage": stage, "artifactKey": key,
                "complete": data["complete"], "candidateCount": data["candidateCount"],
                "capturedCount": data["capturedCount"]})
    except Exception:
        logger.warning("eval_diagnostic_capture_skipped")


def read_eval_diagnostics(actor, run_id: str) -> list[dict]:
    """Admin-only, persona-rechecked access to previously persisted artifacts."""
    if not actor.admin:
        from app.errors import ApiException
        raise ApiException(403, "OPERATION_FORBIDDEN", "仅管理员可读取评估诊断")
    from app.qa.service import QaService
    with get_db_connection() as conn:
        run = conn.execute(select(qa_run_table.c.owner_id, qa_run_table.c.budget_json).where(and_(
            qa_run_table.c.tenant_id == actor.tenant_id,
            qa_run_table.c.id == run_id))).first()
        if not run:
            from app.errors import ApiException
            raise ApiException(404, "RESOURCE_NOT_FOUND", "运行不存在")
        budget = run.budget_json or {}
        scope = budget.get("evaluationScope")
        if not scope:
            from app.errors import ApiException
            raise ApiException(403, "OPERATION_FORBIDDEN", "不是评估运行")
        events = conn.execute(select(run_event_table.c.payload).where(and_(
            run_event_table.c.tenant_id == actor.tenant_id,
            run_event_table.c.run_id == run_id,
            run_event_table.c.stage == "EVAL_DIAG_ARTIFACT")).order_by(
                run_event_table.c.sequence_no.asc())).fetchall()
    from app.identity.deps import Actor
    from app.evaluation.catalog import EvaluationCatalog
    persona = Actor(actor.tenant_id, str(run.owner_id), False)
    EvaluationCatalog.check_snapshot(persona, scope["snapshotId"], check_current=True)
    detail = QaService.get_run(persona, run_id)
    if detail.get("visibility") == "HIDDEN":
        from app.errors import ApiException
        raise ApiException(403, "SOURCE_CHANGED", "权限或来源已改变")
    result = []
    for row in events:
        key = (row.payload or {}).get("artifactKey")
        if not key:
            continue
        try:
            data = json.loads(read_artifact(str(key)))
            items = data.get("items") or []
            with get_db_connection() as conn:
                assert_hits_still_readable(conn, persona, [
                    {"documentId": None, "buildId": item.get("buildId"),
                     "chunkId": item.get("chunkId")} for item in items])
            result.append(data)
        except Exception:
            result.append({"schemaVersion": _SCHEMA_VERSION, "status": "UNAVAILABLE"})
    return result
