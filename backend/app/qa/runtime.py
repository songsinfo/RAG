import json
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import httpx
from sqlalchemy import and_, func, select

from app.db import get_db_connection
from app.errors import ApiException
from app.models import model_call_table


T = TypeVar("T")


@dataclass(frozen=True)
class AgentRuntimePolicy:
    overall_timeout_seconds: int = 120
    max_rounds: int = 6
    max_tool_calls: int = 8
    repeated_call_limit: int = 3
    max_context_tokens: int = 20000
    max_total_tokens: int = 50000
    max_model_calls: int = 12
    call_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.25
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: int = 30
    approval_tools: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> "AgentRuntimePolicy":
        approval_tools = frozenset(
            item.strip()
            for item in os.getenv("AGENT_APPROVAL_TOOLS", "").split(",")
            if item.strip()
        )
        return cls(
            overall_timeout_seconds=max(5, int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))),
            max_rounds=max(1, int(os.getenv("AGENT_MAX_ROUNDS", "6"))),
            max_tool_calls=max(1, int(os.getenv("AGENT_MAX_TOOL_CALLS", "8"))),
            repeated_call_limit=max(2, int(os.getenv("AGENT_REPEAT_CALL_LIMIT", "3"))),
            max_context_tokens=max(1000, int(os.getenv("AGENT_MAX_CONTEXT_TOKENS", "20000"))),
            max_total_tokens=max(1000, int(os.getenv("AGENT_MAX_TOTAL_TOKENS", "50000"))),
            max_model_calls=max(1, int(os.getenv("AGENT_MAX_MODEL_CALLS", "12"))),
            call_max_attempts=max(1, min(5, int(os.getenv("AGENT_CALL_MAX_ATTEMPTS", "3")))),
            retry_base_delay_seconds=max(0.05, float(os.getenv("AGENT_RETRY_BASE_DELAY_SECONDS", "0.25"))),
            circuit_failure_threshold=max(2, int(os.getenv("AGENT_CIRCUIT_FAILURE_THRESHOLD", "5"))),
            circuit_reset_seconds=max(5, int(os.getenv("AGENT_CIRCUIT_RESET_SECONDS", "30"))),
            approval_tools=approval_tools,
        )


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # Conservative mixed Chinese/English approximation.
    return max(1, (len(value) + 2) // 3)


def message_tokens(messages: List[Dict[str, Any]]) -> int:
    return sum(estimate_tokens(message) for message in messages)


def compact_messages(
    messages: List[Dict[str, Any]],
    max_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    original_tokens = message_tokens(messages)
    if original_tokens <= max_tokens:
        return list(messages), {
            "trimmed": False,
            "droppedMessages": 0,
            "estimatedTokens": original_tokens,
        }

    source = [dict(message) for message in messages]
    system = next((m for m in source if m.get("role") == "system"), None)
    user_indexes = [i for i, m in enumerate(source) if m.get("role") == "user"]
    current_user_index = user_indexes[-1] if user_indexes else 0
    current_user = source[current_user_index] if source else {"role": "user", "content": ""}

    # Previous conversation turns are the first candidates for compaction. The
    # current user request and newest assistant/tool exchanges are preserved.
    tail = source[current_user_index + 1:]
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for message in tail:
        if message.get("role") == "assistant" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)

    mandatory = ([system] if system else []) + [current_user]
    kept_groups: List[List[Dict[str, Any]]] = []
    dropped_groups: List[List[Dict[str, Any]]] = []
    for group in reversed(groups):
        candidate_groups = [group] + kept_groups
        candidate = mandatory + [item for g in candidate_groups for item in g]
        if message_tokens(candidate) <= max_tokens:
            kept_groups.insert(0, group)
        else:
            dropped_groups.insert(0, group)

    # Everything before the current user plus any old tool interaction groups
    # becomes a non-factual execution summary.
    dropped_messages = source[:current_user_index] + [
        item for group in dropped_groups for item in group
    ]
    if system and dropped_messages and dropped_messages[0] is system:
        dropped_messages = dropped_messages[1:]

    tool_names: List[str] = []
    evidence_ids: List[str] = []
    for message in dropped_messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if call.get("name"):
                    tool_names.append(str(call["name"]))
        if message.get("role") == "tool":
            try:
                payload = json.loads(str(message.get("content") or "{}"))
                for evidence in payload.get("evidence") or []:
                    if evidence.get("evidenceId"):
                        evidence_ids.append(str(evidence["evidenceId"]))
            except Exception:
                pass

    summary = {
        "role": "system",
        "content": (
            "Earlier conversation/agent steps were compacted for context budget. "
            f"Dropped messages={len(dropped_messages)}; "
            f"tools={sorted(set(tool_names))}; evidenceIds={sorted(set(evidence_ids))}. "
            "This is execution metadata, not factual evidence."
        ),
    }
    compacted = ([system] if system else []) + [summary, current_user] + [
        item for group in kept_groups for item in group
    ]

    # Bound giant tool payloads while preserving assistant/tool pairing.
    if message_tokens(compacted) > max_tokens:
        for message in compacted:
            if message.get("role") == "tool":
                content = str(message.get("content") or "")
                if len(content) > 1000:
                    message["content"] = content[:1000] + "...<trimmed>"
            if message_tokens(compacted) <= max_tokens:
                break

    return compacted, {
        "trimmed": True,
        "droppedMessages": len(dropped_messages),
        "estimatedTokens": message_tokens(compacted),
    }


def get_run_usage(tenant_id: str, run_id: str) -> Dict[str, int]:
    with get_db_connection() as conn:
        row = conn.execute(
            select(
                func.count(model_call_table.c.id).label("calls"),
                func.coalesce(func.sum(model_call_table.c.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(model_call_table.c.output_tokens), 0).label("output_tokens"),
            ).where(
                and_(
                    model_call_table.c.tenant_id == tenant_id,
                    model_call_table.c.run_id == run_id,
                )
            )
        ).first()
    input_tokens = int(row.input_tokens or 0) if row else 0
    output_tokens = int(row.output_tokens or 0) if row else 0
    return {
        "modelCalls": int(row.calls or 0) if row else 0,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }


def assert_runtime_budget(
    tenant_id: str,
    run_id: str,
    deadline_epoch: float,
    policy: AgentRuntimePolicy,
) -> Dict[str, int]:
    if time.time() >= deadline_epoch:
        raise ApiException(408, "AGENT_TIMEOUT", "Agent 执行超过整体时间预算")
    usage = get_run_usage(tenant_id, run_id)
    if usage["modelCalls"] >= policy.max_model_calls:
        raise ApiException(429, "BUDGET_EXCEEDED", "Agent 模型调用次数预算已耗尽")
    if usage["totalTokens"] >= policy.max_total_tokens:
        raise ApiException(429, "BUDGET_EXCEEDED", "Agent Token 预算已耗尽")
    return usage


_RETRYABLE_CODES = {
    "MODEL_GATEWAY_UNAVAILABLE",
    "RERANK_UNAVAILABLE",
    "MILVUS_UNAVAILABLE",
    "DOCLING_UNAVAILABLE",
}


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, ApiException):
        return exc.code in _RETRYABLE_CODES or exc.status_code in {429, 502, 503, 504}
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("429", "502", "503", "504", "timeout", "temporarily unavailable"))


