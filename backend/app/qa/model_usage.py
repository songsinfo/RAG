"""Reserve budgets before each external attempt; unknown bills remain unknown."""
import json
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP

from sqlalchemy import and_, or_, select, update, func

from app.config import is_test_env
from app.db import get_db_connection
from app.errors import ApiException
from app.models import model_call_table, qa_run_table, tenant_table, eval_run_table
from app.qa.runtime import AgentRuntimePolicy

_usage_owner = ContextVar("model_usage_owner", default=(None, None, None))


@contextmanager
def usage_scope(tenant_id, run_id=None, job_id=None):
    if job_id is None and run_id is None:
        job_id = _usage_owner.get()[2]
    token = _usage_owner.set((tenant_id, run_id, job_id))
    try:
        yield
    finally:
        _usage_owner.reset(token)


def model_prices(alias):
    try:
        price = json.loads(os.getenv("RAG_MODEL_PRICES_CNY", "{}"))[alias]
        values = [Decimal(str(price[k])) for k in ("inputPerMillion", "outputPerMillion")]
        if any(not v.is_finite() or v < 0 for v in values):
            raise ValueError()
        return values
    except (KeyError, TypeError, ValueError, ArithmeticError):
        if is_test_env():
            return None
        raise ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", f"请配置 {alias} 的人民币预算费率")


def token_ceiling(payload):
    # UTF-8 byte count is a conservative text-token reservation, not a usage bill.
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256


