import hashlib
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from sqlalchemy import select, and_, or_, func, text, update, delete
from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor, can_read_document
from app.config import settings, is_test_env
from app.models import (
    tenant_table,
    app_user_table,
    document_table,
    document_version_table,
    index_build_table,
    source_unit_table,
    chunk_table,
    job_table,
    qa_run_table,
    config_revision_table,
    eval_dataset_version_table,
    eval_case_table,
    eval_gold_table,
    eval_snapshot_table,
    eval_snapshot_document_table,
    eval_run_table,
    eval_result_table,
    eval_review_table,
    run_event_table,
    model_call_table,
)
from app.evaluation.metrics import EvaluationMetrics
from app.evaluation.catalog import EvaluationCatalog, obj, maps, hash_payload, case_view
from app.retrieval.config import validate_retrieval_payload
from app.evaluation.judge import (\n    judge_answer, RUBRIC_VERSION, LEGACY_RUBRIC_VERSION, INPUT_CONTRACT_VERSION,\n    EVALUATION_SCOPE, RELEVANCE_POLICY,\n)
from app.model_budget import BudgetService


def check_no_model(config: Dict[str, Any]):
    """Verify RETRIEVAL_ONLY against the same effective config used at runtime."""
    try:
        effective = validate_retrieval_payload(config)
    except ApiException as exc:
        if exc.code == "UNSUPPORTED_STRATEGY":
            raise ApiException(409, "MODEL_REQUIRED", exc.message) from exc
        raise ApiException(409, "INVALID_CONFIG", exc.message) from exc

    query = config.get("query", {}) if isinstance(config.get("query"), dict) else {}
    post = config.get("post", {}) if isinstance(config.get("post"), dict) else {}

    if (
        effective.mode != "BM25"
        or effective.rerank_enabled
        or query.get("rewrite")
        or query.get("hyde")
        or query.get("multiQuery")
        or post.get("rerank")
        or post.get("mmr")
    ):
        raise ApiException(
            409,
            "MODEL_REQUIRED",
            "RETRIEVAL_ONLY 只运行无模型 BM25；Dense/Hybrid/Rerank 消融请使用 RETRIEVAL_PIPELINE",
        )


def number(v: Any) -> float:
    """Parse and validate numeric parameter."""
    try:
        n = float(v)
        if not math.isfinite(n):
            raise ValueError()
        return n
    except Exception:
        raise ApiException(400, "INVALID_INPUT", "数字参数无效")


