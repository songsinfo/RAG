import json
import math
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, select

from app.db import get_db_connection
from app.identity.deps import Actor, assert_hits_still_readable, get_readable_build_records
from app.models import chunk_span_table, chunk_table, source_unit_table
from app.retrieval.config import RetrievalConfig, load_retrieval_config
from app.retrieval.milvus_service import MilvusHybridService
from app.retrieval.reranker import rerank_hits
from app.retrieval.ranking import rrf_fuse
from app.retrieval.embedding import get_embeddings
from app.qa.model_gateway import ModelGateway
from app.errors import ApiException


class RetrievalPipeline:
    @classmethod
    def retrieve(
        cls,
        actor: Actor,
        query: str,
        *,
        kb_ids: Optional[List[str]] = None,
        document_ids: Optional[List[str]] = None,
        config_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit_override: Optional[int] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        cfg = load_retrieval_config(actor.tenant_id, config_id)

        with get_db_connection() as conn:
            readable = get_readable_build_records(conn, actor, kb_ids, document_ids)

        allowed_build_ids = [row["buildId"] for row in readable]
        allowed_build_epochs = {
            row["buildId"]: int(row.get("indexEpoch") or 0)
            for row in readable
        }
        if not allowed_build_ids:
            return {
                "hits": [],
                "warnings": [],
                "config": cfg.as_dict(),
                "allowedBuildCount": 0,
            }

        warnings: List[str] = []
        independent_query, query_variants, hyde_text = cls._prepare_queries(
            actor=actor,
            query=query,
            history=history or [],
            cfg=cfg,
            run_id=run_id,
            warnings=warnings,
        )

        ranked_lists: List[List[Dict[str, Any]]] = []
        for query_variant in query_variants:
            ranked_lists.append(
                MilvusHybridService.search(
                    tenant_id=actor.tenant_id,
                    query=query_variant,
                    allowed_build_ids=allowed_build_ids,
                    dense_top_k=cfg.dense_top_k,
                    sparse_top_k=cfg.sparse_top_k,
                    fusion_top_k=cfg.fusion_top_k,
                    mode=cfg.mode,
                    rrf_enabled=cfg.rrf_enabled,
                    rrf_k=cfg.rrf_k,
                    allowed_build_epochs=allowed_build_epochs,
                    representations=list(cfg.representations),
                )
            )
        if hyde_text:
            ranked_lists.append(
                MilvusHybridService.search(
                    tenant_id=actor.tenant_id,
                    query=hyde_text,
                    allowed_build_ids=allowed_build_ids,
                    dense_top_k=cfg.dense_top_k,
                    sparse_top_k=0,
                    fusion_top_k=cfg.fusion_top_k,
                    mode="DENSE",
                    rrf_enabled=False,
                    rrf_k=cfg.rrf_k,
                    allowed_build_epochs=allowed_build_epochs,
                    representations=list(cfg.representations),
                )
            )

        ranked_lists = [items for items in ranked_lists if items]
        if not ranked_lists:
            hits = []
        elif len(ranked_lists) == 1:
            hits = ranked_lists[0][:cfg.fusion_top_k]
        else:
            hits = rrf_fuse(ranked_lists, rrf_k=cfg.rrf_k)[:cfg.fusion_top_k]

        # Reranking is an external model boundary. Re-check every candidate
        # before any candidate text can leave the application process.
        with get_db_connection() as conn:
            assert_hits_still_readable(conn, actor, hits, kb_ids, document_ids)

        if cfg.rerank_enabled and hits:
            hits, rerank_warnings = rerank_hits(
                tenant_id=actor.tenant_id,
                run_id=run_id,
                query=independent_query,
                hits=hits,
                model_alias=cfg.rerank_model,
                top_k=cfg.rerank_top_k,
                fail_open=cfg.rerank_fail_open,
            )
            warnings.extend(rerank_warnings)

        if cfg.mmr_enabled and hits:
            hits = cls._mmr_select(independent_query, hits, cfg.final_top_k, cfg.mmr_lambda)

        if cfg.parent_expansion_enabled and hits:
            hits = cls._expand_parents(actor.tenant_id, hits, cfg)

        hits = cls._attach_provenance(actor.tenant_id, hits)
        final_limit = cfg.final_top_k
        if limit_override is not None:
            final_limit = max(1, min(50, int(limit_override)))
        hits = hits[:final_limit]

        with get_db_connection() as conn:
            assert_hits_still_readable(conn, actor, hits, kb_ids, document_ids)

        return {
            "hits": hits,
            "warnings": warnings,
            "config": cfg.as_dict(),
            "allowedBuildCount": len(allowed_build_ids),
        }

    @staticmethod
    def _prepare_queries(
        actor: Actor,
        query: str,
        history: List[Dict[str, str]],
        cfg: RetrievalConfig,
        run_id: Optional[str],
        warnings: List[str],
    ) -> Tuple[str, List[str], Optional[str]]:
        independent = query.strip()
        if cfg.rewrite_enabled and history:
            messages = [
                {
                    "role": "system",
                    "content": "结合历史对话把当前追问改写成不依赖上下文的独立检索问题。不得添加新事实。只输出 JSON {\"query\":\"...\"}。",
                },
                {
                    "role": "user",
                    "content": json.dumps({"history": history[-8:], "question": query}, ensure_ascii=False),
                },
            ]
            try:
                raw = ModelGateway.chat(
                    actor.tenant_id,
                    run_id,
                    "REWRITE",
                    messages,
                    max_tokens=300,
                    model_alias="chat-main",
                )
                parsed = json.loads(raw)
                rewritten = str(parsed.get("query", "")).strip()
                if not rewritten:
                    raise ValueError("empty rewritten query")
                independent = rewritten
            except Exception:
                if not cfg.allow_rewrite_fallback:
                    raise
                warnings.append("REWRITE_SKIPPED")

        variants = [independent]
        if cfg.multi_query_enabled:
            try:
                raw = ModelGateway.chat(
                    actor.tenant_id,
                    run_id,
                    "MULTI_QUERY",
                    [
                        {
                            "role": "system",
                            "content": "为检索生成最多两个不改变约束条件的等价问法。只输出 JSON {\"queries\":[\"...\"]}。",
                        },
                        {"role": "user", "content": independent},
                    ],
                    max_tokens=400,
                    model_alias="chat-main",
                )
                parsed = json.loads(raw)
                queries = parsed.get("queries", [])
                if isinstance(queries, list):
                    for item in queries[:2]:
                        value = str(item).strip()
                        if value and value not in variants:
                            variants.append(value)
            except Exception:
                warnings.append("MULTI_QUERY_SKIPPED")

        hyde_text: Optional[str] = None
        if cfg.hyde_enabled:
            try:
                hyde_text = ModelGateway.chat(
                    actor.tenant_id,
                    run_id,
                    "HYDE",
                    [
                        {
                            "role": "system",
                            "content": "生成一个不超过300 token的假想资料段落，只用于向量检索。不得声称其为事实。",
                        },
                        {"role": "user", "content": independent},
                    ],
                    max_tokens=300,
                    model_alias="chat-main",
                ).strip()
                if not hyde_text:
                    hyde_text = None
            except Exception:
                warnings.append("HYDE_SKIPPED")
                hyde_text = None

        return independent, variants, hyde_text

    @staticmethod
    def _mmr_select(query: str, hits: List[Dict[str, Any]], count: int, lambda_value: float) -> List[Dict[str, Any]]:
        if len(hits) <= 1:
            return hits[:count]
        texts = [str(hit.get("matchedText") or hit.get("text") or "") for hit in hits]
        vectors = get_embeddings([query] + texts, dim=1024)
        query_vector = vectors[0]
        doc_vectors = vectors[1:]

        def cosine(left: List[float], right: List[float]) -> float:
            dot = sum(a * b for a, b in zip(left, right))
            left_norm = math.sqrt(sum(a * a for a in left))
            right_norm = math.sqrt(sum(b * b for b in right))
            if left_norm == 0.0 or right_norm == 0.0:
                return 0.0
            return dot / (left_norm * right_norm)

        selected: List[int] = []
        remaining = set(range(len(hits)))
        target = min(max(1, count), len(hits))
        while remaining and len(selected) < target:
            best_index = None
            best_score = float("-inf")
            for idx in remaining:
                relevance = cosine(query_vector, doc_vectors[idx])
                redundancy = max(
                    (cosine(doc_vectors[idx], doc_vectors[chosen]) for chosen in selected),
                    default=0.0,
                )
                score = lambda_value * relevance - (1.0 - lambda_value) * redundancy
                if score > best_score:
                    best_score = score
                    best_index = idx
            if best_index is None:
                break
            selected.append(best_index)
            remaining.remove(best_index)

        return [hits[index] for index in selected]

    @staticmethod
    def _expand_parents(
        tenant_id: str,
        hits: List[Dict[str, Any]],
        cfg: RetrievalConfig,
    ) -> List[Dict[str, Any]]:
        child_ids = [str(h.get("chunkId")) for h in hits if h.get("chunkId")]
        if not child_ids:
            return hits

        with get_db_connection() as conn:
            child_rows = conn.execute(
                select(
                    chunk_table.c.id,
                    chunk_table.c.build_id,
                    chunk_table.c.parent_id,
                    chunk_table.c.text_content,
                ).where(
                    and_(
                        chunk_table.c.tenant_id == tenant_id,
                        chunk_table.c.id.in_(child_ids),
                        chunk_table.c.kind == "CHILD",
                    )
                )
            ).fetchall()
            child_map = {str(r.id): r for r in child_rows}
            parent_ids = [str(r.parent_id) for r in child_rows if r.parent_id]
            parent_rows = []
            if parent_ids:
                parent_rows = conn.execute(
                    select(
                        chunk_table.c.id,
                        chunk_table.c.build_id,
                        chunk_table.c.text_content,
                    ).where(
                        and_(
                            chunk_table.c.tenant_id == tenant_id,
                            chunk_table.c.id.in_(parent_ids),
                            chunk_table.c.kind == "PARENT",
                        )
                    )
                ).fetchall()

        parent_map = {str(r.id): r for r in parent_rows}
        expanded: List[Dict[str, Any]] = []
        seen_contexts = set()

        for hit in hits:
            item = dict(hit)
            child = child_map.get(str(hit.get("chunkId")))
            context_key: Tuple[str, str]
            if child and child.parent_id:
                parent = parent_map.get(str(child.parent_id))
                if parent and str(parent.build_id) == str(hit.get("buildId")):
                    parent_text = str(parent.text_content or "")
                    item["matchedText"] = str(hit.get("text") or "")
                    item["parentChunkId"] = str(parent.id)
                    item["text"] = parent_text[: cfg.parent_max_chars]
                    item["parentTruncated"] = len(parent_text) > cfg.parent_max_chars
                    context_key = (str(hit.get("buildId")), str(parent.id))
                else:
                    context_key = (str(hit.get("buildId")), str(hit.get("chunkId")))
            else:
                context_key = (str(hit.get("buildId")), str(hit.get("chunkId")))

            if context_key in seen_contexts:
                continue
            seen_contexts.add(context_key)
            expanded.append(item)

        return expanded

    @staticmethod
    def _attach_provenance(tenant_id: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        original_chunk_ids = [str(h.get("chunkId")) for h in hits if h.get("chunkId")]
        parent_ids = [str(h.get("parentChunkId")) for h in hits if h.get("parentChunkId")]
        if not original_chunk_ids and not parent_ids:
            return hits

        with get_db_connection() as conn:
            parent_children: Dict[str, List[str]] = {}
            child_ordinals: Dict[str, int] = {}
            if parent_ids:
                rows = conn.execute(
                    select(
                        chunk_table.c.id,
                        chunk_table.c.parent_id,
                        chunk_table.c.ordinal,
                    ).where(
                        and_(
                            chunk_table.c.tenant_id == tenant_id,
                            chunk_table.c.parent_id.in_(parent_ids),
                            chunk_table.c.kind == "CHILD",
                        )
                    ).order_by(chunk_table.c.ordinal.asc())
                ).fetchall()
                for row in rows:
                    cid = str(row.id)
                    pid = str(row.parent_id)
                    parent_children.setdefault(pid, []).append(cid)
                    child_ordinals[cid] = int(row.ordinal)

            all_chunk_ids = set(original_chunk_ids)
            for child_ids in parent_children.values():
                all_chunk_ids.update(child_ids)

            span_rows = []
            if all_chunk_ids:
                span_rows = conn.execute(
                    select(
                        chunk_span_table.c.chunk_id,
                        chunk_span_table.c.unit_id,
                        chunk_span_table.c.ordinal,
                        chunk_span_table.c.location,
                        chunk_span_table.c.start_cp,
                        chunk_span_table.c.end_cp,
                    ).where(
                        and_(
                            chunk_span_table.c.tenant_id == tenant_id,
                            chunk_span_table.c.chunk_id.in_(list(all_chunk_ids)),
                        )
                    )
                ).fetchall()

            unit_ids = list({str(row.unit_id) for row in span_rows})
            unit_rows = []
            if unit_ids:
                unit_rows = conn.execute(
                    select(
                        source_unit_table.c.id,
                        source_unit_table.c.text_content,
                        source_unit_table.c.location,
                    ).where(
                        and_(
                            source_unit_table.c.tenant_id == tenant_id,
                            source_unit_table.c.id.in_(unit_ids),
                        )
                    )
                ).fetchall()

        unit_map = {
            str(row.id): {
                "text": str(row.text_content or ""),
                "location": row.location or {},
            }
            for row in unit_rows
        }
        spans_by_chunk: Dict[str, List[Dict[str, Any]]] = {}
        for row in span_rows:
            spans_by_chunk.setdefault(str(row.chunk_id), []).append(
                {
                    "unitId": str(row.unit_id),
                    "spanOrdinal": int(row.ordinal),
                    "location": row.location or {},
                    "start": int(row.start_cp),
                    "end": int(row.end_cp),
                }
            )
        for values in spans_by_chunk.values():
            values.sort(key=lambda value: value["spanOrdinal"])

        output = []
        for hit in hits:
            item = dict(hit)
            parent_id = str(hit.get("parentChunkId") or "")
            if parent_id:
                provenance_chunks = parent_children.get(parent_id, [])
            else:
                provenance_chunks = [str(hit.get("chunkId"))] if hit.get("chunkId") else []

            provenance_chunks = sorted(
                provenance_chunks,
                key=lambda cid: child_ordinals.get(cid, 0),
            )
            source_units: List[Dict[str, Any]] = []
            seen_units = set()
            context_text = str(item.get("text") or "")
            cursor = 0

            for cid in provenance_chunks:
                for span in spans_by_chunk.get(cid, []):
                    unit_id = span["unitId"]
                    if unit_id in seen_units:
                        continue
                    seen_units.add(unit_id)
                    unit = unit_map.get(unit_id, {"text": "", "location": {}})
                    unit_text = unit["text"]
                    pos = context_text.find(unit_text, cursor) if unit_text else -1
                    if pos < 0 and unit_text:
                        pos = context_text.find(unit_text)
                    source = {
                        "unitId": unit_id,
                        "location": span["location"] or unit["location"],
                        "start": span["start"],
                        "end": span["end"],
                    }
                    if pos >= 0:
                        source["contextStart"] = pos
                        source["contextEnd"] = pos + len(unit_text)
                        source["contextMapped"] = True
                        cursor = source["contextEnd"]
                    else:
                        # Keep provenance even when the source unit was normalized,
                        # summarized or the parent context was truncated. In that
                        # case the exact parent offsets are unavailable, but the
                        # unit/location chain must not disappear.
                        source["contextMapped"] = False
                    source_units.append(source)

            item["sourceUnits"] = source_units
            output.append(item)
        return output
