import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import select, and_, or_, exists, update
from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor
from app.models import (
    conversation_table,
    context_segment_table,
    segment_kb_table,
    qa_run_table,
    run_source_table,
    run_event_table,
    model_call_table,
    knowledge_base_table,
    document_table,
    kb_grant_table,
    document_grant_table,
)
from app.qa.content_guard import validate_input, validate_output
from app.qa.question_router import classify_question, Route
from app.qa.claim_validator import validate_claims, INSTRUCTION
from app.qa.model_gateway import ModelGateway
from app.retrieval.milvus_service import MilvusHybridService

_executor = ThreadPoolExecutor(max_workers=4)


class QaService:
    @staticmethod
    def recover_unfinished_runs():
        """Recovers any pending runs left over from process restart."""
        try:
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                conn.execute(
                    update(qa_run_table)
                    .where(qa_run_table.c.status.in_(["QUEUED", "RUNNING"]))
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
            pass

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

            return [
                {
                    "id": str(r.id),
                    "title": r.title,
                    "createdAt": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

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

        return {
            "id": conv_id,
            "title": conv.title,
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

            conn.execute(
                qa_run_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=run_id,
                    owner_id=actor.user_id,
                    conversation_id=conv_id,
                    segment_id=seg.id,
                    turn_no=max_turn + 1,
                    config_id=None,
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
            conn.commit()

        # Submit to async executor
        _executor.submit(
            QaService._execute_run,
            actor,
            run_id,
            conv_id,
            str(seg.id),
            question,
            mode,
            strict_mode,
        )

        return {
            "runId": run_id,
            "status": "QUEUED",
            "pollAfterMs": 1000,
        }

    @staticmethod
    def _record_event(tenant_id: str, run_id: str, stage: str, payload: Optional[Dict[str, Any]] = None):
        try:
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
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
    ):
        now = datetime.now(timezone.utc)
        try:
            with get_db_connection() as conn:
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

            # 2. Scope & Retrieval
            QaService._record_event(actor.tenant_id, run_id, "RETRIEVING")
            with get_db_connection() as conn:
                # Get segment scope
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

                # Resolve allowed active build IDs based on MySQL ACL
                stmt = select(document_table.c.active_build_id, document_table.c.id.label("document_id")).where(
                    and_(
                        document_table.c.tenant_id == actor.tenant_id,
                        document_table.c.status == "LIVE",
                        document_table.c.active_build_id.isnot(None),
                    )
                )
                if allowed_kb_ids:
                    stmt = stmt.where(document_table.c.kb_id.in_(allowed_kb_ids))

                if not actor.admin:
                    stmt = stmt.where(
                        or_(
                            exists().where(
                                and_(
                                    kb_grant_table.c.tenant_id == document_table.c.tenant_id,
                                    kb_grant_table.c.kb_id == document_table.c.kb_id,
                                    or_(
                                        kb_grant_table.c.principal_type == "ALL",
                                        kb_grant_table.c.user_id == actor.user_id,
                                    ),
                                )
                            ),
                            exists().where(
                                and_(
                                    document_grant_table.c.tenant_id == document_table.c.tenant_id,
                                    document_grant_table.c.document_id == document_table.c.id,
                                    document_grant_table.c.user_id == actor.user_id,
                                )
                            ),
                        )
                    )
                permitted_rows = conn.execute(stmt).fetchall()
                allowed_build_ids = [str(r.active_build_id) for r in permitted_rows]
                build_to_doc = {str(r.active_build_id): str(r.document_id) for r in permitted_rows}

            # 3. If no permitted builds, return NO_EVIDENCE without calling LLM
            if not allowed_build_ids:
                no_ev_res = {
                    "status": "NO_EVIDENCE",
                    "claims": [],
                    "conflicts": [],
                    "answer": "当前可访问资料不足以回答。",
                    "verification": "NOT_APPLICABLE",
                    "evidence": [],
                    "warnings": [],
                }
                QaService._finalize_run(actor.tenant_id, run_id, "COMPLETED", "FINISHED", "NO_EVIDENCE", no_ev_res)
                return

            # Hybrid Search
            hits = MilvusHybridService.search(
                tenant_id=actor.tenant_id,
                query=question,
                allowed_build_ids=allowed_build_ids,
                top_k=5,
            )
            QaService._record_event(actor.tenant_id, run_id, "RETRIEVED", {"hitCount": len(hits)})

            if not hits:
                no_ev_res = {
                    "status": "NO_EVIDENCE",
                    "claims": [],
                    "conflicts": [],
                    "answer": "当前可访问资料不足以回答。",
                    "verification": "NOT_APPLICABLE",
                    "evidence": [],
                    "warnings": [],
                }
                QaService._finalize_run(actor.tenant_id, run_id, "COMPLETED", "FINISHED", "NO_EVIDENCE", no_ev_res)
                return

            if QaService._is_cancelled(actor.tenant_id, run_id):
                return

            # 4. Context assembly
            evidence = []
            evidence_map = {}
            with get_db_connection() as conn:
                for idx, h in enumerate(hits, start=1):
                    eid = f"E{idx}"
                    doc_id = h.get("documentId")
                    ev_item = {
                        "evidenceId": eid,
                        "chunkId": h["chunkId"],
                        "documentId": doc_id,
                        "text": h["text"],
                        "score": round(h["score"], 6),
                    }
                    evidence.append(ev_item)
                    evidence_map[eid] = h["text"]

                    # Record run_source
                    conn.execute(
                        run_source_table.insert().values(
                            tenant_id=actor.tenant_id,
                            id=str(uuid.uuid4()),
                            run_id=run_id,
                            document_id=doc_id,
                            build_id=allowed_build_ids[0] if allowed_build_ids else "",
                            unit_id=None,
                            evidence_id=eid,
                            purpose="CITATION",
                            span_json={},
                            payload_hash=hashlib.sha256(h["text"].encode("utf-8")).digest(),
                            send_status="SENT",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                conn.commit()

            # 5. Generation
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
            validated["warnings"] = []

            # 7. Strict Mode Verification
            if strict_mode and validated.get("claims"):
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
            QaService._fail_run(actor.tenant_id, run_id, e.code)
        except Exception as e:
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
    def _finalize_run(tenant_id: str, run_id: str, status: str, stage: str, terminal_reason: str, result_json: Dict[str, Any]):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conn.execute(
                update(qa_run_table)
                .where(
                    and_(
                        qa_run_table.c.tenant_id == tenant_id,
                        qa_run_table.c.id == run_id,
                        qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                    )
                )
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

    @staticmethod
    def _fail_run(tenant_id: str, run_id: str, error_code: str):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conn.execute(
                update(qa_run_table)
                .where(
                    and_(
                        qa_run_table.c.tenant_id == tenant_id,
                        qa_run_table.c.id == run_id,
                        qa_run_table.c.status.in_(["QUEUED", "RUNNING"]),
                    )
                )
                .values(
                    status="FAILED",
                    stage="FINISHED",
                    terminal_reason=error_code,
                    finished_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

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

            base = {
                "runId": str(r.id),
                "status": str(r.status),
                "stage": str(r.stage),
                "terminalReason": r.terminal_reason,
                "question": r.question,
            }

            # Check ACL visibility of underlying sources
            sources = conn.execute(
                select(run_source_table.c.document_id).where(
                    and_(
                        run_source_table.c.tenant_id == actor.tenant_id,
                        run_source_table.c.run_id == run_id,
                    )
                )
            ).fetchall()
            doc_ids = list(set(str(s.document_id) for s in sources))

            # Verify if user still can read all documents
            for did in doc_ids:
                doc_check = conn.execute(
                    select(document_table.c.id).where(
                        and_(
                            document_table.c.tenant_id == actor.tenant_id,
                            document_table.c.id == did,
                            document_table.c.status == "LIVE",
                        )
                    )
                ).first()
                if not doc_check:
                    base["visibility"] = "HIDDEN"
                    base["notice"] = "来源权限或状态已变化，回答不可显示。"
                    return base

                if not actor.admin:
                    has_perm = conn.execute(
                        select(
                            or_(
                                exists().where(
                                    and_(
                                        kb_grant_table.c.tenant_id == actor.tenant_id,
                                        kb_grant_table.c.kb_id == document_table.c.kb_id,
                                        or_(
                                            kb_grant_table.c.principal_type == "ALL",
                                            kb_grant_table.c.user_id == actor.user_id,
                                        ),
                                    )
                                ),
                                exists().where(
                                    and_(
                                        document_grant_table.c.tenant_id == actor.tenant_id,
                                        document_grant_table.c.document_id == did,
                                        document_grant_table.c.user_id == actor.user_id,
                                    )
                                ),
                            )
                        ).where(
                            and_(
                                document_table.c.tenant_id == actor.tenant_id,
                                document_table.c.id == did,
                            )
                        )
                    ).scalar()
                    if not has_perm:
                        base["visibility"] = "HIDDEN"
                        base["notice"] = "来源权限或状态已变化，回答不可显示。"
                        return base

            if r.result_json:
                base.update(r.result_json)
            base["visibility"] = "VISIBLE"

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
        run_data = QaService.get_run(actor, run_id)
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
                "events": [
                    {
                        "sequenceNo": e.sequence_no,
                        "stage": e.stage,
                        "payload": e.payload,
                        "durationMs": e.duration_ms,
                        "createdAt": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in events
                ],
            }