class EvaluationRunner:
    """Evaluation job execution, metric computation, ablation comparison and baseline diff service."""

    @staticmethod
    def estimate(actor: Actor, body: Dict[str, Any]) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)

        dataset_ver_id = str(body.get("datasetVersionId", "")).strip()
        snapshot_id = str(body.get("snapshotId", "")).strip()
        config_id = str(body.get("configId", "")).strip()

        if not dataset_ver_id or not snapshot_id or not config_id:
            raise ApiException(400, "INVALID_INPUT", "题集、快照与配置 ID 必填")

        with get_db_connection() as conn:
            ds = conn.execute(
                select(eval_dataset_version_table).where(
                    and_(
                        eval_dataset_version_table.c.tenant_id == actor.tenant_id,
                        eval_dataset_version_table.c.id == dataset_ver_id,
                    )
                )
            ).first()
            if not ds:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "题集版本不存在")
            if ds.status != "FROZEN":
                raise ApiException(409, "DATASET_NOT_FROZEN", "先冻结题集")

            EvaluationCatalog.check_snapshot(actor, snapshot_id, check_current=True)

            cfg = conn.execute(
                select(config_revision_table).where(
                    and_(
                        config_revision_table.c.tenant_id == actor.tenant_id,
                        config_revision_table.c.id == config_id,
                    )
                )
            ).first()
            if not cfg:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "配置不存在")
            if cfg.kind != "RETRIEVAL" or cfg.status != "PUBLISHED":
                raise ApiException(409, "INVALID_CONFIG", "选择已发布检索配置")

            split = str(body.get("split", "TEST")).strip().upper()
            if split not in {"DEV", "TEST"}:
                raise ApiException(400, "INVALID_INPUT", "split必须DEV/TEST")

            repeats = int(body.get("repeats", 1))
            if repeats < 1 or repeats > 3:
                raise ApiException(400, "INVALID_INPUT", "重复次数1..3")

            mode = str(body.get("mode", "RAG")).strip().upper()
            judge_mode = str(body.get("judgeMode", "HUMAN")).upper()
            if judge_mode not in {"HUMAN", "LLM"}:
                raise ApiException(400, "INVALID_INPUT", "judgeMode 必须 HUMAN/LLM")
            if mode not in {"RAG", "AGENT", "RETRIEVAL_ONLY", "RETRIEVAL_PIPELINE"}:
                raise ApiException(400, "INVALID_INPUT", "评估模式无效")

            cfg_payload = obj(cfg.payload)
            if mode == "RETRIEVAL_ONLY":
                check_no_model(cfg_payload)

            cases_rows = conn.execute(
                select(eval_case_table).where(
                    and_(
                        eval_case_table.c.tenant_id == actor.tenant_id,
                        eval_case_table.c.dataset_version_id == dataset_ver_id,
                        eval_case_table.c.split == split,
                    )
                )
            ).fetchall()

            if not cases_rows:
                raise ApiException(409, "EMPTY_SPLIT", "所选分区无题目")

            turns = 0
            for c in cases_rows:
                q = obj(c.question_json)
                q_turns = maps(q.get("turns"))
                if mode in {"RETRIEVAL_ONLY", "RETRIEVAL_PIPELINE"} and len(q_turns) > 1:
                    raise ApiException(409, "MULTITURN_REQUIRES_QA", "多轮题请使用 RAG 或 AGENT 评测")
                turns += max(1, len(q_turns))

            if mode == "RETRIEVAL_ONLY":
                ceiling = 0.0
                cost_status = "NO_MODEL_CALLS"
            else:
                aliases = ["embedding-main"]
                effective = validate_retrieval_payload(cfg_payload)
                if effective.mode == "BM25":
                    aliases = []
                if effective.rerank_enabled:
                    aliases.append(effective.rerank_model)
                if mode in {"RAG", "AGENT"}:
                    aliases.append("chat-main")
                if judge_mode == "LLM" or body.get("strict", False):
                    aliases.append("judge-main")
                for alias in aliases:
                    BudgetService.pricing(actor.tenant_id, alias)
                total_turns = turns * repeats
                total_questions = len(cases_rows) * repeats
                # v2 Judge sees the complete final answer plus actual conversation history.
                # Keep this conservative: answer/retrieval work plus one larger semantic Judge payload.
                input_tokens = total_turns * 30000 + total_questions * 42000
                output_tokens = total_turns * 6000 + total_questions * 2800
                ceiling = round((input_tokens * 0.00002 + output_tokens * 0.00006), 4)
                cost_status = "ESTIMATE_NOT_BILL"

            estimate_dict = {
                "datasetVersionId": dataset_ver_id,
                "snapshotId": snapshot_id,
                "configId": config_id,
                "split": split,
                "repeats": repeats,
                "mode": mode,
                "judgeMode": judge_mode,
                "rubricVersion": RUBRIC_VERSION,
                "inputContractVersion": INPUT_CONTRACT_VERSION,
                "evaluationScope": EVALUATION_SCOPE,
                "relevancePolicy": RELEVANCE_POLICY,
                "strict": bool(body.get("strict", False)),
                "plannedQuestions": len(cases_rows) * repeats,
                "plannedTurns": turns * repeats,
                "estimatedCostCny": ceiling,
                "costStatus": cost_status,
                "retrievalConfig": cfg_payload,
            }
            estimate_dict["estimateId"] = hash_payload(estimate_dict).hex()
            return estimate_dict

    @staticmethod
    def start(actor: Actor, body: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)
        if not idempotency_key or len(idempotency_key) > 128:
            raise ApiException(400, "IDEMPOTENCY_KEY_REQUIRED", "需要有效Idempotency-Key")

        est = EvaluationRunner.estimate(actor, body)
        if "estimateId" in body and str(body.get("estimateId")) != est["estimateId"]:
            raise ApiException(409, "ESTIMATE_CHANGED", "估算已变化，请重新估算")

        cap = number(body.get("maxCostCny", 10))
        if cap <= 0 or cap > 10:
            raise ApiException(400, "INVALID_BATCH_LIMIT", "批次费用上限需大于0且不超过10元")
        max_model_calls = int(body.get("maxModelCalls", 1000))
        if max_model_calls < 1 or max_model_calls > 1000:
            raise ApiException(400, "INVALID_BATCH_LIMIT", "批次模型调用上限需在1到1000之间")

        if est.get("estimatedCostCny") is None:
            raise ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", "价格未知不能启动付费评估，可先运行RETRIEVAL_ONLY")

        if number(est["estimatedCostCny"]) > cap:
            raise ApiException(429, "BATCH_BUDGET_EXCEEDED", "按最坏输入输出估算超过批次限额")

        req_hash = hash_payload(body)
        now = datetime.now(timezone.utc)
        run_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())

        manifest = dict(est)
        manifest["maxCostCny"] = cap
        manifest["maxModelCalls"] = max_model_calls
        manifest["requesterId"] = actor.user_id
        manifest["environment"] = settings.ENVIRONMENT

        with get_db_connection() as conn:
            existing_job = conn.execute(
                select(job_table).where(
                    and_(
                        job_table.c.tenant_id == actor.tenant_id,
                        job_table.c.kind == "EVALUATE",
                        job_table.c.idempotency_key == idempotency_key,
                    )
                )
            ).first()

            if existing_job:
                if bytes(existing_job.request_hash) != req_hash:
                    raise ApiException(409, "IDEMPOTENCY_CONFLICT", "幂等键内容冲突")
                return {"runId": str(existing_job.target_id), "jobId": str(existing_job.id)}

            conn.execute(
                job_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=job_id,
                    kind="EVALUATE",
                    target_id=run_id,
                    requested_by=actor.user_id,
                    idempotency_key=idempotency_key,
                    request_hash=req_hash,
                    status="QUEUED",
                    stage="EVALUATING",
                    available_at=now,
                    checkpoint={
                        "runId": run_id,
                        "maxCostCny": cap,
                        "maxModelCalls": max_model_calls,
                    },
                    created_at=now,
                    updated_at=now,
                )
            )

            conn.execute(
                eval_run_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=run_id,
                    dataset_version_id=body.get("datasetVersionId"),
                    snapshot_id=body.get("snapshotId"),
                    config_id=body.get("configId"),
                    job_id=job_id,
                    status="QUEUED",
                    manifest_json=manifest,
                    metrics_json=None,
                    created_at=now,
                    updated_at=now,
                )
            )

            cases_rows = conn.execute(
                select(eval_case_table.c.id).where(
                    and_(
                        eval_case_table.c.tenant_id == actor.tenant_id,
                        eval_case_table.c.dataset_version_id == body.get("datasetVersionId"),
                        eval_case_table.c.split == est["split"],
                    )
                )
            ).fetchall()

            repeats = int(est.get("repeats", 1))
            for c in cases_rows:
                for rep in range(repeats):
                    conn.execute(
                        eval_result_table.insert().values(
                            tenant_id=actor.tenant_id,
                            id=str(uuid.uuid4()),
                            eval_run_id=run_id,
                            dataset_version_id=body.get("datasetVersionId"),
                            case_id=str(c.id),
                            repeat_no=rep,
                            qa_run_id=None,
                            status="PENDING",
                            metrics_json=None,
                            judge_json=None,
                            error_code=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            conn.commit()

        if is_test_env():
            EvaluationRunner._execute_run(actor, run_id)

        # Non-test environments leave the durable EVALUATE job queued for
        # app.runtime.worker, so process restarts do not lose long evaluations.
        return {"runId": run_id, "jobId": job_id, "status": "QUEUED"}

    @staticmethod
    def _execute_run(actor: Actor, run_id: str):
        """Asynchronous execution of all cases in an eval run."""
        from app.retrieval.pipeline import RetrievalPipeline
        from app.qa.service import QaService

        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conn.execute(
                update(eval_run_table)
                .where(and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id))
                .values(status="RUNNING", updated_at=now)
            )
            conn.execute(
                update(job_table)
                .where(and_(job_table.c.tenant_id == actor.tenant_id, job_table.c.target_id == run_id))
                .values(status="RUNNING", updated_at=now)
            )
            conn.commit()

            run_row = conn.execute(
                select(eval_run_table).where(
                    and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id)
                )
            ).first()
            if not run_row:
                return
            manifest = obj(run_row.manifest_json)
            mode = manifest.get("mode", "RAG")
            snapshot_id = str(run_row.snapshot_id)
            eval_config_id = str(run_row.config_id)
            eval_job_id = str(run_row.job_id)

            snap_docs = conn.execute(
                select(eval_snapshot_document_table.c.document_id).where(
                    and_(
                        eval_snapshot_document_table.c.tenant_id == actor.tenant_id,
                        eval_snapshot_document_table.c.snapshot_id == snapshot_id,
                    )
                )
            ).fetchall()
            allowed_doc_ids = [str(d.document_id) for d in snap_docs]

            snapshot_manifest = obj(conn.execute(select(eval_snapshot_table.c.manifest_json).where(and_(
                eval_snapshot_table.c.tenant_id == actor.tenant_id,
                eval_snapshot_table.c.id == snapshot_id,
            ))).scalar())
            evaluation_scope = {"snapshotId": snapshot_id, "evalRunId": run_id,
                                "documents": snapshot_manifest.get("documents", [])}

            pending_results = conn.execute(
                select(eval_result_table).where(
                    and_(
                        eval_result_table.c.tenant_id == actor.tenant_id,
                        eval_result_table.c.eval_run_id == run_id,
                        eval_result_table.c.status == "PENDING",
                    )
                ).order_by(eval_result_table.c.created_at.asc())
            ).fetchall()

        for res in pending_results:
            # Check if cancelled
            with get_db_connection() as conn:
                curr_run = conn.execute(
                    select(eval_run_table.c.status).where(
                        and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id)
                    )
                ).first()
                if curr_run and curr_run.status == "CANCELLED":
                    break

            result_id = str(res.id)
            case_id = str(res.case_id)

            with get_db_connection() as conn:
                c_row = conn.execute(
                    select(eval_case_table).where(
                        and_(eval_case_table.c.tenant_id == actor.tenant_id, eval_case_table.c.id == case_id)
                    )
                ).first()

            if not c_row:
                continue

            c_dict = case_view(c_row)
            question_obj = c_dict.get("question", {})
            question_text = str(question_obj.get("text", "")).strip()
            turns = maps(question_obj.get("turns"))
            if not question_text and turns:
                question_text = str(turns[0].get("text", "")).strip()

            expected_obj = c_dict.get("expected", {})
            gold_groups = maps(expected_obj.get("evidenceGroups"))

            start_t = time.perf_counter()
            try:
                EvaluationCatalog.check_snapshot(actor, snapshot_id, check_current=True)
                persona_actor = EvaluationRunner._persona(actor, c_dict)
            except ApiException as exc:
                EvaluationRunner._update_result(actor, result_id, "FAILED",
                    {"taskSuccess": False, "costCny": None}, {"error": exc.code}, error_code=exc.code)
                continue

            if mode in {"RETRIEVAL_ONLY", "RETRIEVAL_PIPELINE"}:
                try:
                    if len(turns) > 1:
                        raise ApiException(409, "MULTITURN_REQUIRES_QA", "多轮题不能仅执行第一轮检索")
                    retrieval = RetrievalPipeline.retrieve(
                        actor=persona_actor,
                        query=question_text,
                        document_ids=allowed_doc_ids,
                        config_id=eval_config_id,
                        run_id=None,
                        job_id=eval_job_id,
                    )
                    hits = retrieval["hits"]
                    EvaluationCatalog.check_snapshot(actor, snapshot_id, check_current=True)
                    latency_ms = (time.perf_counter() - start_t) * 1000.0

                    metric = EvaluationRunner._compute_retrieval_metrics(actor, c_dict, hits, hits)
                    metric["durationMs"] = round(latency_ms, 2)
                    metric["costCny"] = 0.0 if mode == "RETRIEVAL_ONLY" else None
                    metric["costStatus"] = "NO_MODEL_CALLS" if mode == "RETRIEVAL_ONLY" else "UNKNOWN"
                    metric["mode"] = mode
                    metric["configId"] = eval_config_id
                    metric["retrievalConfig"] = retrieval["config"]
                    hit_val = metric.get("hit")
                    exp_status = expected_obj.get("status", "ANSWER")
                    if exp_status == "ABSTAIN":
                        metric["taskSuccess"] = None
                    else:
                        metric["taskSuccess"] = bool(hit_val is not None and hit_val > 0.0)

                    judge_res = {"correctness": "NOT_EVALUATED", "hits": hits}
                    EvaluationRunner._update_result(
                        actor, result_id, "COMPLETED", metric, judge_res, error_code=None
                    )
                except Exception as e:
                    latency_ms = (time.perf_counter() - start_t) * 1000.0
                    fail_metric = {"hit": 0.0, "recall": 0.0, "mrr": 0.0, "ndcg": 0.0, "taskSuccess": False, "durationMs": round(latency_ms, 2), "costCny": 0.0 if mode == "RETRIEVAL_ONLY" else None}
                    EvaluationRunner._update_result(
                        actor, result_id, "FAILED", fail_metric, {"error": str(e)}, error_code="RETRIEVAL_FAILED"
                    )
            else:
                # Full RAG mode via production QaService
                try:
                    qa_detail, qa_run_ids = EvaluationRunner._run_qa_turns(
                        actor, persona_actor, run_id, result_id, c_dict, manifest, evaluation_scope,
                    )
                    qa_run_id = qa_run_ids[-1]
                    EvaluationCatalog.check_snapshot(actor, snapshot_id, check_current=True)

                    latency_ms = (time.perf_counter() - start_t) * 1000.0
                    status_str = qa_detail.get("status", "FAILED")
                    is_terminal_success = status_str in {"SUCCEEDED", "COMPLETED", "PARTIAL", "NO_EVIDENCE", "CONFLICT"}

                    evidence_items = qa_detail.get("evidence", [])
                    metrics = EvaluationRunner._compute_retrieval_metrics(actor, c_dict, evidence_items, evidence_items)
                    metrics["durationMs"] = round(latency_ms, 2)
                    metrics["terminalStatus"] = status_str
                    metrics["executedTurns"] = len(qa_run_ids)

                    # Match expectation
                    exp_status = expected_obj.get("status", "ANSWER")
                    status_matches = (
                        (exp_status == "ABSTAIN" and status_str == "NO_EVIDENCE") or
                        (exp_status == "PARTIAL" and status_str == "PARTIAL") or
                        (exp_status == "CONFLICT" and status_str == "CONFLICT") or
                        (exp_status == "ANSWER" and status_str == "COMPLETED")
                    )
                    metrics["statusMatch"] = status_matches

                    # LLM judge or deterministic evaluation
                    # Retrieval and output status cannot establish semantic correctness.
                    correctness = "UNDETERMINED" if is_terminal_success and status_matches else "WRONG"
                    judge_obj = {
                        "correctness": correctness,
                        "supportedClaims": None,
                        "totalClaims": len(qa_detail.get("claims", [])),
                        "supportedCitationLinks": None,
                        "totalCitationLinks": sum(len(c.get("evidence", [])) for c in qa_detail.get("claims", [])),
                        "claimsWithSupportedCitation": None,
                        "method": "STATUS_ONLY_V2",
                        "output": qa_detail,
                        "qaRunIds": qa_run_ids,
                    }

                    if correctness == "UNDETERMINED" and manifest.get("judgeMode") == "LLM":
                        try:
                            from app.qa.context import assert_run_sources_readable
                            assert_run_sources_readable(persona_actor, qa_run_id)
                            judge_rubric = str(manifest.get("rubricVersion") or LEGACY_RUBRIC_VERSION)
                            judge_contract = str(manifest.get("inputContractVersion") or INPUT_CONTRACT_VERSION)
                            answer_present = "answer" in qa_detail
                            answer_text = qa_detail.get("answer") if answer_present else None
                            conversation_context = EvaluationRunner._judge_conversation_context(
                                persona_actor, qa_run_ids
                            )
                            final_question = str(qa_detail.get("question") or question_text)
                            judge_obj.update(judge_answer(
                                actor.tenant_id,
                                qa_run_id,
                                final_question,
                                expected_obj,
                                qa_detail,
                                answer_text=answer_text,
                                answer_present=answer_present,
                                conversation_context=conversation_context,
                                rubric_version=judge_rubric,
                                input_contract_version=judge_contract,
                            ))
                            assert_run_sources_readable(persona_actor, qa_run_id)
                            correctness = judge_obj["correctness"]
                        except Exception as exc:
                            judge_obj["judgeError"] = type(exc).__name__
                            judge_obj["correctness"] = "UNDETERMINED"
                    metrics["correctness"] = correctness
                    metrics["taskSuccess"] = correctness == "CORRECT" if correctness != "UNDETERMINED" else None
                    with get_db_connection() as conn:
                        calls = conn.execute(select(model_call_table.c.settled_amount, model_call_table.c.pricing_snapshot).where(and_(
                            model_call_table.c.tenant_id == actor.tenant_id,
                            model_call_table.c.run_id.in_(qa_run_ids),
                        ))).fetchall()
                    amounts = [number(c.settled_amount) for c in calls
                               if c.settled_amount is not None and obj(c.pricing_snapshot).get("rate") != "fixed"]
                    metrics["knownCostCny"] = sum(amounts)
                    metrics["costCny"] = sum(amounts) if calls and len(amounts) == len(calls) else None
                    metrics["costStatus"] = "SETTLED" if metrics["costCny"] is not None else "UNKNOWN"

                    if mode == "AGENT":
                        with get_db_connection() as conn:
                            event_rows = conn.execute(
                                select(run_event_table.c.stage, run_event_table.c.payload).where(and_(
                                    run_event_table.c.tenant_id == actor.tenant_id,
                                    run_event_table.c.run_id.in_(qa_run_ids),
                                ))
                            ).fetchall()
                            call_rows = conn.execute(
                                select(
                                    model_call_table.c.input_tokens,
                                    model_call_table.c.output_tokens,
                                    model_call_table.c.status,
                                ).where(and_(
                                    model_call_table.c.tenant_id == actor.tenant_id,
                                    model_call_table.c.run_id.in_(qa_run_ids),
                                ))
                            ).fetchall()

                        tool_events = [e for e in event_rows if str(e.stage) == "AGENT_TOOL"]
                        model_events = [e for e in event_rows if str(e.stage) == "AGENT_MODEL"]
                        trim_events = [e for e in event_rows if str(e.stage) == "AGENT_CONTEXT_TRIMMED"]
                        approval_events = [e for e in event_rows if str(e.stage) == "AGENT_APPROVAL_REQUIRED"]
                        tool_names = [
                            str((e.payload or {}).get("name") or "")
                            for e in tool_events
                            if (e.payload or {}).get("name")
                        ]
                        expected_tools = expected_obj.get("tools") if isinstance(expected_obj.get("tools"), list) else []
                        expected_tools = [str(x) for x in expected_tools]
                        tool_accuracy = None
                        if expected_tools:
                            expected_set = set(expected_tools)
                            actual_set = set(tool_names)
                            tool_accuracy = len(expected_set & actual_set) / len(expected_set)

                        metrics["agentTrajectory"] = {
                            "turns": len(model_events),
                            "toolCalls": len(tool_events),
                            "uniqueTools": sorted(set(tool_names)),
                            "toolSelectionAccuracy": tool_accuracy,
                            "contextTrimEvents": len(trim_events),
                            "approvalInterrupts": len(approval_events),
                            "modelCalls": len(call_rows),
                            "inputTokens": sum(int(c.input_tokens or 0) for c in call_rows),
                            "outputTokens": sum(int(c.output_tokens or 0) for c in call_rows),
                            "failedModelCalls": sum(1 for c in call_rows if str(c.status) == "FAILED"),
                            "timeout": status_str == "FAILED" and qa_detail.get("terminalReason") == "AGENT_TIMEOUT",
                        }

                    final_status = "COMPLETED" if is_terminal_success else "FAILED"
                    EvaluationRunner._update_result(
                        actor,
                        result_id,
                        final_status,
                        metrics,
                        judge_obj,
                        error_code=None if is_terminal_success else "QA_EXECUTION_FAILED",
                        qa_run_id=qa_run_id,
                    )
                except Exception as e:
                    latency_ms = (time.perf_counter() - start_t) * 1000.0
                    fail_metric = {"hit": 0.0, "recall": 0.0, "mrr": 0.0, "ndcg": 0.0, "taskSuccess": False, "durationMs": round(latency_ms, 2), "costCny": None}
                    EvaluationRunner._update_result(
                        actor, result_id, "FAILED", fail_metric, {"error": str(e)}, error_code=type(e).__name__
                    )

        # Finish run
        with get_db_connection() as conn:
            curr_run = conn.execute(
                select(eval_run_table.c.status).where(
                    and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id)
                )
            ).first()
            if curr_run and curr_run.status != "CANCELLED":
                agg = EvaluationRunner.aggregate(actor, run_id)
                now = datetime.now(timezone.utc)
                conn.execute(
                    update(eval_run_table)
                    .where(and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id))
                    .values(status="COMPLETED", metrics_json=agg, updated_at=now)
                )
                conn.execute(
                    update(job_table)
                    .where(and_(job_table.c.tenant_id == actor.tenant_id, job_table.c.target_id == run_id))
                    .values(status="COMPLETED", updated_at=now)
                )
                conn.commit()

    @staticmethod
    def _persona(actor, case):
        login = str(case.get("persona", {}).get("loginName", "")).strip()
        with get_db_connection() as conn:
            user = conn.execute(select(app_user_table.c.id).where(and_(
                app_user_table.c.tenant_id == actor.tenant_id,
                app_user_table.c.login_name == login,
                app_user_table.c.status == "ACTIVE",
            ))).first()
        if not user:
            raise ApiException(409, "PERSONA_UNAVAILABLE", "评测身份不存在或已停用")
        return Actor(actor.tenant_id, str(user.id), False)

    @staticmethod
    def _run_qa_turns(actor, persona, eval_run_id, result_id, case, manifest, scope):
        from app.qa.service import QaService
        question = case.get("question", {})
        turns = maps(question.get("turns")) or [{"text": question.get("text", "")}]
        with get_db_connection() as conn:
            prior_conv = conn.execute(select(qa_run_table.c.conversation_id).where(and_(
                qa_run_table.c.tenant_id == persona.tenant_id,
                qa_run_table.c.owner_id == persona.user_id,
                qa_run_table.c.idempotency_key == f"{persona.user_id}:eval:{result_id}:0",
            ))).scalar()
        conv = ({"conversationId": str(prior_conv)} if prior_conv else
                QaService.create_conversation(persona, {"title": f"Eval: {case.get('caseKey')}"}))
        run_ids = []
        for index, turn in enumerate(turns):
            EvaluationCatalog.check_snapshot(actor, scope["snapshotId"], check_current=True)
            with get_db_connection() as conn:
                eval_row = conn.execute(select(eval_run_table.c.status, eval_run_table.c.job_id).where(and_(
                    eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == eval_run_id,
                ))).first()
            if eval_row and eval_row.status == "CANCELLED":
                raise ApiException(409, "CANCELLED", "评测已取消")
            segment_id = QaService.get_conversation_detail(persona, conv["conversationId"])["currentSegmentId"]
            submitted = QaService.submit_run(persona, conv["conversationId"], {
                "question": str(turn.get("text", "")).strip(), "mode": manifest["mode"],
                "strictMode": manifest.get("strict", False), "segmentId": segment_id,
            }, idempotency_key=f"eval:{result_id}:{index}", evaluation_scope=scope,
                trusted_config_id=manifest["configId"],
                trusted_evaluation_job_id=str(eval_row.job_id) if eval_row else None)
            run_id = submitted["runId"]
            run_ids.append(run_id)
            deadline = time.monotonic() + float(os.getenv("AGENT_TIMEOUT_SECONDS", "120")) + 10
            while True:
                detail = QaService.get_run(persona, run_id)
                if detail.get("status") not in {"QUEUED", "RUNNING", "WAITING_APPROVAL"}:
                    break
                with get_db_connection() as conn:
                    status = conn.execute(select(eval_run_table.c.status).where(and_(
                        eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == eval_run_id,
                    ))).scalar()
                if status == "CANCELLED" or time.monotonic() >= deadline:
                    QaService.cancel_run(persona, run_id)
                    raise ApiException(409, "CANCELLED" if status == "CANCELLED" else "QA_TIMEOUT", "评测问答已取消或超时")
                time.sleep(0.5)
            if detail.get("visibility") == "HIDDEN" or detail.get("status") in {"FAILED", "CANCELLED"}:
                raise ApiException(409, "QA_EXECUTION_FAILED", "评测问答未成功完成")
        return detail, run_ids

    @staticmethod
    def _judge_conversation_context(persona: Actor, qa_run_ids: List[str]) -> List[Dict[str, str]]:
        """Build the exact prior user/assistant history plus the final user turn.

        The final assistant answer is scored separately as answerText and is therefore not duplicated here.
        """
        if not qa_run_ids:
            return []
        with get_db_connection() as conn:
            rows = conn.execute(
                select(
                    qa_run_table.c.id,
                    qa_run_table.c.question,
                    qa_run_table.c.result_json,
                ).where(and_(
                    qa_run_table.c.tenant_id == persona.tenant_id,
                    qa_run_table.c.owner_id == persona.user_id,
                    qa_run_table.c.id.in_(qa_run_ids),
                ))
            ).fetchall()
        by_id = {str(r.id): r for r in rows}
        messages: List[Dict[str, str]] = []
        for index, rid in enumerate(qa_run_ids):
            row = by_id.get(str(rid))
            if not row:
                continue
            question = str(row.question or "").strip()
            if question:
                messages.append({"role": "user", "content": question})
            if index < len(qa_run_ids) - 1:
                result = obj(row.result_json)
                if "answer" in result:
                    answer = "" if result.get("answer") is None else str(result.get("answer"))
                    messages.append({"role": "assistant", "content": answer})
        return messages

    @staticmethod
    def _update_result(
        actor: Actor,
        result_id: str,
        status_val: str,
        metrics: Dict[str, Any],
        judge: Dict[str, Any],
        error_code: Optional[str] = None,
        qa_run_id: Optional[str] = None,
    ):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conn.execute(
                update(eval_result_table)
                .where(
                    and_(
                        eval_result_table.c.tenant_id == actor.tenant_id,
                        eval_result_table.c.id == result_id,
                    )
                )
                .values(
                    status=status_val,
                    metrics_json=metrics,
                    judge_json=judge,
                    error_code=error_code,
                    qa_run_id=qa_run_id,
                    updated_at=now,
                )
            )
            conn.commit()

    @staticmethod
    def _compute_retrieval_metrics(
        actor: Actor,
        c_dict: Dict[str, Any],
        ranked_hits: List[Dict[str, Any]],
        evidence_list: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        expected = obj(c_dict.get("expected"))
        gold_groups_raw = maps(expected.get("evidenceGroups"))

        groups: List[Set[str]] = []
        ideal_grades: List[int] = []
        anchors: Dict[str, Dict[str, Any]] = {}
        anchor_groups: Dict[str, int] = {}
        anchor_grades: Dict[str, int] = {}

        for g_idx, grp in enumerate(gold_groups_raw):
            group_ids: Set[str] = set()
            max_grade = 0
            for a_idx, alt in enumerate(maps(grp.get("alternatives"))):
                key = f"g{g_idx}:{len(anchors)}"
                grade = int(alt.get("relevanceGrade", 3))
                group_ids.add(key)
                anchors[key] = alt
                anchor_groups[key] = g_idx
                anchor_grades[key] = grade
                max_grade = max(max_grade, grade)
            groups.append(group_ids)
            ideal_grades.append(max_grade)

        # Ranked hits matching against anchors
        ranked: List[Set[str]] = []
        ranked_grades: List[int] = []
        seen_groups: Set[int] = set()

        chunk_ids = [
            str(hit.get("chunkId", hit.get("id", "")))
            for hit in ranked_hits
            if hit.get("chunkId", hit.get("id"))
        ]
        row_map: Dict[str, Any] = {}
        if chunk_ids:
            with get_db_connection() as conn:
                rows = conn.execute(
                    select(
                        chunk_table.c.id,
                        chunk_table.c.text_content,
                        index_build_table.c.document_id,
                        index_build_table.c.version_id,
                    )
                    .select_from(
                        chunk_table.join(
                            index_build_table,
                            and_(
                                index_build_table.c.tenant_id == chunk_table.c.tenant_id,
                                index_build_table.c.id == chunk_table.c.build_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            chunk_table.c.tenant_id == actor.tenant_id,
                            chunk_table.c.id.in_(chunk_ids),
                        )
                    )
                ).fetchall()
                row_map = {str(row.id): row for row in rows}

        for hit in ranked_hits:
            chunk_id = str(hit.get("chunkId", hit.get("id", "")))
            row = row_map.get(chunk_id)

            if not row:
                text_content = str(hit.get("text", ""))
                doc_id = str(hit.get("documentId", ""))
            else:
                text_content = row.text_content or ""
                doc_id = str(row.document_id)

            matched_anchors: Set[str] = set()
            for a_key, a_val in anchors.items():
                target_doc = str(a_val.get("documentId", ""))
                target_quote = str(a_val.get("quote", ""))
                version_id = str(row.version_id) if row else str(hit.get("versionId", ""))
                if doc_id == target_doc and version_id == str(a_val.get("versionId", "")) and target_quote and target_quote in text_content:
                    matched_anchors.add(a_key)

            ranked.append(matched_anchors)

            highest_grade = 0
            for match in matched_anchors:
                group_index = anchor_groups[match]
                if group_index not in seen_groups:
                    highest_grade = max(highest_grade, anchor_grades[match])
            for match in matched_anchors:
                seen_groups.add(anchor_groups[match])
            ranked_grades.append(highest_grade)

        res = EvaluationMetrics.retrieval(
            groups=groups,
            ranked=ranked,
            k=5,
            ranked_grades=ranked_grades,
            ideal_grades=ideal_grades,
        )

        res["retrievalStage"] = "FINAL_RANKED_CANONICAL"
        res["standardFactGroups"] = len(groups)
        return res

    @staticmethod
    def aggregate(actor: Actor, run_id: str) -> Dict[str, Any]:
        """Aggregate execution, semantic, relevance, calibration, retrieval, latency, and cost metrics."""
        with get_db_connection() as conn:
            rows = conn.execute(
                select(eval_result_table).where(
                    and_(
                        eval_result_table.c.tenant_id == actor.tenant_id,
                        eval_result_table.c.eval_run_id == run_id,
                    )
                )
            ).fetchall()
            run_row = conn.execute(
                select(eval_run_table.c.manifest_json).where(
                    and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id)
                )
            ).first()
            result_ids = [str(r.id) for r in rows]
            reviews = []
            if result_ids:
                reviews = conn.execute(
                    select(eval_review_table).where(and_(
                        eval_review_table.c.tenant_id == actor.tenant_id,
                        eval_review_table.c.result_id.in_(result_ids),
                    )).order_by(eval_review_table.c.created_at.asc(), eval_review_table.c.id.asc())
                ).fetchall()

        # Reviews are append-only and may cover only one dimension. Merge the newest value per dimension.
        latest_reviews: Dict[str, Dict[str, str]] = {}
        reviewed_result_ids: Set[str] = set()
        for review in reviews:
            rid = str(review.result_id)
            reviewed_result_ids.add(rid)
            target = latest_reviews.setdefault(rid, {})
            for key, value in obj(review.labels_json).items():
                if value is not None:
                    target[str(key)] = str(value)

        manifest = obj(run_row.manifest_json) if run_row else {}
        mode = str(manifest.get("mode", ""))
        semantic_mode = mode in {"RAG", "AGENT"}

        total = len(rows)
        completed = sum(1 for r in rows if r.status == "COMPLETED")
        failed = sum(1 for r in rows if r.status == "FAILED")
        pending = sum(1 for r in rows if r.status == "PENDING")
        success_count = 0
        judged = 0
        semantic_unknown = 0

        hit_sum = 0.0
        recall_sum = 0.0
        mrr_sum = 0.0
        ndcg_sum = 0.0
        retrieval_n = 0
        ndcg_n = 0
        durations: List[float] = []

        agent_cases = 0
        agent_turns = 0
        agent_tool_calls = 0
        agent_input_tokens = 0
        agent_output_tokens = 0
        agent_timeouts = 0

        known_cost = 0.0
        unknown_cost = 0
        reviewed_correct = 0

        relevance_labels = ["ON_TOPIC", "PARTLY_OFF_TOPIC", "OFF_TOPIC", "UNCERTAIN"]
        final_relevance_counts = {label: 0 for label in relevance_labels}
        auto_relevance_counts = {label: 0 for label in relevance_labels}
        relevance_assessed = 0
        auto_relevance_assessed = 0
        correctness_pairs: List[Tuple[str, str]] = []
        relevance_pairs: List[Tuple[str, str]] = []
        human_correctness_labels = 0
        human_relevance_labels = 0

        for r in rows:
            m = obj(r.metrics_json)
            judge = obj(r.judge_json)
            labels = latest_reviews.get(str(r.id), {})

            auto_correctness = str(judge.get("correctness") or m.get("correctness") or "")
            human_correctness = labels.get("correctness")
            if human_correctness:
                human_correctness_labels += 1
                if auto_correctness:
                    correctness_pairs.append((auto_correctness, human_correctness))
                m["taskSuccess"] = (
                    human_correctness == "CORRECT"
                    if human_correctness != "UNDETERMINED"
                    else None
                )
                reviewed_correct += int(human_correctness == "CORRECT")

            if semantic_mode:
                if isinstance(m.get("taskSuccess"), bool):
                    judged += 1
                    success_count += int(m["taskSuccess"] is True)
                else:
                    semantic_unknown += 1
            else:
                # Preserve existing retrieval-mode success semantics without presenting them as QA judgment.
                if isinstance(m.get("taskSuccess"), bool):
                    judged += 1
                    success_count += int(m["taskSuccess"] is True)

            auto_rel_obj = judge.get("answerRelevance")
            auto_rel = (
                str(auto_rel_obj.get("verdict"))
                if isinstance(auto_rel_obj, dict) and auto_rel_obj.get("verdict") in relevance_labels
                else None
            )
            if auto_rel:
                auto_relevance_counts[auto_rel] += 1
                auto_relevance_assessed += 1

            human_rel = labels.get("answerRelevance")
            if human_rel in relevance_labels:
                human_relevance_labels += 1
                if auto_rel:
                    relevance_pairs.append((auto_rel, human_rel))
            final_rel = human_rel if human_rel in relevance_labels else auto_rel
            if final_rel:
                final_relevance_counts[final_rel] += 1
                relevance_assessed += 1

            cost = m.get("costCny")
            if isinstance(cost, (int, float)):
                known_cost += cost
            else:
                unknown_cost += 1
                known_cost += float(m.get("knownCostCny") or 0.0)

            if isinstance(m.get("durationMs"), (int, float)):
                durations.append(float(m["durationMs"]))
            if isinstance(m.get("recall"), (int, float)):
                retrieval_n += 1
                recall_sum += float(m["recall"])
                hit_sum += float(m.get("hit", 0.0) or 0.0)
                mrr_sum += float(m.get("mrr", 0.0) or 0.0)
            if isinstance(m.get("ndcg"), (int, float)):
                ndcg_n += 1
                ndcg_sum += float(m["ndcg"])

            trajectory = m.get("agentTrajectory")
            if isinstance(trajectory, dict):
                agent_cases += 1
                agent_turns += int(trajectory.get("turns") or 0)
                agent_tool_calls += int(trajectory.get("toolCalls") or 0)
                agent_input_tokens += int(trajectory.get("inputTokens") or 0)
                agent_output_tokens += int(trajectory.get("outputTokens") or 0)
                agent_timeouts += 1 if trajectory.get("timeout") else 0

        p50 = EvaluationMetrics.percentile(durations, 0.5) if durations else None
        p95 = EvaluationMetrics.percentile(durations, 0.95) if durations else None

        correctness_calibration = EvaluationMetrics.correctness_calibration(correctness_pairs)
        correctness_calibration["humanLabeled"] = human_correctness_labels
        correctness_calibration["comparisonCoverage"] = EvaluationMetrics.ratio(
            correctness_calibration["compared"], human_correctness_labels
        )

        relevance_calibration = EvaluationMetrics.categorical_comparison(
            relevance_pairs, relevance_labels
        )
        relevance_calibration["humanLabeled"] = human_relevance_labels
        relevance_calibration["comparisonCoverage"] = EvaluationMetrics.ratio(
            relevance_calibration["compared"], human_relevance_labels
        )

        relevance_denominator = total if semantic_mode else 0
        relevance_summary = {
            "policy": manifest.get("relevancePolicy") or (
                RELEVANCE_POLICY if manifest.get("rubricVersion") == RUBRIC_VERSION else None
            ),
            "counts": final_relevance_counts,
            "assessed": relevance_assessed if semantic_mode else 0,
            "unevaluated": (relevance_denominator - relevance_assessed) if semantic_mode else 0,
            "coverage": EvaluationMetrics.ratio(relevance_assessed, relevance_denominator),
            "automatic": {
                "counts": auto_relevance_counts,
                "assessed": auto_relevance_assessed if semantic_mode else 0,
                "unevaluated": (relevance_denominator - auto_relevance_assessed) if semantic_mode else 0,
                "coverage": EvaluationMetrics.ratio(auto_relevance_assessed, relevance_denominator),
            },
        }

        return {
            "planned": total,
            "completed": completed,
            "failed": failed,
            "executionFailed": failed,
            "pending": pending,
            "notCompleted": total - completed - failed,
            "completionCoverage": EvaluationMetrics.ratio(completed, total),
            # Backward-compatible denominator: only already-judged results.
            "taskSuccessRate": EvaluationMetrics.ratio(success_count, judged),
            "plannedTaskSuccessRate": (
                EvaluationMetrics.ratio(success_count, total) if semantic_mode else None
            ),
            "successful": success_count if semantic_mode else None,
            "unknown": semantic_unknown if semantic_mode else None,
            "judgedCases": judged,
            "judgmentCoverage": EvaluationMetrics.ratio(judged, total),
            "humanReviewedCases": len(reviewed_result_ids),
            "humanCorrectCases": reviewed_correct,
            "answerRelevance": relevance_summary,
            "judgeCalibration": {
                "correctness": correctness_calibration,
                "answerRelevance": relevance_calibration,
            },
            "hitAt5": (hit_sum / retrieval_n) if retrieval_n else None,
            "recallAt5": (recall_sum / retrieval_n) if retrieval_n else None,
            "mrr": (mrr_sum / retrieval_n) if retrieval_n else None,
            "ndcgAt5": (ndcg_sum / ndcg_n) if ndcg_n else None,
            "retrievalDenominator": retrieval_n,
            "ndcgDenominator": ndcg_n,
            "observedP50Ms": round(p50, 2) if p50 is not None else None,
            "observedP95Ms": round(p95, 2) if p95 is not None else None,
            "knownCostCny": round(known_cost, 8),
            "unknownCostCases": unknown_cost,
            "totalCostCny": round(known_cost, 8) if unknown_cost == 0 else None,
            "agentCases": agent_cases,
            "avgAgentTurns": (agent_turns / agent_cases) if agent_cases else None,
            "avgAgentToolCalls": (agent_tool_calls / agent_cases) if agent_cases else None,
            "avgAgentTokens": ((agent_input_tokens + agent_output_tokens) / agent_cases) if agent_cases else None,
            "agentTimeoutRate": (agent_timeouts / agent_cases) if agent_cases else None,
        }

    @staticmethod
    def get_run(actor: Actor, run_id: str) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)
        with get_db_connection() as conn:
            r = conn.execute(
                select(eval_run_table).where(
                    and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id)
                )
            ).first()
            if not r:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "评估运行不存在")

            metrics = EvaluationRunner.aggregate(actor, run_id)
            return {
                "id": str(r.id),
                "runId": str(r.id),
                "status": str(r.status),
                "manifest": obj(r.manifest_json),
                "metrics": metrics,
            }

    @staticmethod
    def results(actor: Actor, run_id: str) -> List[Dict[str, Any]]:
        EvaluationCatalog.require_admin(actor)
        with get_db_connection() as conn:
            stmt = (
                select(
                    eval_result_table,
                    eval_case_table.c.case_key,
                    eval_case_table.c.question_json,
                    eval_case_table.c.expected_json,
                )
                .select_from(
                    eval_result_table.join(
                        eval_case_table,
                        and_(
                            eval_case_table.c.tenant_id == eval_result_table.c.tenant_id,
                            eval_case_table.c.id == eval_result_table.c.case_id,
                        ),
                    )
                )
                .where(
                    and_(
                        eval_result_table.c.tenant_id == actor.tenant_id,
                        eval_result_table.c.eval_run_id == run_id,
                    )
                )
                .order_by(eval_case_table.c.case_key.asc(), eval_result_table.c.repeat_no.asc())
            )
            rows = conn.execute(stmt).fetchall()

            res = []
            for e in rows:
                item = {
                    "id": str(e.id),
                    "caseId": str(e.case_id),
                    "caseKey": str(e.case_key),
                    "repeatNo": int(e.repeat_no),
                    "qaRunId": str(e.qa_run_id) if e.qa_run_id else None,
                    "status": str(e.status),
                    "question": obj(e.question_json),
                    "expected": obj(e.expected_json),
                    "metrics": obj(e.metrics_json),
                    "judge": obj(e.judge_json),
                    "errorCode": str(e.error_code) if e.error_code else None,
                }
                # Reviews
                rev_rows = conn.execute(
                    select(eval_review_table).where(
                        and_(
                            eval_review_table.c.tenant_id == actor.tenant_id,
                            eval_review_table.c.result_id == str(e.id),
                        )
                    ).order_by(eval_review_table.c.created_at.asc())
                ).fetchall()
                item["reviews"] = [
                    {
                        "id": str(x.id),
                        "labels": obj(x.labels_json),
                        "reason": x.reason_text,
                        "reviewerId": str(x.reviewer_id),
                        "createdAt": x.created_at.isoformat() if x.created_at else None,
                    }
                    for x in rev_rows
                ]
                latest_labels: Dict[str, str] = {}
                latest_review_meta: Dict[str, Dict[str, Any]] = {}
                for review in rev_rows:
                    for key, value in obj(review.labels_json).items():
                        if value is not None:
                            latest_labels[str(key)] = str(value)
                            latest_review_meta[str(key)] = {
                                "reviewId": str(review.id),
                                "reviewerId": str(review.reviewer_id),
                                "createdAt": review.created_at.isoformat() if review.created_at else None,
                                "reason": review.reason_text,
                            }
                judge = item["judge"]
                auto_relevance = judge.get("answerRelevance") if isinstance(judge.get("answerRelevance"), dict) else None
                automatic = {
                    "correctness": judge.get("correctness"),
                    "answerRelevance": auto_relevance.get("verdict") if auto_relevance else None,
                    "expectationAssessment": (
                        judge.get("expectationAssessment", {}).get("verdict")
                        if isinstance(judge.get("expectationAssessment"), dict)
                        else None
                    ),
                }
                effective = dict(automatic)
                for dimension in ("correctness", "answerRelevance", "expectationAssessment"):
                    if dimension in latest_labels:
                        effective[dimension] = latest_labels[dimension]
                item["automaticJudgment"] = automatic
                item["humanJudgment"] = {
                    "labels": latest_labels,
                    "byDimension": latest_review_meta,
                }
                item["effectiveJudgment"] = effective
                res.append(item)
            return res

    @staticmethod
    def cancel(actor: Actor, run_id: str) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            r = conn.execute(
                select(eval_run_table).where(
                    and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id)
                )
            ).first()
            if not r:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "评估运行不存在")
            if r.status not in {"QUEUED", "RUNNING"}:
                return {"runId": run_id, "status": str(r.status), "alreadyTerminal": True}

            conn.execute(
                update(eval_run_table)
                .where(and_(eval_run_table.c.tenant_id == actor.tenant_id, eval_run_table.c.id == run_id))
                .values(status="CANCELLED", updated_at=now)
            )
            conn.execute(
                update(job_table)
                .where(and_(job_table.c.tenant_id == actor.tenant_id, job_table.c.id == r.job_id))
                .values(status="CANCELLED", updated_at=now)
            )
            conn.commit()

        return {"runId": run_id, "status": "CANCELLED"}

    @staticmethod
    def review(actor: Actor, result_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)
        labels = body.get("labels") if isinstance(body.get("labels"), dict) else {"correctness": body.get("label")}
        allowed_correctness = {"CORRECT", "PARTIAL", "WRONG", "UNDETERMINED"}
        allowed_relevance = {"ON_TOPIC", "PARTLY_OFF_TOPIC", "OFF_TOPIC", "UNCERTAIN"}
        allowed_expectation = {"SATISFIED", "VIOLATED", "UNCERTAIN"}

        normalized: Dict[str, str] = {}
        if labels.get("correctness") is not None:
            value = str(labels.get("correctness")).strip().upper()
            if value not in allowed_correctness:
                raise ApiException(400, "INVALID_INPUT", "人工正确性标签无效")
            normalized["correctness"] = value
        if labels.get("answerRelevance") is not None:
            value = str(labels.get("answerRelevance")).strip().upper()
            if value not in allowed_relevance:
                raise ApiException(400, "INVALID_INPUT", "人工 Answer Relevancy 标签无效")
            normalized["answerRelevance"] = value
        if labels.get("expectationAssessment") is not None:
            value = str(labels.get("expectationAssessment")).strip().upper()
            if value not in allowed_expectation:
                raise ApiException(400, "INVALID_INPUT", "人工题型评价标签无效")
            normalized["expectationAssessment"] = value
        if not normalized:
            raise ApiException(400, "INVALID_INPUT", "至少提交一个人工评估维度")

        reason = str(body.get("reason", "")).strip()
        now = datetime.now(timezone.utc)
        review_id = str(uuid.uuid4())

        with get_db_connection() as conn:
            res = conn.execute(
                select(eval_result_table.c.id).where(
                    and_(eval_result_table.c.tenant_id == actor.tenant_id, eval_result_table.c.id == result_id)
                )
            ).first()
            if not res:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "结果不存在")

            conn.execute(
                eval_review_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=review_id,
                    result_id=result_id,
                    reviewer_id=actor.user_id,
                    labels_json=normalized,
                    reason_text=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        return {"reviewId": review_id, "labels": normalized}

    @staticmethod
    def export_run(actor: Actor, run_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)
        mode = str(body.get("mode", "REDACTED")).strip().upper()
        if mode not in {"REDACTED", "FULL"}:
            raise ApiException(400, "INVALID_INPUT", "mode无效")

        run_info = EvaluationRunner.get_run(actor, run_id)
        cases_res = EvaluationRunner.results(actor, run_id)

        if mode == "REDACTED":
            cases_res = [
                {
                    "caseKey": c["caseKey"],
                    "repeatNo": c["repeatNo"],
                    "status": c["status"],
                    "metrics": c["metrics"],
                    "errorCode": c["errorCode"],
                }
                for c in cases_res
            ]

        return {
            "runId": run_id,
            "mode": mode,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "manifest": run_info["manifest"],
            "metrics": run_info["metrics"],
            "cases": cases_res,
        }

    @staticmethod
    def compare(actor: Actor, run_ids: List[str]) -> Dict[str, Any]:
        EvaluationCatalog.require_admin(actor)
        if len(run_ids) < 2 or len(run_ids) > 4:
            raise ApiException(400, "INVALID_INPUT", "选择2到4次实验")

        runs = [EvaluationRunner.get_run(actor, rid) for rid in run_ids]
        first_m = runs[0].get("manifest", {})
        comparison_fields = (
            "datasetVersionId", "snapshotId", "split", "mode", "strict", "repeats",
            "environment", "judgeMode", "rubricVersion", "inputContractVersion",
            "evaluationScope", "relevancePolicy",
        )
        differences = [key for key in comparison_fields if any(
            r.get("manifest", {}).get(key) != first_m.get(key) for r in runs
        )]
        comparable = not differences

        return {
            "comparable": comparable,
            "differingFields": differences,
            "reason": "MATCHED_MANIFEST" if comparable else "DIFFERENT_MANIFEST",
            "runs": runs,
            "results": [{"runId": rid, "cases": EvaluationRunner.results(actor, rid)} for rid in run_ids],
        }

    @staticmethod
    def compare_with_baseline(actor: Actor, run_id: str, baseline_report_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Compare a Python/Milvus evaluation run with the frozen Java/ES baseline report.
        Reads artifacts/baseline-report.json and computes deltas per caseKey and overall metrics.
        """
        EvaluationCatalog.require_admin(actor)
        if not baseline_report_path:
            # Default to repo root artifacts/baseline-report.json
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
            baseline_report_path = os.path.join(repo_root, "artifacts", "baseline-report.json")
            if not os.path.exists(baseline_report_path):
                # Fallback to local worktree artifacts
                worktree_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                baseline_report_path = os.path.join(worktree_root, "..", "artifacts", "baseline-report.json")

        if not os.path.exists(baseline_report_path):
            raise ApiException(404, "RESOURCE_NOT_FOUND", f"未找到基线报告: {baseline_report_path}")

        with open(baseline_report_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        run = EvaluationRunner.get_run(actor, run_id)
        current_metrics = run.get("metrics", {})
        baseline_metrics = baseline.get("metrics", {})

        current_cases = EvaluationRunner.results(actor, run_id)
        current_case_map = {c["caseKey"]: c for c in current_cases}

        case_diffs = []
        matched_cases = 0
        for b_case in baseline.get("cases", []):
            k = b_case.get("caseKey")
            curr = current_case_map.get(k)
            if not curr:
                continue
            matched_cases += 1
            curr_metrics = curr.get("metrics", {})
            curr_recalled = bool(curr_metrics.get("hit", 0.0) > 0.0)
            curr_success = bool(curr_metrics.get("taskSuccess", False))
            curr_latency = curr_metrics.get("durationMs")

            case_diffs.append({
                "caseKey": k,
                "category": b_case.get("category"),
                "split": b_case.get("split"),
                "baseline": {
                    "recalled": b_case.get("recalled"),
                    "citationHit": b_case.get("citationHit"),
                    "success": b_case.get("success"),
                    "latencyMs": b_case.get("latencyMs"),
                },
                "current": {
                    "recalled": curr_recalled,
                    "citationHit": bool(curr_metrics.get("hit", 0.0) > 0.0),
                    "success": curr_success,
                    "latencyMs": curr_latency,
                    "status": curr.get("status"),
                },
                "delta": {
                    "successChanged": curr_success != b_case.get("success"),
                    "recalledChanged": curr_recalled != b_case.get("recalled"),
                },
            })

        # Metric deltas
        c_recall = current_metrics.get("recallAt5") or 0.0
        b_recall = baseline_metrics.get("recallAt5") or 0.0
        c_p50 = current_metrics.get("observedP50Ms") or 0.0
        b_p50 = baseline_metrics.get("p50LatencyMs") or 0.0
        c_success = current_metrics.get("taskSuccessRate") or 0.0
        b_success = baseline_metrics.get("answerSuccessRate") or 0.0

        return {
            "runId": run_id,
            "baselineTimestamp": baseline.get("timestamp"),
            "baselineBackend": baseline.get("backendType"),
            "matchedCasesCount": matched_cases,
            "totalBaselineCases": len(baseline.get("cases", [])),
            "configurationDifferences": {
                "sparseIndexer": {
                    "baseline": baseline.get("configuration", {}).get("sparseIndexer", "Elasticsearch BM25"),
                    "current": "Milvus BM25 Function",
                },
                "denseIndexer": {
                    "baseline": baseline.get("configuration", {}).get("denseIndexer", "Elasticsearch kNN HNSW"),
                    "current": "Milvus HNSW Dense",
                },
                "comparableNote": "两者均采用 BM25 + Dense 混合检索与 RRF 融合，但底层引擎从 Elasticsearch 迁移至 Milvus 2.4+",
            },
            "metricDeltas": {
                "recallAt5": {
                    "baseline": b_recall,
                    "current": round(c_recall, 4),
                    "delta": round(c_recall - b_recall, 4),
                },
                "taskSuccessRate": {
                    "baseline": b_success,
                    "current": round(c_success, 4),
                    "delta": round(c_success - b_success, 4),
                },
                "p50LatencyMs": {
                    "baseline": b_p50,
                    "current": round(c_p50, 2),
                    "delta": round(c_p50 - b_p50, 2),
                },
            },
            "caseDiffs": case_diffs,
        }
