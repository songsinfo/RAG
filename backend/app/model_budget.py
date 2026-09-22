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
        config_id = f"cfg-model-{model_alias}"
        with get_db_connection() as conn:
            row = conn.execute(
                select(
                    config_revision_table.c.id,
                    config_revision_table.c.payload,
                    config_revision_table.c.status,
                ).where(
                    and_(
                        config_revision_table.c.tenant_id == tenant_id,
                        config_revision_table.c.id == config_id,
                        config_revision_table.c.kind == "MODEL",
                    )
                )
            ).first()
        if not row or str(row.status).upper() not in {"ACTIVE", "PUBLISHED"}:
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
    ) -> Dict[str, Any]:
        pricing = cls.pricing(tenant_id, model_alias)
        amount = cls.estimate(pricing, input_upper_bound, output_upper_bound)
        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        environment = os.getenv("RAG_ENVIRONMENT", "demo")
        monthly_limit = cls._decimal(os.getenv("RAG_MONTHLY_BUDGET_CNY", "100"), "RAG_MONTHLY_BUDGET_CNY")

        with get_db_connection() as conn:
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
        environment = os.getenv("RAG_ENVIRONMENT", "demo")
        with get_db_connection() as conn:
            row = conn.execute(
                select(budget_reservation_table).where(
                    and_(
                        budget_reservation_table.c.tenant_id == tenant_id,
                        budget_reservation_table.c.caller_environment == environment,
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
        else:
            cls.settle(tenant_id, str(row.id), "UNKNOWN", None)
            settled = None
        return {
            "reservedAmount": Decimal(str(row.reserved_amount)),
            "settledAmount": settled,
            "pricing": {
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
