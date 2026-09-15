import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, TypedDict

from langgraph.graph import StateGraph, END
from sqlalchemy import select, and_, or_, exists

from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor
from app.models import (
    document_table,
    kb_grant_table,
    document_grant_table,
    context_segment_table,
    segment_kb_table,
    source_unit_table,
    run_source_table,
)
from app.qa.claim_validator import validate_claims, INSTRUCTION
from app.qa.model_gateway import ModelGateway
from app.retrieval.milvus_service import MilvusHybridService

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "在授权知识库中搜索相关切片",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "topK": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_section",
            "description": "读取特定文档的指定章节完整文本",
            "parameters": {
                "type": "object",
                "properties": {
                    "documentId": {"type": "string", "description": "文档ID"},
                    "buildId": {"type": "string", "description": "构建ID"},
                    "sectionId": {"type": "string", "description": "章节/源单元ID"},
                },
                "required": ["documentId", "buildId", "sectionId"],
            },
        },
    },
]


class AgentState(TypedDict):
    messages: List[Dict[str, Any]]
    round: int
    tool_calls_count: int
    repeated_calls: Dict[str, int]
    evidence: List[Dict[str, Any]]
    evidence_map: Dict[str, str]
    warnings: List[str]
    final_answer: Optional[Dict[str, Any]]
    error: Optional[str]


