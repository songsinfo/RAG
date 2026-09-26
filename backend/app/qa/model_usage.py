"""Account for every provider attempt using the shared budget service."""
import json
import uuid

from app.errors import ApiException
from app.model_budget import BudgetService
from app.qa.runtime import AgentRuntimePolicy, get_run_usage


def token_ceiling(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256


def invoke_tracked(fn, *, tenant_id, run_id=None, job_id=None, purpose, alias,
                   payload, max_output_tokens=0, usage_reader=None):
    from app.qa.model_gateway import ModelGateway

    input_limit = token_ceiling(payload)
    output_limit = max(0, int(max_output_tokens))
    if run_id:
        policy = AgentRuntimePolicy.from_env()
        usage = get_run_usage(tenant_id, run_id)
        if usage["modelCalls"] >= policy.max_model_calls or usage["totalTokens"] + input_limit + output_limit > policy.max_total_tokens:
            raise ApiException(429, "BUDGET_EXCEEDED", "本次模型调用将超过调用或 Token 预算")
    call_id = str(uuid.uuid4())
    BudgetService.reserve(tenant_id, call_id, alias, input_limit, output_limit,
                          run_id=run_id, job_id=job_id)
    sent = False
    try:
        ModelGateway.assert_run_sources_current(tenant_id, run_id, allow_eval_judge=purpose == "JUDGE")
        ModelGateway.assert_job_current(tenant_id, job_id)
        sent = True
        result = fn()
        if getattr(result, "status_code", 200) >= 400:
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"模型网关返回状态码 {result.status_code}")
        usage = usage_reader(result) if usage_reader else {}
        reported = bool(usage and type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int
                        and usage["input_tokens"] >= 0 and usage["output_tokens"] >= 0)
        ModelGateway._record_call(
            tenant_id, call_id, run_id, purpose, "SUCCESS" if reported else "UNKNOWN",
            usage["input_tokens"] if reported else input_limit,
            usage["output_tokens"] if reported else output_limit, alias, job_id=job_id,
        )
        return result
    except Exception:
        ModelGateway._record_call(tenant_id, call_id, run_id, purpose,
            "FAILED" if sent else "NOT_SENT", input_limit if sent else 0,
            output_limit if sent else 0, alias, job_id=job_id)
        raise