class CircuitBreaker:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, float]] = {}

    def before_call(self, key: str, policy: AgentRuntimePolicy) -> None:
        with self._lock:
            state = self._state.get(key)
            if not state:
                return
            if state["failures"] < policy.circuit_failure_threshold:
                return
            if time.time() - state["opened_at"] >= policy.circuit_reset_seconds:
                self._state.pop(key, None)
                return
        raise ApiException(503, "CIRCUIT_OPEN", f"{key} 暂时熔断，请稍后重试")

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def failure(self, key: str, policy: AgentRuntimePolicy) -> None:
        with self._lock:
            state = self._state.setdefault(key, {"failures": 0.0, "opened_at": 0.0})
            state["failures"] += 1
            if state["failures"] >= policy.circuit_failure_threshold and not state["opened_at"]:
                state["opened_at"] = time.time()


circuit_breaker = CircuitBreaker()


def retry_call(
    key: str,
    fn: Callable[[], T],
    *,
    policy: Optional[AgentRuntimePolicy] = None,
    deadline_epoch: Optional[float] = None,
) -> T:
    policy = policy or AgentRuntimePolicy.from_env()
    circuit_breaker.before_call(key, policy)
    last_exc: Optional[Exception] = None

    for attempt in range(1, policy.call_max_attempts + 1):
        if deadline_epoch is not None and time.time() >= deadline_epoch:
            raise ApiException(408, "AGENT_TIMEOUT", "Agent 执行超过整体时间预算")
        try:
            value = fn()
            circuit_breaker.success(key)
            return value
        except Exception as exc:
            last_exc = exc
            if not is_retryable_exception(exc) or attempt >= policy.call_max_attempts:
                circuit_breaker.failure(key, policy)
                raise
            sleep_seconds = policy.retry_base_delay_seconds * (2 ** (attempt - 1))
            sleep_seconds *= 0.8 + random.random() * 0.4
            if deadline_epoch is not None:
                remaining = deadline_epoch - time.time()
                if remaining <= sleep_seconds:
                    raise ApiException(408, "AGENT_TIMEOUT", "Agent 执行超过整体时间预算") from exc
            time.sleep(sleep_seconds)

    assert last_exc is not None
    raise last_exc


_NON_RETRYABLE_JOB_CODES = {
    "CANCELLED",
    "USER_CANCELLED",
    "AGENT_TIMEOUT",
    "BUDGET_EXCEEDED",
    "REPEATED_CALLS",
    "INVALID_INPUT",
    "INVALID_TOOL_ARGUMENTS",
    "UNKNOWN_TOOL",
    "OPERATION_FORBIDDEN",
    "SOURCE_CHANGED",
    "VERIFICATION_FAILED",
    "INVALID_MODEL_OUTPUT",
    "APPROVAL_REQUIRED",
    "REQUESTER_NOT_ACTIVE",
}


def is_retryable_job_code(code: Optional[str]) -> bool:
    if not code:
        return True
    return str(code) not in _NON_RETRYABLE_JOB_CODES
