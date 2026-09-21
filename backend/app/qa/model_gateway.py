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

logger = logging.getLogger(__name__)

MODEL_CONFIG_IDS = {
    "chat-main": "cfg-model-chat-main",
    "embedding-main": "cfg-model-embedding-main",
    "judge-main": "cfg-model-judge-main",
}


class ModelGateway:
    @classmethod
    def chat(
        cls,
        tenant_id: str,
        run_id: str,
        purpose: str,  # "ANSWER", "JUDGE", "ROUTING"
        messages: List[Dict[str, str]],
        max_tokens: int = 2400,
        model_alias: str = "chat-main",
    ) -> str:
        gateway_url = os.getenv("LITELLM_BASE_URL")
        master_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")
        call_id = str(uuid.uuid4())
        start_time = time.time()
        now = datetime.now(timezone.utc)

        # Attempt to call LiteLLM Gateway via langchain_openai if configured
        if gateway_url:
            try:
                from langchain_openai import ChatOpenAI
                from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

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

                llm = ChatOpenAI(
                    model=model_alias,
                    openai_api_base=gateway_url if gateway_url.endswith("/v1") else f"{gateway_url}/v1",
                    openai_api_key=master_key,
                    max_retries=0,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    timeout=30.0,
                )
                response = llm.invoke(lc_messages)
                content = str(response.content)

                # Record in model_call table
                duration_ms = int((time.time() - start_time) * 1000)
                input_tokens = response.response_metadata.get("token_usage", {}).get("prompt_tokens", 50)
                output_tokens = response.response_metadata.get("token_usage", {}).get("completion_tokens", 50)

                cls._record_call(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    run_id=run_id,
                    purpose=purpose,
                    status="SUCCESS",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_alias=model_alias,
                )
                return content
            except ApiException:
                raise
            except Exception as e:
                # Failure終态 - record failed call and raise
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
        duration_ms = int((time.time() - start_time) * 1000)
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
    ) -> Dict[str, Any]:
        call_id = str(uuid.uuid4())
        start_time = time.time()
        gateway_url = os.getenv("LITELLM_BASE_URL")
        master_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")

        if cls._agent_mock_handler:
            result = cls._agent_mock_handler(messages)
            cls._record_call(tenant_id, call_id, run_id, purpose, "SUCCESS", 50, 50, model_alias)
            return result

        if gateway_url:
            try:
                from langchain_openai import ChatOpenAI
                from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

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
                        lc_messages.append(AIMessage(content=content, tool_calls=t_calls))

                llm = ChatOpenAI(
                    model=model_alias,
                    openai_api_base=gateway_url if gateway_url.endswith("/v1") else f"{gateway_url}/v1",
                    openai_api_key=master_key,
                    max_retries=0,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    timeout=30.0,
                ).bind_tools(tools)

                response = llm.invoke(lc_messages)
                tool_calls = getattr(response, "tool_calls", [])
                formatted_calls = [
                    {
                        "id": tc.get("id", f"call_{i}"),
                        "type": "function",
                        "function": {
                            "name": tc.get("name"),
                            "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False) if isinstance(tc.get("args"), dict) else str(tc.get("args", "{}")),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ]
                cls._record_call(tenant_id, call_id, run_id, purpose, "SUCCESS", 50, 50, model_alias)
                return {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": formatted_calls,
                }
            except ApiException:
                raise
            except Exception as e:
                cls._record_call(tenant_id, call_id, run_id, purpose, "FAILED", 0, 0, model_alias)
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
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": json.dumps({"query": user_msg, "topK": 5}, ensure_ascii=False),
                        },
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
    def _record_call(
        cls,
        tenant_id: str,
        call_id: str,
        run_id: str,
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
                        settled_amount="0.000000",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        provider_request_id=f"req-{call_id[:8]}",
                        pricing_snapshot={"rate": "fixed"},
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
