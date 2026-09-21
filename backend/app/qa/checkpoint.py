from copy import deepcopy
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, select, update

from app.db import get_db_connection
from app.models import job_table
from app.qa.runtime import AgentRuntimePolicy
from app.qa.execution import current_qa_execution


def load_agent_checkpoint(tenant_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            select(job_table.c.checkpoint).where(
                and_(
                    job_table.c.tenant_id == tenant_id,
                    job_table.c.kind == "QA_RUN",
                    job_table.c.target_id == run_id,
                )
            )
        ).first()
    checkpoint = dict(row.checkpoint or {}) if row and row.checkpoint else {}
    runtime = checkpoint.get("agentRuntime")
    return deepcopy(runtime) if isinstance(runtime, dict) else None


def save_agent_checkpoint(
    tenant_id: str,
    run_id: str,
    *,
    state: Dict[str, Any],
    next_node: str,
    deadline_epoch: float,
    pending_approval: Optional[Dict[str, Any]] = None,
) -> None:
    now = datetime.now(timezone.utc)
    current = current_qa_execution()
    with get_db_connection() as conn:
        clauses = [
            job_table.c.tenant_id == tenant_id,
            job_table.c.kind == "QA_RUN",
            job_table.c.target_id == run_id,
            job_table.c.status.in_(["QUEUED", "RUNNING", "WAITING_APPROVAL"]),
        ]
        if current is not None:
            job_id, lease_epoch = current
            clauses.extend([
                job_table.c.id == job_id,
                job_table.c.status == "RUNNING",
                job_table.c.lease_epoch == lease_epoch,
            ])
        row = conn.execute(
            select(job_table.c.id, job_table.c.checkpoint).where(and_(*clauses))
        ).first()
        if not row:
            return

        checkpoint = dict(row.checkpoint or {})
        checkpoint["agentRuntime"] = {
            "version": 1,
            "nextNode": next_node,
            "deadlineEpoch": deadline_epoch,
            "state": deepcopy(state),
            "pendingApproval": deepcopy(pending_approval),
            "updatedAt": now.isoformat(),
        }
        result = conn.execute(
            update(job_table)
            .where(and_(*clauses, job_table.c.id == row.id))
            .values(checkpoint=checkpoint, updated_at=now)
        )
        if result.rowcount == 1:
            conn.commit()
        else:
            conn.rollback()

def resolve_agent_approval(
    tenant_id: str,
    run_id: str,
    tool_call_id: str,
    approved: bool,
) -> bool:
    now = datetime.now(timezone.utc)
    with get_db_connection() as conn:
        row = conn.execute(
            select(job_table.c.id, job_table.c.checkpoint).where(
                and_(
                    job_table.c.tenant_id == tenant_id,
                    job_table.c.kind == "QA_RUN",
                    job_table.c.target_id == run_id,
                    job_table.c.status == "WAITING_APPROVAL",
                )
            )
        ).first()
        if not row:
            return False
        checkpoint = dict(row.checkpoint or {})
        runtime = checkpoint.get("agentRuntime")
        if not isinstance(runtime, dict):
            return False
        pending = runtime.get("pendingApproval")
        if not isinstance(pending, dict) or str(pending.get("toolCallId")) != tool_call_id:
            return False

        state = dict(runtime.get("state") or {})
        key = "approved_tool_calls" if approved else "rejected_tool_calls"
        values = list(state.get(key) or [])
        if tool_call_id not in values:
            values.append(tool_call_id)
        state[key] = values
        runtime["state"] = state
        runtime["pendingApproval"] = None
        runtime["nextNode"] = "tools"
        # Human approval time is outside the active execution budget. Resume
        # with a fresh execution deadline while keeping the same turn/tool/token budgets.
        runtime["deadlineEpoch"] = time.time() + AgentRuntimePolicy.from_env().overall_timeout_seconds
        runtime["updatedAt"] = now.isoformat()
        checkpoint["agentRuntime"] = runtime

        conn.execute(
            update(job_table)
            .where(and_(
                job_table.c.tenant_id == tenant_id,
                job_table.c.id == row.id,
                job_table.c.status == "WAITING_APPROVAL",
            ))
            .values(
                checkpoint=checkpoint,
                status="QUEUED",
                stage="QA",
                available_at=now,
                lease_until=None,
                error_code=None,
                updated_at=now,
            )
        )
        conn.commit()
        return True
