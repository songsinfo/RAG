import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from app.config import settings, is_test_env
from app.db import get_db_connection
from app.errors import ApiException
from app.models import model_call_table
from app.qa.model_clients import get_chat_model, get_http_client
from app.qa.runtime import AgentRuntimePolicy, retry_call
from app.qa.model_usage import invoke_tracked

logger = logging.getLogger(__name__)

MODEL_CONFIG_IDS = {
    "chat-main": "cfg-model-chat-main",
    "embedding-main": "cfg-model-embedding-main",
    "judge-main": "cfg-model-judge-main",
    "rerank-main": "cfg-model-rerank-main",
}


class ModelGateway:
    @staticmethod
    def _invoke(llm, messages, tenant_id, run_id, purpose, alias, max_tokens):
        return invoke_tracked(
            lambda: llm.invoke(messages), tenant_id=tenant_id, run_id=run_id,
            purpose=purpose, alias=alias, payload={"messages": [m.model_dump() for m in messages],
                                                "tools": getattr(llm, "kwargs", {}).get("tools", [])},
            max_output_tokens=max_tokens,
            usage_reader=lambda response: getattr(response, "usage_metadata", None) or {},
        )

    @classmethod
    def chat(
        cls,
        tenant_id: str,
        run_id: Optional[str],
        purpose: str,  # "ANSWER", "JUDGE", "ROUTING"
        messages: List[Dict[str, str]],
        max_tokens: int = 2400,
        model_alias: str = "chat-main",
        deadline_epoch: Optional[float] = None,
    ) -> str:
        gateway_url = os.getenv("LITELLM_BASE_URL")
        call_id = str(uuid.uuid4())

        # Attempt to call LiteLLM Gateway via langchain_openai if configured
        if gateway_url:
            try:
                from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

                lc_messages = []
                for m in messages:
                    role = m.get("role")
                    content = m.get("content", "")
                    if role == "system":
                        lc_messages.append(SystemMessage(content=content))
                    elif role == "user":
                        lc_messages.append(HumanMessage(content=content))
                    else:
                        lc_messages.append(AIMessage(content=content))

                timeout_seconds = 30.0
                if deadline_epoch is not None:
                    timeout_seconds = max(1.0, min(30.0, deadline_epoch - time.time()))
                    if deadline_epoch <= time.time():
                        raise ApiException(408, "AGENT_TIMEOUT", "Agent 执行超过整体时间预算")
                llm = get_chat_model(model_alias, max_tokens, timeout_seconds)
                if llm is None:
                    raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", "未配置大模型网关地址")

                policy = AgentRuntimePolicy.from_env()
                try:
                    response = retry_call(
                        f"llm:{model_alias}",
                        lambda: cls._invoke(llm, lc_messages, tenant_id, run_id, purpose, model_alias, max_tokens),
                        policy=policy,
                        deadline_epoch=deadline_epoch,
                    )
                except Exception as exc:
                    if isinstance(exc, ApiException) and exc.code in {"BUDGET_EXCEEDED", "BATCH_BUDGET_EXCEEDED", "CANCELLED", "MODEL_PRICING_NOT_CONFIGURED"}:
                        raise
                    fallback_alias = os.getenv("AGENT_FALLBACK_MODEL_ALIAS", "").strip()
                    if not fallback_alias or fallback_alias == model_alias:
                        raise
                    fallback = get_chat_model(fallback_alias, max_tokens, timeout_seconds)
                    if fallback is None:
                        raise
                    response = retry_call(
                        f"llm:{fallback_alias}",
                        lambda: cls._invoke(fallback, lc_messages, tenant_id, run_id, purpose, fallback_alias, max_tokens),
                        policy=policy,
                        deadline_epoch=deadline_epoch,
                    )
                content = str(response.content)

                return content
            except ApiException:
                raise
            except Exception as e:
                raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"大模型网关调用失败: {e}")

        if not is_test_env():
            cls._record_call(
                tenant_id=tenant_id,
                call_id=call_id,
                run_id=run_id,
                purpose=purpose,
                status="FAILED",
                input_tokens=0,
                output_tokens=0,
                model_alias=model_alias,
            )
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", "未配置大模型网关地址 LITELLM_BASE_URL")

        # Fallback for offline unit test environments
        cls._record_call(
            tenant_id=tenant_id,
            call_id=call_id,
            run_id=run_id,
            purpose=purpose,
            status="SUCCESS",
            input_tokens=30,
            output_tokens=30,
            model_alias=model_alias,
        )

        # Deterministic simulation based on prompt
        if purpose == "ANSWER":
            # Extract evidence if present
            user_msg = messages[-1].get("content", "")
            try:
                data = json.loads(user_msg)
                evidence = data.get("evidence", [])
            except Exception:
                evidence = []

            if not evidence:
                return json.dumps({
                    "status": "NO_EVIDENCE",
                    "claims": [],
                    "gaps": [],
                    "conflicts": [],
                })

            ev0 = evidence[0]
            eid = ev0.get("evidenceId", "E1")
            text = ev0.get("text", "")
            snippet_len = min(len(text), 20)
            return json.dumps({
                "status": "COMPLETED",
                "claims": [
                    {
                        "id": "c1",
                        "text": f"根据文档记载，{text[:snippet_len]}",
                        "evidence": [
                            {"evidenceId": eid, "start": 0, "end": snippet_len}
                        ],
                    }
                ],
                "gaps": [],
                "conflicts": [],
            })

        if purpose == "JUDGE":
            user_msg = messages[-1].get("content", "")
            try:
                data = json.loads(user_msg)
                claims = data.get("claims", [])
            except Exception:
                claims = []

            verdicts = [{"claimId": c.get("id"), "status": "SUPPORTED"} for c in claims]
            return json.dumps({"verdicts": verdicts})

        return json.dumps({"answer": "OK"})

    _agent_mock_handler = None  # Hook for test injection

    @classmethod
    def chat_tools(
        cls,
        tenant_id: str,
        run_id: str,
        purpose: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 1200,
        model_alias: str = "chat-main",
        deadline_epoch: Optional[float] = None,
    ) -> Dict[str, Any]:
        call_id = str(uuid.uuid4())
        gateway_url = os.getenv("LITELLM_BASE_URL")

        if cls._agent_mock_handler:
            result = cls._agent_mock_handler(messages)
            cls._record_call(tenant_id, call_id, run_id, purpose, "SUCCESS", 50, 50, model_alias)
            return result

        if gateway_url:
            try:
                from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

                lc_messages = []
                for m in messages:
                    role = m.get("role")
                    content = m.get("content", "")
                    if role == "system":
                        lc_messages.append(SystemMessage(content=content))
                    elif role == "user":
                        lc_messages.append(HumanMessage(content=content))
                    elif role == "tool":
                        lc_messages.append(ToolMessage(content=content, tool_call_id=m.get("tool_call_id", "")))
                    elif role == "assistant":
                        t_calls = m.get("tool_calls", [])
                        normalized_calls = []
                        for tc in t_calls:
                            if tc.get("name"):
                                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                                normalized_calls.append({
                                    "name": tc.get("name"),
                                    "args": args,
                                    "id": tc.get("id"),
                                    "type": "tool_call",
                                })
                            else:
                                fn = tc.get("function") or {}
                                raw_args = fn.get("arguments", "{}")
                                try:
                                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                                except Exception:
                                    args = {}
                                normalized_calls.append({
                                    "name": fn.get("name"),
                                    "args": args if isinstance(args, dict) else {},
                                    "id": tc.get("id"),
                                    "type": "tool_call",
                                })
                        lc_messages.append(AIMessage(content=content, tool_calls=normalized_calls))

                timeout_seconds = 30.0
                if deadline_epoch is not None:
                    timeout_seconds = max(1.0, min(30.0, deadline_epoch - time.time()))
                    if deadline_epoch <= time.time():
                        raise ApiException(408, "AGENT_TIMEOUT", "Agent 执行超过整体时间预算")
                base_model = get_chat_model(model_alias, max_tokens, timeout_seconds)
                if base_model is None:
                    raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", "未配置大模型网关地址")
                llm = base_model.bind_tools(tools, strict=True)

                policy = AgentRuntimePolicy.from_env()
                try:
                    response = retry_call(
                        f"llm-tools:{model_alias}",
                        lambda: cls._invoke(llm, lc_messages, tenant_id, run_id, purpose, model_alias, max_tokens),
                        policy=policy,
                        deadline_epoch=deadline_epoch,
                    )
                except Exception as exc:
                    if isinstance(exc, ApiException) and exc.code in {"BUDGET_EXCEEDED", "BATCH_BUDGET_EXCEEDED", "CANCELLED", "MODEL_PRICING_NOT_CONFIGURED"}:
                        raise
                    fallback_alias = os.getenv("AGENT_FALLBACK_MODEL_ALIAS", "").strip()
                    if not fallback_alias or fallback_alias == model_alias:
                        raise
                    fallback_model = get_chat_model(fallback_alias, max_tokens, timeout_seconds)
                    if fallback_model is None:
                        raise
                    fallback_bound = fallback_model.bind_tools(tools, strict=True)
                    response = retry_call(
                        f"llm-tools:{fallback_alias}",
                        lambda: cls._invoke(fallback_bound, lc_messages, tenant_id, run_id, purpose, fallback_alias, max_tokens),
                        policy=policy,
                        deadline_epoch=deadline_epoch,
                    )
                tool_calls = getattr(response, "tool_calls", [])
                formatted_calls = [
                    {
                        "id": tc.get("id", f"call_{i}"),
                        "name": tc.get("name"),
                        "args": tc.get("args", {}) if isinstance(tc.get("args"), dict) else {},
                        "type": "tool_call",
                    }
                    for i, tc in enumerate(tool_calls)
                ]
                return {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": formatted_calls,
                }
            except ApiException:
                raise
            except Exception as e:
                raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"Agent大模型网关调用失败: {e}")

        if not is_test_env():
            cls._record_call(tenant_id, call_id, run_id, purpose, "FAILED", 0, 0, model_alias)
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", "未配置大模型网关地址 LITELLM_BASE_URL")

        # Default deterministic mock: first round search, second round answer
        cls._record_call(tenant_id, call_id, run_id, purpose, "SUCCESS", 30, 30, model_alias)
        has_tool_response = any(m.get("role") == "tool" for m in messages)
        if not has_tool_response:
            user_msg = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_search_1",
                        "name": "search_knowledge",
                        "args": {"query": user_msg, "topK": 5},
                        "type": "tool_call",
                    }
                ],
            }
        else:
            return {
                "role": "assistant",
                "content": json.dumps({
                    "status": "COMPLETED",
                    "claims": [
                        {
                            "id": "c1",
                            "text": "经过多步研读，已确认系统技术规格",
                            "evidence": [{"evidenceId": "E1", "start": 0, "end": 10}],
                        }
                    ],
                    "gaps": [],
                    "conflicts": [],
                }, ensure_ascii=False),
                "tool_calls": [],
            }


    @classmethod
    def rerank(
        cls,
        tenant_id: str,
        run_id: Optional[str],
        query: str,
        documents: List[str],
        top_n: int,
        model_alias: str = "rerank-main",
    ) -> List[Dict[str, Any]]:
        if not documents:
            return []

        call_id = str(uuid.uuid4())
        gateway_url = os.getenv("LITELLM_BASE_URL") or getattr(settings, "LITELLM_BASE_URL", "")
        master_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")

        if gateway_url:
            base = gateway_url.rstrip("/")
            endpoint = f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
            try:
                response = invoke_tracked(lambda: get_http_client().post(
                    endpoint,
                    headers={"Authorization": f"Bearer {master_key}"},
                    json={
                        "model": model_alias,
                        "query": query,
                        "documents": documents,
                        "top_n": min(top_n, len(documents)),
                        "return_documents": False,
                    },
                    timeout=20.0,
                ), tenant_id=tenant_id, run_id=run_id, purpose="RERANK", alias=model_alias,
                    payload={"query": query, "documents": documents},
                    usage_reader=lambda response: {"input_tokens": response.json()["usage"]["total_tokens"], "output_tokens": 0}
                    if response.status_code == 200 and "total_tokens" in response.json().get("usage", {}) else {})
                if response.status_code != 200:
                    raise ApiException(
                        503,
                        "RERANK_UNAVAILABLE",
                        f"Reranker 网关返回异常状态码 {response.status_code}",
                    )
                payload = response.json()
                results = payload.get("results") or payload.get("data") or []
                normalized = []
                for item in results:
                    idx = item.get("index")
                    if not isinstance(idx, int):
                        continue
                    normalized.append(
                        {
                            "index": idx,
                            "relevance_score": float(
                                item.get("relevance_score", item.get("score", 0.0)) or 0.0
                            ),
                        }
                    )
                if not normalized:
                    raise ApiException(502, "INVALID_MODEL_OUTPUT", "Reranker 未返回有效结果")

                return normalized
            except ApiException:
                raise
            except Exception as exc:
                raise ApiException(503, "RERANK_UNAVAILABLE", f"Reranker 网关调用失败: {exc}")

        if not is_test_env():
            cls._record_call(
                tenant_id, call_id, run_id, "RERANK", "FAILED", 0, 0, model_alias
            )
            raise ApiException(503, "RERANK_UNAVAILABLE", "未配置可用的 Reranker 网关")

        # Deterministic local reranker for tests: lexical overlap, then original order.
        terms = [t.lower() for t in query.split() if t.strip()]
        scored = []
        for idx, document in enumerate(documents):
            lowered = document.lower()
            overlap = sum(1.0 for term in terms if term in lowered)
            scored.append(
                {
                    "index": idx,
                    "relevance_score": overlap + (1.0 / (1000.0 + idx)),
                }
            )
        scored.sort(key=lambda item: item["relevance_score"], reverse=True)
        cls._record_call(
            tenant_id=tenant_id,
            call_id=call_id,
            run_id=run_id,
            purpose="RERANK",
            status="SUCCESS",
            input_tokens=max(1, (len(query) + sum(len(d) for d in documents)) // 4),
            output_tokens=0,
            model_alias=model_alias,
        )
        return scored[: min(top_n, len(scored))]

    @classmethod
    def _record_call(
        cls,
        tenant_id: str,
        call_id: str,
        run_id: Optional[str],
        purpose: str,
        status: str,
        input_tokens: int,
        output_tokens: int,
        model_alias: str,
    ):
        try:
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                conn.execute(
                    model_call_table.insert().values(
                        tenant_id=tenant_id,
                        id=call_id,
                        run_id=run_id,
                        job_id=None,
                        purpose=purpose,
                        model_config_id=MODEL_CONFIG_IDS.get(model_alias, model_alias),
                        attempt_no=1,
                        status=status,
                        reserved_amount="0.000000",
                        settled_amount="0.000000" if is_test_env() else None,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        provider_request_id=f"req-{call_id[:8]}",
                        pricing_snapshot={"source": "TEST" if is_test_env() else "UNKNOWN"},
                        usage_detail={"model": model_alias},
                        reservation_authority="INTERNAL",
                        reservation_id=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()
        except Exception:
            logger.exception("Failed to persist model call %s", call_id)