class LangGraphAgent:
    @classmethod
    def run(
        cls,
        actor: Actor,
        run_id: str,
        question: str,
        segment_id: str,
        record_event_fn,
        is_cancelled_fn,
    ) -> Dict[str, Any]:
        initial_messages = [
            {
                "role": "system",
                "content": INSTRUCTION + " 可使用 search_knowledge 和 read_section 读取资料。最多六轮。",
            },
            {"role": "user", "content": question},
        ]

        state: AgentState = {
            "messages": initial_messages,
            "round": 0,
            "tool_calls_count": 0,
            "repeated_calls": {},
            "evidence": [],
            "evidence_map": {},
            "warnings": [],
            "final_answer": None,
            "error": None,
        }

        # Build LangGraph StateGraph
        builder = StateGraph(AgentState)

        def model_step(s: AgentState) -> Dict[str, Any]:
            if is_cancelled_fn(actor.tenant_id, run_id):
                raise ApiException(409, "CANCELLED", "请求已取消")

            if s["round"] >= 6:
                raise ApiException(429, "BUDGET_EXCEEDED", "Agent 主循环次数已耗尽")

            record_event_fn(actor.tenant_id, run_id, "AGENT_MODEL", {"round": s["round"] + 1})

            reply = ModelGateway.chat_tools(
                tenant_id=actor.tenant_id,
                run_id=run_id,
                purpose="AGENT",
                messages=s["messages"],
                tools=AGENT_TOOLS,
                max_tokens=1200,
            )

            tool_calls = reply.get("tool_calls", [])
            new_messages = list(s["messages"])
            new_messages.append(reply)

            if not tool_calls:
                # LLM finished reasoning, validate final response
                content_str = reply.get("content", "")
                try:
                    parsed = json.loads(content_str)
                except Exception:
                    parsed = {"status": "NO_EVIDENCE", "claims": []}

                validated = validate_claims(parsed, s["evidence_map"])
                validated["evidence"] = s["evidence"]
                validated["warnings"] = s["warnings"]
                return {
                    "messages": new_messages,
                    "final_answer": validated,
                    "round": s["round"],
                }

            return {
                "messages": new_messages,
                "round": s["round"],
            }

        def tools_step(s: AgentState) -> Dict[str, Any]:
            if is_cancelled_fn(actor.tenant_id, run_id):
                raise ApiException(409, "CANCELLED", "请求已取消")

            last_msg = s["messages"][-1]
            tool_calls = last_msg.get("tool_calls", [])
            new_messages = list(s["messages"])
            evidence_list = list(s["evidence"])
            evidence_map = dict(s["evidence_map"])
            warnings_list = list(s["warnings"])
            repeated_map = dict(s["repeated_calls"])
            calls_count = s["tool_calls_count"]

            for tc in tool_calls:
                calls_count += 1
                if calls_count > 8:
                    raise ApiException(429, "BUDGET_EXCEEDED", "工具调用次数已耗尽")

                fn_data = tc.get("function", {})
                name = fn_data.get("name")
                raw_args = fn_data.get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}

                # Normalized signature check for 3-time repeated loop termination
                sig = f"{name}:{json.dumps(args, sort_keys=True)}"
                repeated_map[sig] = repeated_map.get(sig, 0) + 1
                if repeated_map[sig] >= 3:
                    raise ApiException(409, "REPEATED_CALLS", "重复工具调用已终止")

                # Dispatch tool execution
                try:
                    if name == "search_knowledge":
                        query = args.get("query")
                        top_k = args.get("topK", 6)
                        if not query or not isinstance(top_k, int) or top_k < 1 or top_k > 10:
                            raise ApiException(400, "INVALID_TOOL_ARGUMENTS", "检索工具参数无效")

                        # Resolve allowed builds in MySQL
                        allowed_builds = cls._resolve_allowed_builds(actor, segment_id)
                        hits = MilvusHybridService.search(
                            tenant_id=actor.tenant_id,
                            query=query,
                            allowed_build_ids=allowed_builds,
                            top_k=top_k,
                        )

                        tool_ev = []
                        now = datetime.now(timezone.utc)
                        with get_db_connection() as conn:
                            for h in hits:
                                eid = f"E{len(evidence_list) + 1}"
                                ev_item = {
                                    "evidenceId": eid,
                                    "chunkId": h["chunkId"],
                                    "documentId": h.get("documentId"),
                                    "text": h["text"],
                                    "score": round(h["score"], 6),
                                }
                                evidence_list.append(ev_item)
                                evidence_map[eid] = h["text"]
                                tool_ev.append(ev_item)

                                # Register in run_source table
                                conn.execute(
                                    run_source_table.insert().values(
                                        tenant_id=actor.tenant_id,
                                        id=str(uuid.uuid4()),
                                        run_id=run_id,
                                        document_id=h.get("documentId"),
                                        build_id=allowed_builds[0] if allowed_builds else "",
                                        unit_id=None,
                                        evidence_id=eid,
                                        purpose="TOOL",
                                        span_json={},
                                        payload_hash=hashlib.sha256(h["text"].encode("utf-8")).digest(),
                                        send_status="SENT",
                                        created_at=now,
                                        updated_at=now,
                                    )
                                )
                            conn.commit()

                        result_payload = {
                            "status": "OK" if tool_ev else "NO_EVIDENCE",
                            "evidence": tool_ev,
                        }

                    elif name == "read_section":
                        doc_id = args.get("documentId")
                        build_id = args.get("buildId")
                        section_id = args.get("sectionId")
                        if not doc_id or not build_id or not section_id:
                            raise ApiException(400, "INVALID_TOOL_ARGUMENTS", "章节工具参数无效")

                        ev_item = cls._read_section(actor, run_id, doc_id, build_id, section_id, len(evidence_list) + 1)
                        evidence_list.append(ev_item)
                        evidence_map[ev_item["evidenceId"]] = ev_item["text"]
                        result_payload = {"status": "OK", "evidence": [ev_item]}

                    else:
                        raise ApiException(400, "UNKNOWN_TOOL", "不支持此工具")

                except ApiException as e:
                    if e.code in {"INVALID_TOOL_ARGUMENTS", "UNKNOWN_TOOL", "INVALID_INPUT"}:
                        result_payload = {"status": "ERROR", "code": e.code}
                    else:
                        raise e

                record_event_fn(actor.tenant_id, run_id, "AGENT_TOOL", {"name": name, "callCount": calls_count})
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result_payload, ensure_ascii=False),
                    }
                )

            return {
                "messages": new_messages,
                "evidence": evidence_list,
                "evidence_map": evidence_map,
                "warnings": warnings_list,
                "repeated_calls": repeated_map,
                "tool_calls_count": calls_count,
                "round": s["round"] + 1,
            }

        def route_after_model(s: AgentState) -> str:
            if s.get("final_answer") or s.get("error"):
                return END
            return "tools"

        builder.add_node("model", model_step)
        builder.add_node("tools", tools_step)
        builder.set_entry_point("model")
        builder.add_conditional_edges("model", route_after_model, {"tools": "tools", END: END})
        builder.add_edge("tools", "model")

        graph = builder.compile()
        final_state = graph.invoke(state)

        if final_state.get("final_answer"):
            return final_state["final_answer"]

        # Default fallback
        return {
            "status": "COMPLETED",
            "claims": [],
            "conflicts": [],
            "answer": "当前探查完成。",
            "verification": "NOT_APPLICABLE",
            "evidence": final_state.get("evidence", []),
            "warnings": final_state.get("warnings", []),
        }

    @classmethod
    def _resolve_allowed_builds(cls, actor: Actor, segment_id: str) -> List[str]:
        with get_db_connection() as conn:
            seg = conn.execute(
                select(context_segment_table).where(
                    and_(
                        context_segment_table.c.tenant_id == actor.tenant_id,
                        context_segment_table.c.id == segment_id,
                    )
                )
            ).first()

            allowed_kbs = []
            if seg and seg.scope_mode == "SELECTED":
                kb_rows = conn.execute(
                    select(segment_kb_table.c.kb_id).where(
                        and_(
                            segment_kb_table.c.tenant_id == actor.tenant_id,
                            segment_kb_table.c.segment_id == segment_id,
                        )
                    )
                ).fetchall()
                allowed_kbs = [str(r.kb_id) for r in kb_rows]

            from app.identity.deps import get_readable_builds
            return get_readable_builds(conn, actor, allowed_kbs)

    @classmethod
    def _read_section(
        cls,
        actor: Actor,
        run_id: str,
        doc_id: str,
        build_id: str,
        section_id: str,
        ev_index: int,
    ) -> Dict[str, Any]:
        with get_db_connection() as conn:
            from app.identity.deps import can_read_document
            if not can_read_document(conn, actor, doc_id):
                raise ApiException(403, "OPERATION_FORBIDDEN", "无权访问此文档")

            unit = conn.execute(
                select(source_unit_table).where(
                    and_(
                        source_unit_table.c.tenant_id == actor.tenant_id,
                        source_unit_table.c.document_id == doc_id,
                        source_unit_table.c.build_id == build_id,
                        source_unit_table.c.id == section_id,
                    )
                )
            ).first()
            if not unit:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "章节单元不存在")

            text = unit.text_content or ""
            if len(text.encode("utf-8")) > 6000:
                raise ApiException(400, "INVALID_TOOL_ARGUMENTS", "章节超过预算，请先搜索具体问题")

            eid = f"E{ev_index}"
            now = datetime.now(timezone.utc)
            conn.execute(
                run_source_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    document_id=doc_id,
                    build_id=build_id,
                    unit_id=section_id,
                    evidence_id=eid,
                    purpose="TOOL",
                    span_json={},
                    payload_hash=hashlib.sha256(text.encode("utf-8")).digest(),
                    send_status="SENT",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        return {
            "evidenceId": eid,
            "documentId": doc_id,
            "buildId": build_id,
            "unitId": section_id,
            "text": text,
            "score": 1.0,
        }
