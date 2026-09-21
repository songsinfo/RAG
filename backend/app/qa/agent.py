import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import and_, select

from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor, assert_hits_still_readable
from app.models import context_segment_table, run_source_table, segment_kb_table, source_unit_table
from app.qa.claim_validator import INSTRUCTION, validate_claims
from app.qa.model_gateway import ModelGateway
from app.retrieval.pipeline import RetrievalPipeline

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


@dataclass(frozen=True)
class AgentContext:
    actor: Actor
    run_id: str
    segment_id: str
    config_id: str
    record_event_fn: Callable[..., Any]
    is_cancelled_fn: Callable[[str, str], bool]


class LangGraphAgent:
    _graph = None
    _graph_lock = threading.Lock()

    @classmethod
    def _get_graph(cls):
        if cls._graph is not None:
            return cls._graph
        with cls._graph_lock:
            if cls._graph is not None:
                return cls._graph

            builder = StateGraph(AgentState, context_schema=AgentContext)
            builder.add_node("model", cls._model_step)
            builder.add_node("tools", cls._tools_step)
            builder.add_edge(START, "model")
            builder.add_conditional_edges(
                "model",
                cls._route_after_model,
                {"tools": "tools", END: END},
            )
            builder.add_edge("tools", "model")
            cls._graph = builder.compile()
            return cls._graph

    @classmethod
    def run(
        cls,
        actor: Actor,
        run_id: str,
        question: str,
        segment_id: str,
        record_event_fn,
        is_cancelled_fn,
        config_id: str,
    ) -> Dict[str, Any]:
        state: AgentState = {
            "messages": [
                {
                    "role": "system",
                    "content": INSTRUCTION + " 可使用 search_knowledge 和 read_section 读取资料。最多六轮。",
                },
                {"role": "user", "content": question},
            ],
            "round": 0,
            "tool_calls_count": 0,
            "repeated_calls": {},
            "evidence": [],
            "evidence_map": {},
            "warnings": [],
            "final_answer": None,
            "error": None,
        }
        context = AgentContext(
            actor=actor,
            run_id=run_id,
            segment_id=segment_id,
            config_id=config_id,
            record_event_fn=record_event_fn,
            is_cancelled_fn=is_cancelled_fn,
        )
        final_state = cls._get_graph().invoke(state, context=context)

        if final_state.get("final_answer"):
            return final_state["final_answer"]
        return {
            "status": "COMPLETED",
            "claims": [],
            "conflicts": [],
            "answer": "当前探查完成。",
            "verification": "NOT_APPLICABLE",
            "evidence": final_state.get("evidence", []),
            "warnings": final_state.get("warnings", []),
        }

    @staticmethod
    def _route_after_model(state: AgentState) -> str:
        if state.get("final_answer") or state.get("error"):
            return END
        return "tools"

    @classmethod
    def _model_step(cls, state: AgentState, runtime: Runtime[AgentContext]) -> Dict[str, Any]:
        ctx = runtime.context
        actor = ctx.actor
        if ctx.is_cancelled_fn(actor.tenant_id, ctx.run_id):
            raise ApiException(409, "CANCELLED", "请求已取消")
        if state["round"] >= 6:
            raise ApiException(429, "BUDGET_EXCEEDED", "Agent 主循环次数已耗尽")

        ctx.record_event_fn(
            actor.tenant_id,
            ctx.run_id,
            "AGENT_MODEL",
            {"round": state["round"] + 1},
        )
        with get_db_connection() as conn:
            assert_hits_still_readable(conn, actor, state["evidence"])

        reply = ModelGateway.chat_tools(
            tenant_id=actor.tenant_id,
            run_id=ctx.run_id,
            purpose="AGENT",
            messages=state["messages"],
            tools=AGENT_TOOLS,
            max_tokens=1200,
        )
        new_messages = list(state["messages"])
        new_messages.append(reply)
        tool_calls = reply.get("tool_calls", [])

        if not tool_calls:
            try:
                parsed = json.loads(reply.get("content", ""))
            except Exception:
                parsed = {"status": "NO_EVIDENCE", "claims": []}
            validated = validate_claims(parsed, state["evidence_map"])
            validated["evidence"] = state["evidence"]
            validated["warnings"] = state["warnings"]
            return {
                "messages": new_messages,
                "final_answer": validated,
                "round": state["round"],
            }

        return {"messages": new_messages, "round": state["round"]}

    @classmethod
    def _tools_step(cls, state: AgentState, runtime: Runtime[AgentContext]) -> Dict[str, Any]:
        ctx = runtime.context
        actor = ctx.actor
        if ctx.is_cancelled_fn(actor.tenant_id, ctx.run_id):
            raise ApiException(409, "CANCELLED", "请求已取消")

        last_msg = state["messages"][-1]
        tool_calls = last_msg.get("tool_calls", [])
        new_messages = list(state["messages"])
        evidence_list = list(state["evidence"])
        evidence_map = dict(state["evidence_map"])
        warnings_list = list(state["warnings"])
        repeated_map = dict(state["repeated_calls"])
        calls_count = state["tool_calls_count"]

        evidence_by_key = {
            cls._evidence_key(ev): ev
            for ev in evidence_list
            if cls._evidence_key(ev)
        }

        for tc in tool_calls:
            calls_count += 1
            if calls_count > 8:
                raise ApiException(429, "BUDGET_EXCEEDED", "工具调用次数已耗尽")

            name, args = cls._tool_call_parts(tc)
            sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            repeated_map[sig] = repeated_map.get(sig, 0) + 1
            if repeated_map[sig] >= 3:
                raise ApiException(409, "REPEATED_CALLS", "重复工具调用已终止")

            try:
                if name == "search_knowledge":
                    query = args.get("query")
                    top_k = args.get("topK", args.get("top_k", 6))
                    if not query or not isinstance(top_k, int) or top_k < 1 or top_k > 10:
                        raise ApiException(400, "INVALID_TOOL_ARGUMENTS", "检索工具参数无效")

                    allowed_kbs = cls._resolve_allowed_kbs(actor, ctx.segment_id)
                    retrieval = RetrievalPipeline.retrieve(
                        actor=actor,
                        query=query,
                        kb_ids=allowed_kbs or None,
                        config_id=ctx.config_id,
                        run_id=ctx.run_id,
                        limit_override=top_k,
                    )
                    warnings_list.extend(retrieval["warnings"])
                    tool_ev = []
                    now = datetime.now(timezone.utc)

                    with get_db_connection() as conn:
                        for hit in retrieval["hits"]:
                            key = cls._hit_key(hit)
                            existing = evidence_by_key.get(key)
                            if existing:
                                tool_ev.append(cls._compact_evidence(existing))
                                continue

                            eid = f"E{len(evidence_list) + 1}"
                            source_units = hit.get("sourceUnits") or []
                            unit_id = source_units[0].get("unitId") if source_units else None
                            ev_item = {
                                "evidenceId": eid,
                                "kbId": hit.get("kbId"),
                                "documentId": hit.get("documentId"),
                                "versionId": hit.get("versionId"),
                                "buildId": hit.get("buildId"),
                                "indexEpoch": hit.get("indexEpoch"),
                                "chunkId": hit.get("chunkId"),
                                "parentChunkId": hit.get("parentChunkId"),
                                "unitId": unit_id,
                                "location": source_units[0].get("location") if source_units else {},
                                "matchedText": hit.get("matchedText"),
                                "text": hit["text"],
                                "score": round(float(hit.get("score", 0.0)), 6),
                                "rerankScore": hit.get("rerankScore"),
                            }
                            evidence_list.append(ev_item)
                            evidence_map[eid] = hit["text"]
                            evidence_by_key[key] = ev_item
                            tool_ev.append(cls._compact_evidence(ev_item))

                            conn.execute(
                                run_source_table.insert().values(
                                    tenant_id=actor.tenant_id,
                                    id=str(uuid.uuid4()),
                                    run_id=ctx.run_id,
                                    document_id=hit.get("documentId"),
                                    build_id=hit.get("buildId"),
                                    unit_id=unit_id,
                                    evidence_id=eid,
                                    purpose="TOOL",
                                    span_json={
                                        "chunkId": hit.get("chunkId"),
                                        "parentChunkId": hit.get("parentChunkId"),
                                        "sourceUnits": source_units,
                                    },
                                    payload_hash=hashlib.sha256(hit["text"].encode("utf-8")).digest(),
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
                    doc_id = args.get("documentId") or args.get("document_id")
                    build_id = args.get("buildId") or args.get("build_id")
                    section_id = args.get("sectionId") or args.get("section_id")
                    if not doc_id or not build_id or not section_id:
                        raise ApiException(400, "INVALID_TOOL_ARGUMENTS", "章节工具参数无效")

                    key = f"unit:{build_id}:{section_id}"
                    existing = evidence_by_key.get(key)
                    if existing:
                        result_payload = {"status": "OK", "evidence": [existing]}
                    else:
                        ev_item = cls._read_section(
                            actor,
                            ctx.run_id,
                            doc_id,
                            build_id,
                            section_id,
                            len(evidence_list) + 1,
                        )
                        evidence_list.append(ev_item)
                        evidence_map[ev_item["evidenceId"]] = ev_item["text"]
                        evidence_by_key[key] = ev_item
                        # read_section is an explicit deep-read action, so its
                        # bounded (<= 6000 bytes) text is intentionally returned.
                        result_payload = {"status": "OK", "evidence": [ev_item]}
                else:
                    raise ApiException(400, "UNKNOWN_TOOL", "不支持此工具")

            except ApiException as exc:
                if exc.code in {"INVALID_TOOL_ARGUMENTS", "UNKNOWN_TOOL", "INVALID_INPUT"}:
                    result_payload = {"status": "ERROR", "code": exc.code}
                else:
                    raise

            ctx.record_event_fn(
                actor.tenant_id,
                ctx.run_id,
                "AGENT_TOOL",
                {"name": name, "callCount": calls_count},
            )
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
            "round": state["round"] + 1,
        }

    @staticmethod
    def _tool_call_parts(tool_call: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        # LangChain v1 normalized ToolCall: {name, args, id, type=tool_call}.
        if tool_call.get("name"):
            args = tool_call.get("args") or {}
            return str(tool_call.get("name")), args if isinstance(args, dict) else {}

        # Keep compatibility with existing deterministic tests / raw OpenAI shape.
        fn_data = tool_call.get("function") or {}
        name = str(fn_data.get("name") or "")
        raw_args = fn_data.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {}
        return name, args if isinstance(args, dict) else {}

    @staticmethod
    def _hit_key(hit: Dict[str, Any]) -> str:
        build_id = str(hit.get("buildId") or "")
        context_id = str(hit.get("parentChunkId") or hit.get("chunkId") or "")
        return f"chunk:{build_id}:{context_id}" if build_id and context_id else ""

    @staticmethod
    def _evidence_key(evidence: Dict[str, Any]) -> str:
        if evidence.get("unitId") and not evidence.get("chunkId"):
            return f"unit:{evidence.get('buildId')}:{evidence.get('unitId')}"
        build_id = str(evidence.get("buildId") or "")
        context_id = str(evidence.get("parentChunkId") or evidence.get("chunkId") or "")
        return f"chunk:{build_id}:{context_id}" if build_id and context_id else ""

    @staticmethod
    def _compact_evidence(evidence: Dict[str, Any], max_chars: int = 800) -> Dict[str, Any]:
        relevant = str(evidence.get("matchedText") or evidence.get("text") or "")
        return {
            "evidenceId": evidence.get("evidenceId"),
            "documentId": evidence.get("documentId"),
            "buildId": evidence.get("buildId"),
            "unitId": evidence.get("unitId"),
            "location": evidence.get("location") or {},
            "excerpt": relevant[:max_chars],
            "truncated": len(relevant) > max_chars,
            "score": evidence.get("rerankScore")
            if evidence.get("rerankScore") is not None
            else evidence.get("score"),
        }

    @classmethod
    def _resolve_allowed_kbs(cls, actor: Actor, segment_id: str) -> List[str]:
        with get_db_connection() as conn:
            seg = conn.execute(
                select(context_segment_table).where(
                    and_(
                        context_segment_table.c.tenant_id == actor.tenant_id,
                        context_segment_table.c.id == segment_id,
                    )
                )
            ).first()
            if not seg or seg.scope_mode != "SELECTED":
                return []
            rows = conn.execute(
                select(segment_kb_table.c.kb_id).where(
                    and_(
                        segment_kb_table.c.tenant_id == actor.tenant_id,
                        segment_kb_table.c.segment_id == segment_id,
                    )
                )
            ).fetchall()
            return [str(row.kb_id) for row in rows]

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