def invoke_tracked(fn, *, tenant_id=None, run_id=None, job_id=None, purpose, alias,
                   payload, max_output_tokens=0, usage_reader=None):
    owner = _usage_owner.get()
    tenant_id = tenant_id or owner[0]
    run_id = run_id or owner[1]
    job_id = job_id or (owner[2] if run_id is None else None)
    if not tenant_id:
        if is_test_env():
            return fn()
        raise ApiException(500, "MODEL_USAGE_OWNER_REQUIRED", "模型调用缺少租户归属")
    prices = model_prices(alias)
    input_limit = token_ceiling(payload)
    output_limit = max(0, int(max_output_tokens))
    reserved = ((input_limit * prices[0] + output_limit * prices[1]) / Decimal(1000000)).quantize(
        Decimal("0.000001"), rounding=ROUND_UP) if prices else Decimal(0)
    call_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    policy = AgentRuntimePolicy.from_env()
    with get_db_connection() as conn:
        # Serialize reservations across workers and runs of the same tenant.
        conn.execute(select(tenant_table.c.id).where(tenant_table.c.id == tenant_id).with_for_update()).scalar_one()
        if run_id:
            run = conn.execute(select(qa_run_table.c.status, qa_run_table.c.budget_json).where(and_(
                qa_run_table.c.tenant_id == tenant_id, qa_run_table.c.id == run_id,
            )).with_for_update()).first()
            if not run or run.status == "CANCELLED":
                raise ApiException(409, "CANCELLED", "模型调用所属运行已取消或不存在")
            calls = conn.execute(select(model_call_table.c.input_tokens, model_call_table.c.output_tokens).where(and_(
                model_call_table.c.tenant_id == tenant_id, model_call_table.c.run_id == run_id,
            ))).fetchall()
            if len(calls) >= policy.max_model_calls or sum(int(c.input_tokens or 0) + int(c.output_tokens or 0) for c in calls) + input_limit + output_limit > policy.max_total_tokens:
                raise ApiException(429, "BUDGET_EXCEEDED", "本次模型调用将超过调用或 Token 预算")
            eval_id = ((run.budget_json or {}).get("evaluationScope") or {}).get("evalRunId")
        else:
            eval_id = None
        if prices:
            monthly_reserved = conn.execute(select(func.coalesce(func.sum(model_call_table.c.reserved_amount), 0)).where(and_(
                model_call_table.c.tenant_id == tenant_id,
                model_call_table.c.created_at >= now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            ))).scalar_one()
            monthly_limit = Decimal(os.getenv("RAG_MONTHLY_BUDGET_CNY", "100"))
            if not monthly_limit.is_finite() or monthly_limit < 0:
                raise ApiException(409, "INVALID_CONFIG", "月预算必须是非负有限数值")
            if Decimal(str(monthly_reserved)) + reserved > monthly_limit:
                raise ApiException(429, "BUDGET_EXCEEDED", "租户本月模型预算预留已耗尽")
        eval_run = None
        if eval_id or job_id:
            eval_run = conn.execute(select(eval_run_table.c.id, eval_run_table.c.job_id, eval_run_table.c.status,
                                           eval_run_table.c.manifest_json).where(and_(
                eval_run_table.c.tenant_id == tenant_id,
                eval_run_table.c.id == eval_id if eval_id else eval_run_table.c.job_id == job_id,
            ))).first()
        if eval_run:
            if eval_run.status == "CANCELLED":
                raise ApiException(409, "CANCELLED", "评测已取消")
            qa_ids = select(qa_run_table.c.id).where(and_(
                qa_run_table.c.tenant_id == tenant_id,
                qa_run_table.c.budget_json["evaluationScope"]["evalRunId"].as_string() == str(eval_run.id),
            ))
            batch_reserved = conn.execute(select(func.coalesce(func.sum(model_call_table.c.reserved_amount), 0)).where(and_(
                model_call_table.c.tenant_id == tenant_id,
                or_(model_call_table.c.job_id == eval_run.job_id, model_call_table.c.run_id.in_(qa_ids)),
            ))).scalar_one()
            if Decimal(str(batch_reserved)) + reserved > Decimal(str(eval_run.manifest_json["maxCostCny"])):
                raise ApiException(429, "BATCH_BUDGET_EXCEEDED", "评测批次模型预算预留已耗尽")
        attempts = conn.execute(select(func.count()).select_from(model_call_table).where(and_(
            model_call_table.c.tenant_id == tenant_id, model_call_table.c.run_id == run_id,
            model_call_table.c.job_id == job_id, model_call_table.c.purpose == purpose,
        ))).scalar_one()
        conn.execute(model_call_table.insert().values(
            tenant_id=tenant_id, id=call_id, run_id=run_id, job_id=job_id, purpose=purpose,
            model_config_id=f"cfg-model-{alias}", attempt_no=attempts + 1, status="RUNNING",
            reserved_amount=str(reserved), settled_amount=None,
            input_tokens=input_limit, output_tokens=output_limit,
            pricing_snapshot={"source": "CONFIGURED_CNY" if prices else "UNKNOWN",
                              "inputPerMillion": str(prices[0]) if prices else None,
                              "outputPerMillion": str(prices[1]) if prices else None},
            usage_detail={"model": alias, "tokenStatus": "RESERVED", "billingStatus": "UNKNOWN"},
            reservation_authority="INTERNAL", reservation_id=call_id, created_at=now, updated_at=now,
        ))
        conn.commit()
    try:
        result = fn()
        if getattr(result, "status_code", 200) >= 400:
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"模型网关返回状态码 {result.status_code}")
        usage = usage_reader(result) if usage_reader else {}
        values = {"status": "SUCCESS", "updated_at": datetime.now(timezone.utc)}
        if usage and usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
            reported = (int(usage["input_tokens"]), int(usage["output_tokens"]))
            if all(n >= 0 for n in reported):
                values.update(input_tokens=reported[0], output_tokens=reported[1])
        values["usage_detail"] = {"model": alias, "tokenStatus": "REPORTED" if len(values) > 2 else "RESERVED",
                                  "billingStatus": "UNKNOWN"}
        with get_db_connection() as conn:
            conn.execute(update(model_call_table).where(and_(
                model_call_table.c.tenant_id == tenant_id, model_call_table.c.id == call_id,
            )).values(**values))
            conn.commit()
        return result
    except Exception:
        # A timeout may already have incurred usage. Retain its reservation.
        with get_db_connection() as conn:
            conn.execute(update(model_call_table).where(and_(
                model_call_table.c.tenant_id == tenant_id, model_call_table.c.id == call_id,
            )).values(status="FAILED", updated_at=datetime.now(timezone.utc)))
            conn.commit()
        raise
