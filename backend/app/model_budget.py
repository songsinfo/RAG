import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Dict

from sqlalchemy import and_, func, select, update

from app.db import get_db_connection
from app.errors import ApiException
from app.models import (
    budget_reservation_table,
    config_revision_table,
    tenant_budget_table,
    qa_run_table,
    job_table,
)


_MILLION = Decimal("1000000")
_QUANT = Decimal("0.000001")


class BudgetService:
    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", f"模型价格字段无效: {field}") from exc
        if result < 0:
            raise ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", f"模型价格字段不能为负数: {field}")
        return result

    @classmethod
    def pricing(cls, tenant_id: str, model_alias: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            rows = conn.execute(
                select(
                    config_revision_table.c.id,
                    config_revision_table.c.payload,
                    config_revision_table.c.status,
                    config_revision_table.c.revision,
                    config_revision_table.c.updated_at,
                )
                .where(
                    and_(
                        config_revision_table.c.tenant_id == tenant_id,
                        config_revision_table.c.kind == "MODEL",
                        config_revision_table.c.status.in_(["ACTIVE", "PUBLISHED"]),
                    )
                )
                .order_by(
                    config_revision_table.c.updated_at.desc(),
                    config_revision_table.c.revision.desc(),
                )
            ).fetchall()
        row = next(
            (
                item
                for item in rows
                if isinstance(item.payload, dict)
                and str(item.payload.get("modelAlias") or "").strip() == model_alias
            ),
            None,
        )
        if not row:
            raise ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", f"模型价格配置不存在: {model_alias}")
        payload = row.payload if isinstance(row.payload, dict) else {}
        if str(payload.get("currency", "CNY")).upper() != "CNY":
            raise ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", "模型价格必须使用 CNY")
        input_rate = cls._decimal(payload.get("inputPricePerMillion"), "inputPricePerMillion")
        output_rate = cls._decimal(payload.get("outputPricePerMillion"), "outputPricePerMillion")
        return {
            "configId": str(row.id),
            "modelAlias": model_alias,
            "currency": "CNY",
            "inputPricePerMillion": input_rate,
            "outputPricePerMillion": output_rate,
        }

    @classmethod
    def estimate(cls, pricing: Dict[str, Any], input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ApiException(400, "INVALID_INPUT", "Token 数不能为负数")
        input_rate = cls._decimal(pricing.get("inputPricePerMillion"), "inputPricePerMillion")
        output_rate = cls._decimal(pricing.get("outputPricePerMillion"), "outputPricePerMillion")
        amount = (
            input_rate * Decimal(input_tokens)
            + output_rate * Decimal(output_tokens)
        ) / _MILLION
        return amount.quantize(_QUANT, rounding=ROUND_CEILING)

    @classmethod
    def reserve(
        cls,
        tenant_id: str,
        call_id: str,
        model_alias: str,
        input_upper_bound: int,
        output_upper_bound: int,
        caller_environment: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
    ) -> Dict[str, Any]:
        pricing = cls.pricing(tenant_id, model_alias)
        amount = cls.estimate(pricing, input_upper_bound, output_upper_bound)
        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        base_environment = caller_environment or os.getenv("RAG_ENVIRONMENT", "demo")
        monthly_limit = cls._decimal(os.getenv("RAG_MONTHLY_BUDGET_CNY", "100"), "RAG_MONTHLY_BUDGET_CNY")

        with get_db_connection() as conn:
            batch_job_id = None
            if run_id:
                run_row = conn.execute(
                    select(qa_run_table.c.budget_json).where(
                        and_(
                            qa_run_table.c.tenant_id == tenant_id,
                            qa_run_table.c.id == run_id,
                        )
                    )
                ).first()
                budget_json = run_row.budget_json if run_row and isinstance(run_row.budget_json, dict) else {}
                batch_job_id = str(budget_json.get("evaluationJobId") or "") or None
            elif job_id:
                owner_job = conn.execute(
                    select(job_table.c.kind, job_table.c.status).where(
                        and_(
                            job_table.c.tenant_id == tenant_id,
                            job_table.c.id == job_id,
                        )
                    )
                ).first()
                if owner_job and str(owner_job.kind) == "EVALUATE":
                    batch_job_id = job_id

            environment = (
                f"{base_environment}|eval:{batch_job_id}"
                if batch_job_id
                else base_environment
            )

            existing = conn.execute(
                select(budget_reservation_table).where(
                    and_(
                        budget_reservation_table.c.tenant_id == tenant_id,
                        budget_reservation_table.c.caller_environment == environment,
                        budget_reservation_table.c.caller_call_id == call_id,
                    )
                )
            ).first()
            if existing:
                if Decimal(str(existing.reserved_amount)) != amount:
                    raise ApiException(409, "BUDGET_RESERVATION_CONFLICT", "重复调用的预算预留金额不一致")
                return {
                    "id": str(existing.id),
                    "amount": amount,
                    "pricing": pricing,
                    "status": str(existing.status),
                }

            budget = conn.execute(
                select(tenant_budget_table)
                .where(
                    and_(
                        tenant_budget_table.c.tenant_id == tenant_id,
                        tenant_budget_table.c.period_month == month,
                    )
                )
                .with_for_update()
            ).first()
            if not budget:
                budget_id = str(uuid.uuid4())
                conn.execute(
                    tenant_budget_table.insert().values(
                        tenant_id=tenant_id,
                        id=budget_id,
                        period_month=month,
                        currency="CNY",
                        limit_amount=monthly_limit,
                        reserved_amount=Decimal("0"),
                        settled_amount=Decimal("0"),
                        created_at=now,
                        updated_at=now,
                    )
                )
                budget = conn.execute(
                    select(tenant_budget_table)
                    .where(
                        and_(
                            tenant_budget_table.c.tenant_id == tenant_id,
                            tenant_budget_table.c.id == budget_id,
                        )
                    )
                    .with_for_update()
                ).first()

            if batch_job_id:
                batch_job = conn.execute(
                    select(job_table)
                    .where(
                        and_(
                            job_table.c.tenant_id == tenant_id,
                            job_table.c.id == batch_job_id,
                            job_table.c.kind == "EVALUATE",
                        )
                    )
                    .with_for_update()
                ).first()
                if not batch_job or str(batch_job.status) in {"FAILED", "CANCELLED"}:
                    raise ApiException(409, "BATCH_NOT_RUNNING", "评测批次已停止")

                checkpoint = batch_job.checkpoint if isinstance(batch_job.checkpoint, dict) else {}
                max_cost = cls._decimal(checkpoint.get("maxCostCny", 10), "maxCostCny")
                max_calls = int(checkpoint.get("maxModelCalls", 1000))

                batch_rows = conn.execute(
                    select(
                        budget_reservation_table.c.status,
                        budget_reservation_table.c.reserved_amount,
                        budget_reservation_table.c.settled_amount,
                    ).where(
                        and_(
                            budget_reservation_table.c.tenant_id == tenant_id,
                            budget_reservation_table.c.caller_environment == environment,
                        )
                    )
                ).fetchall()
                if len(batch_rows) >= max_calls:
                    raise ApiException(429, "BUDGET_EXCEEDED", "后台批次模型调用次数上限已达到")
                batch_amount = Decimal("0")
                for row in batch_rows:
                    if str(row.status) == "NOT_SENT":
                        continue
                    if str(row.status) == "SETTLED" and row.settled_amount is not None:
                        batch_amount += Decimal(str(row.settled_amount))
                    else:
                        batch_amount += Decimal(str(row.reserved_amount or 0))
                if batch_amount + amount > max_cost:
                    raise ApiException(429, "BUDGET_EXCEEDED", "后台批次费用上限已达到")

            in_flight = conn.execute(
                select(func.count()).select_from(budget_reservation_table).where(
                    and_(
                        budget_reservation_table.c.tenant_id == tenant_id,
                        budget_reservation_table.c.status == "RESERVED",
                        budget_reservation_table.c.expires_at > now,
                    )
                )
            ).scalar() or 0
            if int(in_flight) >= 3:
                raise ApiException(429, "RATE_LIMITED", "模型调用在途并发已达到上限")

            reserved = Decimal(str(budget.reserved_amount or 0))
            settled = Decimal(str(budget.settled_amount or 0))
            limit_amount = Decimal(str(budget.limit_amount))
            if reserved + settled + amount > limit_amount:
                raise ApiException(429, "BUDGET_RESERVATION_DENIED", "月度模型预算不足")

            reservation_id = str(uuid.uuid4())
            conn.execute(
                update(tenant_budget_table)
                .where(
                    and_(
                        tenant_budget_table.c.tenant_id == tenant_id,
                        tenant_budget_table.c.id == budget.id,
                    )
                )
                .values(
                    reserved_amount=tenant_budget_table.c.reserved_amount + amount,
                    updated_at=now,
                )
            )
            conn.execute(
                budget_reservation_table.insert().values(
                    tenant_id=tenant_id,
                    id=reservation_id,
                    budget_id=str(budget.id),
                    caller_environment=environment,
                    caller_call_id=call_id,
                    status="RESERVED",
                    reserved_amount=amount,
                    settled_amount=None,
                    expires_at=now + timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        return {
            "id": reservation_id,
            "amount": amount,
            "pricing": pricing,
            "status": "RESERVED",
        }

    @classmethod
    def settle(
        cls,
        tenant_id: str,
        reservation_id: str,
        status: str,
        actual_amount: Decimal | None,
    ) -> Decimal | None:
        if status not in {"SETTLED", "NOT_SENT", "UNKNOWN"}:
            raise ApiException(400, "INVALID_INPUT", "预算结算状态无效")
        if actual_amount is not None and actual_amount < 0:
            raise ApiException(400, "INVALID_INPUT", "结算金额不能为负数")

        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            row = conn.execute(
                select(budget_reservation_table)
                .where(
                    and_(
                        budget_reservation_table.c.tenant_id == tenant_id,
                        budget_reservation_table.c.id == reservation_id,
                    )
                )
                .with_for_update()
            ).first()
            if not row:
                raise ApiException(404, "BUDGET_RESERVATION_NOT_FOUND", "预算预留不存在")

            current_status = str(row.status)
            if current_status in {"SETTLED", "NOT_SENT"}:
                current = Decimal(str(row.settled_amount or 0))
                expected = Decimal("0") if status == "NOT_SENT" else actual_amount
                if status != current_status or (expected is not None and current != expected):
                    raise ApiException(409, "BUDGET_SETTLEMENT_CONFLICT", "预算预留已按不同结果结算")
                return current

            if status == "UNKNOWN":
                conn.execute(
                    update(budget_reservation_table)
                    .where(
                        and_(
                            budget_reservation_table.c.tenant_id == tenant_id,
                            budget_reservation_table.c.id == reservation_id,
                        )
                    )
                    .values(status="UNKNOWN", updated_at=now)
                )
                conn.commit()
                return None

            settled_amount = Decimal("0") if status == "NOT_SENT" else (actual_amount or Decimal("0"))
            conn.execute(
                update(tenant_budget_table)
                .where(
                    and_(
                        tenant_budget_table.c.tenant_id == tenant_id,
                        tenant_budget_table.c.id == row.budget_id,
                    )
                )
                .values(
                    reserved_amount=tenant_budget_table.c.reserved_amount - Decimal(str(row.reserved_amount)),
                    settled_amount=tenant_budget_table.c.settled_amount + settled_amount,
                    updated_at=now,
                )
            )
            conn.execute(
                update(budget_reservation_table)
                .where(
                    and_(
                        budget_reservation_table.c.tenant_id == tenant_id,
                        budget_reservation_table.c.id == reservation_id,
                    )
                )
                .values(
                    status=status,
                    settled_amount=settled_amount,
                    updated_at=now,
                )
            )
            conn.commit()
            return settled_amount

    @classmethod
    def settle_call(
        cls,
        tenant_id: str,
        call_id: str,
        model_alias: str,
        call_status: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Dict[str, Any]:
        with get_db_connection() as conn:
            row = conn.execute(
                select(budget_reservation_table).where(
                    and_(
                        budget_reservation_table.c.tenant_id == tenant_id,
                        budget_reservation_table.c.caller_call_id == call_id,
                    )
                )
            ).first()
        if not row:
            return {
                "reservedAmount": Decimal("0"),
                "settledAmount": Decimal("0") if call_status == "SUCCESS" else None,
                "pricing": {"rate": "unreserved"},
                "reservationId": None,
                "authority": "NONE",
            }

        pricing = cls.pricing(tenant_id, model_alias)
        if call_status == "SUCCESS":
            actual = cls.estimate(pricing, input_tokens, output_tokens)
            cls.settle(tenant_id, str(row.id), "SETTLED", actual)
            settled = actual
        elif call_status == "NOT_SENT":
            cls.settle(tenant_id, str(row.id), "NOT_SENT", Decimal("0"))
            settled = Decimal("0")
        else:
            cls.settle(tenant_id, str(row.id), "UNKNOWN", None)
            settled = None
        return {
            "reservedAmount": Decimal(str(row.reserved_amount)),
            "settledAmount": settled,
            "pricing": {
                "configId": str(pricing["configId"]),
                "currency": "CNY",
                "inputPricePerMillion": str(pricing["inputPricePerMillion"]),
                "outputPricePerMillion": str(pricing["outputPricePerMillion"]),
                "modelAlias": model_alias,
            },
            "reservationId": str(row.id),
            "authority": "LOCAL_MONTHLY_BUDGET",
        }

    @staticmethod
    def summary(tenant_id: str):
        with get_db_connection() as conn:
            rows = conn.execute(
                select(tenant_budget_table)
                .where(tenant_budget_table.c.tenant_id == tenant_id)
                .order_by(tenant_budget_table.c.period_month.desc())
                .limit(12)
            ).fetchall()
        return [
            {
                "periodMonth": str(row.period_month),
                "currency": str(row.currency),
                "limitAmount": str(row.limit_amount),
                "reservedAmount": str(row.reserved_amount),
                "settledAmount": str(row.settled_amount),
            }
            for row in rows
        ]
