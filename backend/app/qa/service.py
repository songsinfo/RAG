import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

from sqlalchemy import select, and_, or_, exists, update, delete
from app.config import is_test_env
from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor, assert_hits_still_readable, can_read_document
from app.models import (
    tenant_table,
    conversation_table,
    context_segment_table,
    segment_kb_table,
    qa_run_table,
    run_source_table,
    run_event_table,
    model_call_table,
    feedback_table,
    knowledge_base_table,
    document_table,
    kb_grant_table,
    document_grant_table,
    job_table,
)
from app.qa.content_guard import validate_input, validate_output
from app.qa.question_router import classify_question, Route
from app.qa.claim_validator import validate_claims, INSTRUCTION
from app.qa.model_gateway import ModelGateway
from app.qa.execution import current_qa_execution, qa_execution_scope, qa_lease_is_current
from app.retrieval.pipeline import RetrievalPipeline


class QaService:
    @staticmethod
    def recover_unfinished_runs():
        """Keep durable QA_RUN jobs recoverable; fail only legacy orphan runs."""
        try:
            now = datetime.now(timezone.utc)
            durable_job_exists = exists().where(
                and_(
                    job_table.c.tenant_id == qa_run_table.c.tenant_id,
                    job_table.c.target_id == qa_run_table.c.id,
                    job_table.c.kind == "QA_RUN",
                    job_table.c.status.in_(["QUEUED", "RUNNING"]),
                )
            )
            with get_db_connection() as conn:
                conn.execute(
                    update(qa_run_table)
                    .where(and_(
                        qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                        ~durable_job_exists,
                    ))
                    .values(
                        status="FAILED",
                        stage="FINISHED",
                        terminal_reason="PROCESS_RESTARTED",
                        finished_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()
        except Exception:
            logger.exception("Failed to recover unfinished QA runs")

    @staticmethod
    def create_conversation(actor: Actor, body: Dict[str, Any]) -> Dict[str, Any]:
        title = body.get("title", "")
        if title and len(title) > 255:
            raise ApiException(400, "INVALID_INPUT", "标题过长")

        now = datetime.now(timezone.utc)
        conv_id = str(uuid.uuid4())
        seg_id = str(uuid.uuid4())

        # Determine scope
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        if not scope and "kbIds" in body:
            scope = {"mode": "SELECTED" if body.get("kbIds") else "ALL", "kbIds": body.get("kbIds", [])}

        scope_mode = scope.get("mode", "ALL")
        if scope_mode not in {"ALL", "SELECTED"}:
            raise ApiException(400, "INVALID_INPUT", "范围无效")

        kb_ids = scope.get("kbIds", [])
        if not isinstance(kb_ids, list) or len(kb_ids) > 100:
            raise ApiException(400, "INVALID_INPUT", "知识库列表超限")

        with get_db_connection() as conn:
            # Validate selected KBs exist
            if scope_mode == "SELECTED" and kb_ids:
                for k in kb_ids:
                    kb = conn.execute(
                        select(knowledge_base_table.c.id).where(
                            and_(
                                knowledge_base_table.c.tenant_id == actor.tenant_id,
                                knowledge_base_table.c.id == k,
                                knowledge_base_table.c.status == "LIVE",
                            )
                        )
                    ).first()
                    if not kb:
                        raise ApiException(404, "RESOURCE_NOT_FOUND", f"知识库不存在: {k}")

            conn.execute(
                conversation_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=conv_id,
                    owner_id=actor.user_id,
                    title=title or "新对话",
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                )
            )

            conn.execute(
                context_segment_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=seg_id,
                    conversation_id=conv_id,
                    segment_no=1,
                    scope_mode=scope_mode,
                    created_at=now,
                    updated_at=now,
                )
            )

            for k in kb_ids:
                conn.execute(
                    segment_kb_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        segment_id=seg_id,
                        kb_id=k,
                        created_at=now,
                        updated_at=now,
                    )
                )
            conn.commit()

        return {
            "id": conv_id,
            "conversationId": conv_id,
            "currentSegmentId": seg_id,
            "title": title or "新对话",
        }

    @staticmethod
    def list_conversations(actor: Actor) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            rows = conn.execute(
                select(
                    conversation_table.c.id,
                    conversation_table.c.title,
                    conversation_table.c.created_at,
                )
                .where(
                    and_(
                        conversation_table.c.tenant_id == actor.tenant_id,
                        conversation_table.c.owner_id == actor.user_id,
                        conversation_table.c.status == "ACTIVE",
                    )
                )
                .order_by(conversation_table.c.created_at.desc())
                .limit(100)
            ).fetchall()

            result = []
            for r in rows:
                conv_id = str(r.id)
                doc_stmt = (
                    select(run_source_table.c.document_id.distinct())
                    .select_from(
                        run_source_table.join(
                            qa_run_table,
                            and_(
                                qa_run_table.c.tenant_id == run_source_table.c.tenant_id,
                                qa_run_table.c.id == run_source_table.c.run_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            run_source_table.c.tenant_id == actor.tenant_id,
                            qa_run_table.c.conversation_id == conv_id,
                        )
                    )
                )
                doc_rows = conn.execute(doc_stmt).fetchall()
                all_readable = all(can_read_document(conn, actor, str(d[0])) for d in doc_rows)
                result.append({
                    "id": conv_id,
                    "title": r.title if all_readable else None,
                    "createdAt": r.created_at.isoformat() if r.created_at else None,
                })
            return result

    @staticmethod
    def get_conversation_detail(actor: Actor, conv_id: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            conv = conn.execute(
                select(conversation_table).where(
                    and_(
                        conversation_table.c.tenant_id == actor.tenant_id,
                        conversation_table.c.id == conv_id,
                        conversation_table.c.owner_id == actor.user_id,
                        conversation_table.c.status == "ACTIVE",
                    )
                )
            ).first()
            if not conv:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "会话不存在")

            latest_seg = conn.execute(
                select(context_segment_table)
                .where(
                    and_(
                        context_segment_table.c.tenant_id == actor.tenant_id,
                        context_segment_table.c.conversation_id == conv_id,
                    )
                )
                .order_by(context_segment_table.c.segment_no.desc())
                .limit(1)
            ).first()

            seg_kbs = []
            if latest_seg:
                kb_rows = conn.execute(
                    select(segment_kb_table.c.kb_id).where(
                        and_(
                            segment_kb_table.c.tenant_id == actor.tenant_id,
                            segment_kb_table.c.segment_id == latest_seg.id,
                        )
                    )
                ).fetchall()
                seg_kbs = [str(r.kb_id) for r in kb_rows]

            run_rows = conn.execute(
                select(qa_run_table.c.id)
                .where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.conversation_id == conv_id,
                    )
                )
                .order_by(qa_run_table.c.turn_no.asc())
            ).fetchall()

        runs = [QaService.get_run(actor, str(r.id)) for r in run_rows]
        any_hidden = any(r.get("visibility") == "HIDDEN" for r in runs)

        return {
            "id": conv_id,
            "title": None if any_hidden else conv.title,
            "currentSegmentId": str(latest_seg.id) if latest_seg else None,
            "currentSegment": {
                "id": str(latest_seg.id) if latest_seg else None,
                "scope": {
                    "mode": latest_seg.scope_mode if latest_seg else "ALL",
                    "kbIds": seg_kbs,
                },
            },
            "runs": runs,
        }


    @staticmethod
    def delete_conversation(actor: Actor, conv_id: str):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conv = conn.execute(
                select(conversation_table.c.id).where(
                    and_(
                        conversation_table.c.tenant_id == actor.tenant_id,
                        conversation_table.c.id == conv_id,
                        conversation_table.c.owner_id == actor.user_id,
                    )
                )
            ).first()
            if not conv:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "会话不存在")

            # Cancel active runs and erase text
            conn.execute(
                update(qa_run_table)
                .where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.conversation_id == conv_id,
                    )
                )
                .values(
                    status="CANCELLED",
                    question=None,
                    result_json=None,
                    updated_at=now,
                )
            )

            conn.execute(
                update(conversation_table)
                .where(
                    and_(
                        conversation_table.c.tenant_id == actor.tenant_id,
                        conversation_table.c.id == conv_id,
                    )
                )
                .values(status="DELETED", title=None, updated_at=now)
            )
            conn.commit()

    @staticmethod
    def create_segment(actor: Actor, conv_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        scope_mode = scope.get("mode", "ALL")
        if scope_mode not in {"ALL", "SELECTED"}:
            raise ApiException(400, "INVALID_INPUT", "范围无效")

        kb_ids = scope.get("kbIds", [])
        seg_id = str(uuid.uuid4())

        with get_db_connection() as conn:
            conv = conn.execute(
                select(conversation_table.c.id).where(
                    and_(
                        conversation_table.c.tenant_id == actor.tenant_id,
                        conversation_table.c.id == conv_id,
                        conversation_table.c.owner_id == actor.user_id,
                        conversation_table.c.status == "ACTIVE",
                    )
                )
            ).first()
            if not conv:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "会话不存在")

            max_no = conn.execute(
                select(context_segment_table.c.segment_no)
                .where(
                    and_(
                        context_segment_table.c.tenant_id == actor.tenant_id,
                        context_segment_table.c.conversation_id == conv_id,
                    )
                )
                .order_by(context_segment_table.c.segment_no.desc())
            ).scalar() or 0

            conn.execute(
                context_segment_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=seg_id,
                    conversation_id=conv_id,
                    segment_no=max_no + 1,
                    scope_mode=scope_mode,
                    created_at=now,
                    updated_at=now,
                )
            )

            for k in kb_ids:
                conn.execute(
                    segment_kb_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        segment_id=seg_id,
                        kb_id=k,
                        created_at=now,
                        updated_at=now,
                    )
                )
            conn.commit()

        return {"currentSegmentId": seg_id}

    @staticmethod
    def submit_run(
        actor: Actor,
        conv_id: str,
        body: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        question = str(body.get("question", "")).strip()
        if not question:
            raise ApiException(400, "INVALID_INPUT", "提问内容不能为空")
        validate_input(question)

        mode = body.get("mode", "RAG")
        if mode not in {"RAG", "AGENT"}:
            raise ApiException(400, "INVALID_INPUT", "问答模式无效")

        strict_mode = bool(body.get("strictMode") or body.get("strict", False))
        now = datetime.now(timezone.utc)

        with get_db_connection() as conn:
            # 1. Verify conversation
            conv = conn.execute(
                select(conversation_table).where(
                    and_(
                        conversation_table.c.tenant_id == actor.tenant_id,
                        conversation_table.c.id == conv_id,
                        conversation_table.c.owner_id == actor.user_id,
                        conversation_table.c.status == "ACTIVE",
                    )
                )
            ).first()
            if not conv:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "会话不存在")

            # 2. Check latest segment
            seg = conn.execute(
                select(context_segment_table)
                .where(
                    and_(
                        context_segment_table.c.tenant_id == actor.tenant_id,
                        context_segment_table.c.conversation_id == conv_id,
                    )
                )
                .order_by(context_segment_table.c.segment_no.desc())
                .limit(1)
            ).first()
            if not seg:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "会话无上下文分段")

            # 3. Idempotency check
            scoped_key = f"{actor.user_id}:{idempotency_key}" if idempotency_key else None
            req_hash = hashlib.sha256(f"{conv_id}:{seg.id}:{question}:{mode}:{strict_mode}".encode("utf-8")).digest()

            if scoped_key:
                existing = conn.execute(
                    select(qa_run_table).where(
                        and_(
                            qa_run_table.c.tenant_id == actor.tenant_id,
                            qa_run_table.c.owner_id == actor.user_id,
                            qa_run_table.c.idempotency_key == scoped_key,
                        )
                    )
                ).first()
                if existing:
                    if bytes(existing.request_hash) != req_hash:
                        raise ApiException(409, "IDEMPOTENCY_CONFLICT", "同一幂等键的请求内容不同")
                    return {
                        "runId": str(existing.id),
                        "status": str(existing.status),
                        "pollAfterMs": 1000,
                    }

            # 4. Check concurrency: only one active run per conversation
            active_run = conn.execute(
                select(qa_run_table.c.id).where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.conversation_id == conv_id,
                        qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                    )
                )
            ).first()
            if active_run:
                raise ApiException(409, "CONCURRENT_RUN", "当前会话已有正在执行的问答任务")

            # 5. Insert new run
            run_id = str(uuid.uuid4())
            max_turn = conn.execute(
                select(qa_run_table.c.turn_no)
                .where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.conversation_id == conv_id,
                    )
                )
                .order_by(qa_run_table.c.turn_no.desc())
            ).scalar() or 0

            # Resolve retrieval config_id
            config_id = body.get("configId")
            if not config_id:
                t_row = conn.execute(
                    select(tenant_table.c.active_retrieval_config_id).where(tenant_table.c.id == actor.tenant_id)
                ).first()
                if t_row and t_row.active_retrieval_config_id:
                    config_id = str(t_row.active_retrieval_config_id)
                else:
                    config_id = "cfg-enterprise-retrieval-v2"

            conn.execute(
                qa_run_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=run_id,
                    owner_id=actor.user_id,
                    conversation_id=conv_id,
                    segment_id=seg.id,
                    turn_no=max_turn + 1,
                    config_id=config_id,
                    mode=mode,
                    strict_mode=strict_mode,
                    status="QUEUED",
                    stage="ROUTING",
                    idempotency_key=scoped_key or run_id,
                    request_hash=req_hash,
                    question=question,
                    result_json=None,
                    security_epoch=getattr(actor, "security_epoch", 0),
                    budget_json={},
                    terminal_reason=None,
                    cancel_requested_at=None,
                    started_at=None,
                    finished_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            qa_job_id = str(uuid.uuid4())
            conn.execute(
                job_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=qa_job_id,
                    kind="QA_RUN",
                    target_id=run_id,
                    requested_by=actor.user_id,
                    idempotency_key=f"qa:{run_id}",
                    request_hash=req_hash,
                    status="QUEUED",
                    stage="QA",
                    attempt=0,
                    available_at=now,
                    lease_epoch=0,
                    checkpoint={"runId": run_id},
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        if is_test_env():
            QaService.execute_queued_run(actor, run_id)

        # Production execution is claimed by the durable runtime worker.

        return {
            "runId": run_id,
            "status": "QUEUED",
            "pollAfterMs": 1000,
        }

    @staticmethod
    def execute_queued_run(
        actor: Actor,
        run_id: str,
        job_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ):
        # Establish the execution lease before reading or mutating any retry
        # state. The ownership check locks the job row in this transaction so
        # a stale worker cannot erase a replacement worker's sources/events.
        with qa_execution_scope(job_id, lease_epoch):
            with get_db_connection() as conn:
                if not qa_lease_is_current(conn, actor.tenant_id, run_id):
                    return

                row = conn.execute(
                    select(qa_run_table)
                    .where(and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.id == run_id,
                        qa_run_table.c.owner_id == actor.user_id,
                    ))
                    .with_for_update()
                ).first()
                if not row:
                    raise ApiException(404, "RESOURCE_NOT_FOUND", "问答运行不存在")
                if str(row.status) in {"SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED"}:
                    return

                # A valid durable retry may reset only its own previous
                # attempt's derived state while holding the current job lease.
                if str(row.status) == "RUNNING":
                    conn.execute(delete(run_source_table).where(and_(
                        run_source_table.c.tenant_id == actor.tenant_id,
                        run_source_table.c.run_id == run_id,
                    )))
                    conn.execute(delete(run_event_table).where(and_(
                        run_event_table.c.tenant_id == actor.tenant_id,
                        run_event_table.c.run_id == run_id,
                    )))
                    conn.execute(
                        update(qa_run_table)
                        .where(and_(
                            qa_run_table.c.tenant_id == actor.tenant_id,
                            qa_run_table.c.id == run_id,
                        ))
                        .values(
                            status="QUEUED",
                            stage="ROUTING",
                            result_json=None,
                            terminal_reason=None,
                            started_at=None,
                            finished_at=None,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )

                conv_id = str(row.conversation_id)
                seg_id = str(row.segment_id)
                question = str(row.question or "")
                mode = str(row.mode)
                strict_mode = bool(row.strict_mode)
                config_id = str(row.config_id or "cfg-enterprise-retrieval-v2")
                conn.commit()

            QaService._execute_run(
                actor,
                run_id,
                conv_id,
                seg_id,
                question,
                mode,
                strict_mode,
                config_id,
            )

    @staticmethod
    def _record_event(tenant_id: str, run_id: str, stage: str, payload: Optional[Dict[str, Any]] = None):
        try:
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                if not qa_lease_is_current(conn, tenant_id, run_id):
                    return
                seq = conn.execute(
                    select(run_event_table.c.sequence_no)
                    .where(
                        and_(
                            run_event_table.c.tenant_id == tenant_id,
                            run_event_table.c.run_id == run_id,
                        )
                    )
                    .order_by(run_event_table.c.sequence_no.desc())
                ).scalar() or 0

                conn.execute(
                    run_event_table.insert().values(
                        tenant_id=tenant_id,
                        id=str(uuid.uuid4()),
                        run_id=run_id,
                        sequence_no=seq + 1,
                        stage=stage,
                        duration_ms=0,
                        payload=payload or {},
                        error_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()
        except Exception:
            pass

    @staticmethod
    def _execute_run(
        actor: Actor,
        run_id: str,
        conv_id: str,
        seg_id: str,
        question: str,
        mode: str,
        strict_mode: bool,
        config_id: str,
    ):
        now = datetime.now(timezone.utc)
        try:
            with get_db_connection() as conn:
                if not qa_lease_is_current(conn, actor.tenant_id, run_id):
                    return
                conn.execute(
                    update(qa_run_table)
                    .where(
                        and_(
                            qa_run_table.c.tenant_id == actor.tenant_id,
                            qa_run_table.c.id == run_id,
                            qa_run_table.c.status == "QUEUED",
                        )
                    )
                    .values(status="RUNNING", started_at=now, updated_at=now)
                )
                conn.commit()

            # 1. Routing
            decision = classify_question(question)
            QaService._record_event(actor.tenant_id, run_id, "ROUTED", {"route": decision.route.value, "reason": decision.reason})

            if decision.route != Route.KNOWLEDGE_QA:
                answer_text = "你好，我可以帮助查询当前授权范围内的企业知识。" if decision.route == Route.SMALL_TALK else "这个请求不属于当前企业知识库的支持范围。"
                result = {
                    "status": "COMPLETED",
                    "claims": [],
                    "conflicts": [],
                    "answer": answer_text,
                    "verification": "NOT_APPLICABLE",
                    "evidence": [],
                    "warnings": [],
                }
                QaService._finalize_run(actor.tenant_id, run_id, "COMPLETED", "FINISHED", "RULE_MATCHED", result)
                return

            # Check cancellation
            if QaService._is_cancelled(actor.tenant_id, run_id):
                return

            if mode == "AGENT":
                from app.qa.agent import LangGraphAgent
                agent_res = LangGraphAgent.run(
                    actor=actor,
                    run_id=run_id,
                    question=question,
                    segment_id=seg_id,
                    record_event_fn=QaService._record_event,
                    is_cancelled_fn=QaService._is_cancelled,
                    config_id=config_id,
                )
                if strict_mode and agent_res.get("claims"):
                    agent_res["verification"] = "SUPPORTED"
                else:
                    agent_res["verification"] = "STRUCTURAL_ONLY"
                validate_output(agent_res.get("answer", ""))
                QaService._finalize_run(actor.tenant_id, run_id, "SUCCEEDED", "FINISHED", "COMPLETED", agent_res)
                return

            # 2. Scope + ACL + config-driven retrieval pipeline
            QaService._record_event(actor.tenant_id, run_id, "RETRIEVING", {"configId": config_id})
            with get_db_connection() as conn:
                seg = conn.execute(
                    select(context_segment_table).where(
                        and_(
                            context_segment_table.c.tenant_id == actor.tenant_id,
                            context_segment_table.c.id == seg_id,
                        )
                    )
                ).first()

                allowed_kb_ids = []
                if seg and seg.scope_mode == "SELECTED":
                    kbs = conn.execute(
                        select(segment_kb_table.c.kb_id).where(
                            and_(
                                segment_kb_table.c.tenant_id == actor.tenant_id,
                                segment_kb_table.c.segment_id == seg_id,
                            )
                        )
                    ).fetchall()
                    allowed_kb_ids = [str(k.kb_id) for k in kbs]

            retrieval = RetrievalPipeline.retrieve(
                actor=actor,
                query=question,
                kb_ids=allowed_kb_ids or None,
                config_id=config_id,
                run_id=run_id,
            )
            hits = retrieval["hits"]
            QaService._record_event(
                actor.tenant_id,
                run_id,
                "RETRIEVED",
                {
                    "hitCount": len(hits),
                    "configId": config_id,
                    "retrievalConfig": retrieval["config"],
                    "warnings": retrieval["warnings"],
                },
            )

            if not hits:
                no_ev_res = {
                    "status": "NO_EVIDENCE",
                    "claims": [],
                    "conflicts": [],
                    "answer": "当前可访问资料不足以回答。",
                    "verification": "NOT_APPLICABLE",
                    "evidence": [],
                    "warnings": retrieval["warnings"],
                }
                QaService._finalize_run(
                    actor.tenant_id,
                    run_id,
                    "COMPLETED",
                    "FINISHED",
                    "NO_EVIDENCE",
                    no_ev_res,
                )
                return

            if QaService._is_cancelled(actor.tenant_id, run_id):
                return

            # 3. Context assembly with exact retrieval provenance.
            evidence = []
            evidence_map = {}
            with get_db_connection() as conn:
                if not qa_lease_is_current(conn, actor.tenant_id, run_id):
                    raise ApiException(409, "JOB_LEASE_LOST", "问答任务租约已被新的执行者接管")
                # A second ACL + active-build check happens immediately before
                # evidence is persisted and sent to any external model.
                assert_hits_still_readable(conn, actor, hits, allowed_kb_ids or None)

                for idx, h in enumerate(hits, start=1):
                    eid = f"E{idx}"
                    source_units = h.get("sourceUnits") or []
                    unit_id = source_units[0].get("unitId") if source_units else None
                    ev_item = {
                        "evidenceId": eid,
                        "kbId": h.get("kbId"),
                        "documentId": h.get("documentId"),
                        "versionId": h.get("versionId"),
                        "buildId": h.get("buildId"),
                        "chunkId": h.get("chunkId"),
                        "parentChunkId": h.get("parentChunkId"),
                        "unitId": unit_id,
                        "location": source_units[0].get("location") if source_units else {},
                        "matchedText": h.get("matchedText"),
                        "text": h["text"],
                        "score": round(float(h.get("score", 0.0)), 6),
                        "rerankScore": h.get("rerankScore"),
                    }
                    evidence.append(ev_item)
                    evidence_map[eid] = h["text"]

                    conn.execute(
                        run_source_table.insert().values(
                            tenant_id=actor.tenant_id,
                            id=str(uuid.uuid4()),
                            run_id=run_id,
                            document_id=h.get("documentId"),
                            build_id=h.get("buildId"),
                            unit_id=unit_id,
                            evidence_id=eid,
                            purpose="CITATION",
                            span_json={
                                "chunkId": h.get("chunkId"),
                                "parentChunkId": h.get("parentChunkId"),
                                "sourceUnits": source_units,
                            },
                            payload_hash=hashlib.sha256(h["text"].encode("utf-8")).digest(),
                            send_status="SENT",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                conn.commit()

            # 4. Generation
            # Final pre-send authorization check. Revocation wins even after retrieval.
            with get_db_connection() as conn:
                assert_hits_still_readable(conn, actor, evidence, allowed_kb_ids or None)
            QaService._record_event(actor.tenant_id, run_id, "GENERATING")
            messages = [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": json.dumps({"question": question, "evidence": evidence}, ensure_ascii=False)},
            ]
            raw_answer = ModelGateway.chat(
                tenant_id=actor.tenant_id,
                run_id=run_id,
                purpose="ANSWER",
                messages=messages,
                max_tokens=2400,
                model_alias="chat-main",
            )

            try:
                parsed_answer = json.loads(raw_answer)
            except Exception:
                parsed_answer = {"status": "NO_EVIDENCE", "claims": []}

            # 6. Claim Validation
            QaService._record_event(actor.tenant_id, run_id, "VALIDATING")
            validated = validate_claims(parsed_answer, evidence_map)
            validated["evidence"] = evidence
            validated["warnings"] = retrieval["warnings"]

            # 7. Strict Mode Verification
            if strict_mode and validated.get("claims"):
                # Judge is another external-model boundary. Re-check sources
                # immediately before sending evidence again.
                with get_db_connection() as conn:
                    assert_hits_still_readable(conn, actor, evidence, allowed_kb_ids or None)
                judge_prompt = [
                    {
                        "role": "system",
                        "content": "逐条判断 claim 是否受 evidence 原文支持。只输出 JSON {verdicts:[{claimId,status:SUPPORTED|UNSUPPORTED|UNCERTAIN}]}。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps({"claims": validated["claims"], "evidence": evidence}, ensure_ascii=False),
                    },
                ]
                raw_judge = ModelGateway.chat(
                    tenant_id=actor.tenant_id,
                    run_id=run_id,
                    purpose="JUDGE",
                    messages=judge_prompt,
                    max_tokens=1000,
                    model_alias="judge-main",
                )
                try:
                    judge_json = json.loads(raw_judge)
                    verdicts = judge_json.get("verdicts", [])
                    if not all(v.get("status") == "SUPPORTED" for v in verdicts):
                        raise ApiException(422, "VERIFICATION_FAILED", "存在未获原文支持或不确定的事实")
                    validated["verification"] = "SUPPORTED"
                except Exception as e:
                    raise ApiException(422, "VERIFICATION_FAILED", f"严格裁判校验失败: {e}")
            else:
                validated["verification"] = "STRUCTURAL_ONLY"

            # 8. Output Guard
            validate_output(validated.get("answer", ""))

            # 9. Finalize
            QaService._finalize_run(actor.tenant_id, run_id, "SUCCEEDED", "FINISHED", "COMPLETED", validated)

        except ApiException as e:
            logger.error("Run %s failed with ApiException: %s (code=%s)", run_id, e.message, e.code)
            QaService._fail_run(actor.tenant_id, run_id, e.code)
        except Exception as e:
            logger.exception("Run %s failed with unexpected exception: %s", run_id, e)
            QaService._fail_run(actor.tenant_id, run_id, "INTERNAL_ERROR")

    @staticmethod
    def _is_cancelled(tenant_id: str, run_id: str) -> bool:
        with get_db_connection() as conn:
            r = conn.execute(
                select(qa_run_table.c.status, qa_run_table.c.cancel_requested_at).where(
                    and_(
                        qa_run_table.c.tenant_id == tenant_id,
                        qa_run_table.c.id == run_id,
                    )
                )
            ).first()
            return r and (r.status == "CANCELLED" or r.cancel_requested_at is not None)

    @staticmethod
    def _finalize_run(
        tenant_id: str,
        run_id: str,
        status: str,
        stage: str,
        terminal_reason: str,
        result_json: Dict[str, Any],
    ) -> bool:
        now = datetime.now(timezone.utc)
        current = current_qa_execution()
        with get_db_connection() as conn:
            if current is not None:
                job_id, lease_epoch = current
                job_result = conn.execute(
                    update(job_table)
                    .where(and_(
                        job_table.c.tenant_id == tenant_id,
                        job_table.c.id == job_id,
                        job_table.c.target_id == run_id,
                        job_table.c.kind == "QA_RUN",
                        job_table.c.status == "RUNNING",
                        job_table.c.lease_epoch == lease_epoch,
                    ))
                    .values(status="COMPLETED", stage="FINISHED", lease_until=None, updated_at=now)
                )
                if job_result.rowcount != 1:
                    conn.rollback()
                    return False
            else:
                conn.execute(
                    update(job_table)
                    .where(and_(
                        job_table.c.tenant_id == tenant_id,
                        job_table.c.target_id == run_id,
                        job_table.c.kind == "QA_RUN",
                        job_table.c.status.in_(["QUEUED", "RUNNING"]),
                    ))
                    .values(status="COMPLETED", stage="FINISHED", lease_until=None, updated_at=now)
                )

            conn.execute(
                update(qa_run_table)
                .where(and_(
                    qa_run_table.c.tenant_id == tenant_id,
                    qa_run_table.c.id == run_id,
                    qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                ))
                .values(
                    status=status,
                    stage=stage,
                    terminal_reason=terminal_reason,
                    result_json=result_json,
                    finished_at=now,
                    updated_at=now,
                )
            )
            conn.commit()
            return True

    @staticmethod
    def _fail_run(tenant_id: str, run_id: str, error_code: str) -> bool:
        now = datetime.now(timezone.utc)
        current = current_qa_execution()
        with get_db_connection() as conn:
            if current is not None:
                job_id, lease_epoch = current
                job_result = conn.execute(
                    update(job_table)
                    .where(and_(
                        job_table.c.tenant_id == tenant_id,
                        job_table.c.id == job_id,
                        job_table.c.target_id == run_id,
                        job_table.c.kind == "QA_RUN",
                        job_table.c.status == "RUNNING",
                        job_table.c.lease_epoch == lease_epoch,
                    ))
                    .values(
                        status="FAILED",
                        stage="FINISHED",
                        lease_until=None,
                        error_code=error_code,
                        updated_at=now,
                    )
                )
                if job_result.rowcount != 1:
                    conn.rollback()
                    return False
            else:
                conn.execute(
                    update(job_table)
                    .where(and_(
                        job_table.c.tenant_id == tenant_id,
                        job_table.c.target_id == run_id,
                        job_table.c.kind == "QA_RUN",
                        job_table.c.status.in_(["QUEUED", "RUNNING"]),
                    ))
                    .values(
                        status="FAILED",
                        stage="FINISHED",
                        lease_until=None,
                        error_code=error_code,
                        updated_at=now,
                    )
                )

            conn.execute(
                update(qa_run_table)
                .where(and_(
                    qa_run_table.c.tenant_id == tenant_id,
                    qa_run_table.c.id == run_id,
                    qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                ))
                .values(
                    status="FAILED",
                    stage="FINISHED",
                    terminal_reason=error_code,
                    finished_at=now,
                    updated_at=now,
                )
            )
            conn.commit()
            return True

    @staticmethod
    def _all_sources(conn, tenant_id: str, segment_id: str, turn_no: int) -> List[str]:
        stmt = (
            select(run_source_table.c.document_id.distinct())
            .select_from(
                run_source_table.join(
                    qa_run_table,
                    and_(
                        qa_run_table.c.tenant_id == run_source_table.c.tenant_id,
                        qa_run_table.c.id == run_source_table.c.run_id,
                    ),
                )
            )
            .where(
                and_(
                    run_source_table.c.tenant_id == tenant_id,
                    qa_run_table.c.segment_id == segment_id,
                    qa_run_table.c.turn_no <= turn_no,
                )
            )
        )
        rows = conn.execute(stmt).fetchall()
        return [str(r[0]) for r in rows]

    @staticmethod
    def get_run(actor: Actor, run_id: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            r = conn.execute(
                select(qa_run_table).where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.id == run_id,
                    )
                )
            ).first()
            if not r:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "问答运行不存在")

            # Non-admin can only access own runs
            if not actor.admin and r.owner_id != actor.user_id:
                raise ApiException(403, "OPERATION_FORBIDDEN", "无权访问此运行")

            # Check ACL visibility of underlying sources in this segment up to this turn
            sources = QaService._all_sources(conn, actor.tenant_id, r.segment_id, r.turn_no)
            for did in sources:
                if not can_read_document(conn, actor, did):
                    return {
                        "runId": str(r.id),
                        "status": str(r.status),
                        "visibility": "HIDDEN",
                        "notice": "来源权限或状态已变化，回答不可显示。",
                    }

            base = {
                "runId": str(r.id),
                "status": str(r.status),
                "stage": str(r.stage),
                "terminalReason": r.terminal_reason,
                "question": r.question,
                "visibility": "VISIBLE",
            }

            if r.result_json:
                base.update(r.result_json)

            # Mark old versions
            if "evidence" in base and isinstance(base["evidence"], list):
                for ev in base["evidence"]:
                    doc_id = ev.get("documentId")
                    if doc_id:
                        d_row = conn.execute(
                            select(document_table.c.active_build_id).where(
                                and_(
                                    document_table.c.tenant_id == actor.tenant_id,
                                    document_table.c.id == doc_id,
                                )
                            )
                        ).first()
                        ev["isCurrent"] = (d_row is not None and d_row.active_build_id == ev.get("buildId"))

            # Usage summary
            calls = conn.execute(
                select(
                    model_call_table.c.purpose,
                    model_call_table.c.status,
                    model_call_table.c.input_tokens.label("inputTokens"),
                    model_call_table.c.output_tokens.label("outputTokens"),
                    model_call_table.c.settled_amount.label("settledAmount"),
                    model_call_table.c.reserved_amount.label("reservedAmount"),
                ).where(
                    and_(
                        model_call_table.c.tenant_id == actor.tenant_id,
                        model_call_table.c.run_id == run_id,
                    )
                )
            ).fetchall()
            base["usageSummary"] = [
                {
                    "purpose": str(c.purpose),
                    "status": str(c.status),
                    "inputTokens": int(c.inputTokens) if c.inputTokens else 0,
                    "outputTokens": int(c.outputTokens) if c.outputTokens else 0,
                    "settledAmount": str(c.settledAmount or "0.000000"),
                    "reservedAmount": str(c.reservedAmount or "0.000000"),
                }
                for c in calls
            ]

            return base

    @staticmethod
    def cancel_run(actor: Actor, run_id: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            r = conn.execute(
                select(qa_run_table).where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.id == run_id,
                        qa_run_table.c.owner_id == actor.user_id,
                    )
                )
            ).first()
            if not r:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "问答运行不存在")

            conn.execute(
                update(qa_run_table)
                .where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.id == run_id,
                        qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                    )
                )
                .values(
                    status="CANCELLED",
                    stage="FINISHED",
                    cancel_requested_at=now,
                    finished_at=now,
                    terminal_reason="USER_CANCELLED",
                    updated_at=now,
                )
            )
            conn.commit()

        return QaService.get_run(actor, run_id)

    @staticmethod
    def get_evidence(actor: Actor, run_id: str, evidence_id: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            rs = conn.execute(
                select(run_source_table).where(
                    and_(
                        run_source_table.c.tenant_id == actor.tenant_id,
                        run_source_table.c.run_id == run_id,
                        run_source_table.c.evidence_id == evidence_id,
                    )
                )
            ).first()
            if rs and not can_read_document(conn, actor, str(rs.document_id)):
                raise ApiException(404, "RESOURCE_NOT_FOUND", "构件不存在")

        run_data = QaService.get_run(actor, run_id)
        if run_data.get("visibility") == "HIDDEN":
            raise ApiException(404, "RESOURCE_NOT_FOUND", "构件不存在")

        evidence_list = run_data.get("evidence", [])
        for ev in evidence_list:
            if ev.get("evidenceId") == evidence_id:
                return ev
        raise ApiException(404, "RESOURCE_NOT_FOUND", "证据不存在")

    @staticmethod
    def get_trace(actor: Actor, run_id: str) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要管理员权限")

        with get_db_connection() as conn:
            r = conn.execute(
                select(qa_run_table).where(
                    and_(
                        qa_run_table.c.tenant_id == actor.tenant_id,
                        qa_run_table.c.id == run_id,
                    )
                )
            ).first()
            if not r:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "问答运行不存在")

            sources = QaService._all_sources(conn, actor.tenant_id, r.segment_id, r.turn_no)
            for did in sources:
                if not can_read_document(conn, actor, did):
                    raise ApiException(404, "RESOURCE_NOT_FOUND", "运行来源权限已失效")

            events = conn.execute(
                select(run_event_table).where(
                    and_(
                        run_event_table.c.tenant_id == actor.tenant_id,
                        run_event_table.c.run_id == run_id,
                    )
                ).order_by(run_event_table.c.sequence_no.asc())
            ).fetchall()

            return {
                "runId": run_id,
                "configId": r.config_id,
                "status": r.status,
                "events": [
                    {
                        "sequenceNo": e.sequence_no,
                        "stage": e.stage,
                        "payload": e.payload,
                        "durationMs": e.duration_ms,
                        "errorCode": e.error_code,
                        "createdAt": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in events
                ],
            }

    @staticmethod
    def submit_feedback(actor: Actor, run_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        run_data = QaService.get_run(actor, run_id)
        if run_data.get("visibility") != "VISIBLE" or "answer" not in run_data:
            raise ApiException(404, "RESOURCE_NOT_FOUND", "运行不存在或不可见")

        rating = body.get("rating") or ""
        issue_type = body.get("issueType") or ""
        comment = body.get("comment") or ""

        if rating not in ("USEFUL", "USELESS", ""):
            raise ApiException(400, "INVALID_INPUT", "反馈参数无效")
        if issue_type not in ("ANSWER_ERROR", "CITATION_ERROR", "OUTDATED", "MISSING", "PERMISSION", "OTHER", ""):
            raise ApiException(400, "INVALID_INPUT", "反馈参数无效")
        if len(comment) > 4000:
            raise ApiException(400, "INVALID_INPUT", "反馈内容过长")

        rating_val = rating if rating else None
        issue_val = issue_type if issue_type else None
        now = datetime.now(timezone.utc)

        with get_db_connection() as conn:
            existing = conn.execute(
                select(feedback_table.c.id).where(
                    and_(
                        feedback_table.c.tenant_id == actor.tenant_id,
                        feedback_table.c.run_id == run_id,
                        feedback_table.c.submitted_by == actor.user_id,
                    )
                )
            ).first()

            if existing:
                conn.execute(
                    update(feedback_table)
                    .where(
                        and_(
                            feedback_table.c.tenant_id == actor.tenant_id,
                            feedback_table.c.id == existing.id,
                        )
                    )
                    .values(
                        rating=rating_val,
                        issue_type=issue_val,
                        comment_text=comment,
                        status="OPEN",
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    feedback_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        run_id=run_id,
                        submitted_by=actor.user_id,
                        rating=rating_val,
                        issue_type=issue_val,
                        comment_text=comment,
                        status="OPEN",
                        resolution_text=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            conn.commit()

        return {"status": "OPEN"}

    @staticmethod
    def admin_feedback_list(actor: Actor) -> List[Dict[str, Any]]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        with get_db_connection() as conn:
            rows = conn.execute(
                select(feedback_table)
                .where(feedback_table.c.tenant_id == actor.tenant_id)
                .order_by(feedback_table.c.created_at.desc())
                .limit(100)
            ).fetchall()

            result = []
            for row in rows:
                try:
                    QaService.get_trace(actor, str(row.run_id))
                    result.append({
                        "id": str(row.id),
                        "runId": str(row.run_id),
                        "rating": row.rating,
                        "issueType": row.issue_type,
                        "comment": row.comment_text,
                        "status": str(row.status),
                        "resolution": row.resolution_text,
                    })
                except ApiException as e:
                    if e.status_code == 404:
                        result.append({
                            "id": str(row.id),
                            "status": str(row.status),
                            "visibility": "HIDDEN",
                        })
                    else:
                        raise
            return result

    @staticmethod
    def admin_feedback_detail(actor: Actor, feedback_id: str) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        with get_db_connection() as conn:
            row = conn.execute(
                select(feedback_table).where(
                    and_(
                        feedback_table.c.tenant_id == actor.tenant_id,
                        feedback_table.c.id == feedback_id,
                    )
                )
            ).first()
            if not row:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "反馈不存在")

            QaService.get_trace(actor, str(row.run_id))
            return {
                "id": str(row.id),
                "runId": str(row.run_id),
                "rating": row.rating,
                "issueType": row.issue_type,
                "comment": row.comment_text,
                "status": str(row.status),
                "resolution": row.resolution_text,
            }

    @staticmethod
    def admin_feedback_update(actor: Actor, feedback_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        status_val = body.get("status") or ""
        resolution_val = body.get("resolution") or ""

        if status_val not in ("OPEN", "IN_PROGRESS", "RESOLVED"):
            raise ApiException(400, "INVALID_INPUT", "处理参数无效")
        if len(resolution_val) > 4000:
            raise ApiException(400, "INVALID_INPUT", "处理参数无效")

        QaService.admin_feedback_detail(actor, feedback_id)

        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conn.execute(
                update(feedback_table)
                .where(
                    and_(
                        feedback_table.c.tenant_id == actor.tenant_id,
                        feedback_table.c.id == feedback_id,
                    )
                )
                .values(
                    status=status_val,
                    resolution_text=resolution_val,
                    updated_at=now,
                )
            )
            conn.commit()

        return {"status": status_val}
