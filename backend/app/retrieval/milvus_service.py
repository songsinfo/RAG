import hashlib
import os
import logging
import threading
from typing import List, Dict, Any, Optional
from pymilvus import DataType, Function, FunctionType, AnnSearchRequest, RRFRanker, CollectionSchema, FieldSchema
from sqlalchemy import select, and_, or_, exists
from app.config import settings, is_test_env
from app.db import get_db_connection
from app.errors import ApiException
from app.models import chunk_table, index_build_table, document_table, kb_grant_table, document_grant_table
from app.retrieval.normalizer import normalize_terms
from app.retrieval.ranking import rrf_fuse
from app.retrieval.embedding import get_embeddings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "rag_chunks"


class MilvusHybridService:
    # In-memory index fallback for local unit tests without requiring a running Milvus container
    _mock_store: Dict[str, Dict[str, Any]] = {}
    _client = None
    _client_checked = False
    _client_lock = threading.Lock()

    @classmethod
    def get_client(cls):
        with cls._client_lock:
            return cls._connect_client()

    @classmethod
    def _connect_client(cls):
        if cls._client is None:
            if is_test_env() and not os.getenv("MILVUS_URI"):
                cls._client = None
                return None
            milvus_uri = os.getenv("MILVUS_URI") or f"http://{os.getenv('MILVUS_HOST', '127.0.0.1')}:{os.getenv('MILVUS_PORT', '19530')}"
            try:
                from pymilvus import MilvusClient
                client = MilvusClient(uri=milvus_uri, timeout=3.0)
                if not client.has_collection(COLLECTION_NAME):
                    fields = [
                        FieldSchema("id", DataType.VARCHAR, max_length=128, is_primary=True),
                        FieldSchema("tenant_id", DataType.VARCHAR, max_length=64),
                        FieldSchema("kb_id", DataType.VARCHAR, max_length=64),
                        FieldSchema("document_id", DataType.VARCHAR, max_length=64),
                        FieldSchema("version_id", DataType.VARCHAR, max_length=64),
                        FieldSchema("build_id", DataType.VARCHAR, max_length=64),
                        FieldSchema("index_epoch", DataType.INT64),
                        FieldSchema("chunk_id", DataType.VARCHAR, max_length=64),
                        FieldSchema("representation_type", DataType.VARCHAR, max_length=32),
                        FieldSchema("text_content", DataType.VARCHAR, max_length=65535, enable_analyzer=True),
                        FieldSchema("sparse_vector", DataType.SPARSE_FLOAT_VECTOR),
                        FieldSchema("dense_vector", DataType.FLOAT_VECTOR, dim=1024),
                    ]
                    bm25_fn = Function(
                        name="text_bm25_emb",
                        function_type=FunctionType.BM25,
                        input_field_names=["text_content"],
                        output_field_names=["sparse_vector"],
                    )
                    schema = CollectionSchema(fields=fields, functions=[bm25_fn], auto_id=False, enable_dynamic_field=True)
                    index_params = client.prepare_index_params()
                    index_params.add_index(
                        field_name="dense_vector",
                        index_type="HNSW",
                        metric_type="COSINE",
                        params={"M": 16, "efConstruction": 200},
                    )
                    index_params.add_index(
                        field_name="sparse_vector",
                        index_type="SPARSE_INVERTED_INDEX",
                        metric_type="BM25",
                    )
                    client.create_collection(
                        collection_name=COLLECTION_NAME,
                        schema=schema,
                        index_params=index_params,
                    )
                cls._client = client
                cls._client_checked = True
                logger.info("Connected to Milvus server at %s", milvus_uri)
            except Exception as e:
                if not is_test_env():
                    logger.error("Milvus connection failed at %s: %s", milvus_uri, e)
                else:
                    logger.warning("Milvus connection failed (%s); using in-memory mock store for test environment.", e)
                cls._client = None
        return cls._client

    @classmethod
    def upsert_chunks(
        cls,
        tenant_id: str,
        build_id: str,
        chunks: List[Dict[str, Any]],
        kb_id: str,
        doc_id: str,
        version_id: str,
        index_epoch: Optional[int] = None,
    ):
        if not chunks:
            return

        epoch = int(index_epoch or 0)
        batch_size = max(1, min(512, int(os.getenv("MILVUS_UPSERT_BATCH_SIZE", "128"))))
        client = cls.get_client()

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            texts = [c["text_content"] for c in batch]
            embeddings = get_embeddings(texts, dim=1024, batch_size=batch_size, tenant_id=tenant_id)
            records = []

            for chunk, emb in zip(batch, embeddings):
                chunk_id = str(chunk["id"])
                norm = normalize_terms(chunk["text_content"])
                if epoch > 0:
                    record_id = hashlib.sha256(
                        f"{tenant_id}:{build_id}:{epoch}:{chunk_id}".encode("utf-8")
                    ).hexdigest()
                else:
                    record_id = f"{tenant_id}:{build_id}:{chunk_id}"
                record = {
                    "id": record_id,
                    "parent_id": str(chunk.get("parent_id") or ""),
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "document_id": doc_id,
                    "version_id": version_id,
                    "build_id": build_id,
                    "index_epoch": epoch,
                    "chunk_id": chunk_id,
                    "representation_type": "ORIGINAL",
                    "text_content": chunk["text_content"],
                    "normalized_terms": norm,
                    "dense_vector": emb,
                }
                if is_test_env():
                    cls._mock_store[record_id] = record
                records.append(record)

            if client:
                try:
                    client.upsert(collection_name=COLLECTION_NAME, data=records)
                except Exception as exc:
                    logger.error("Failed to upsert to Milvus: %s", exc)
                    if not is_test_env():
                        raise ApiException(503, "MILVUS_UNAVAILABLE", f"Milvus 入库失败: {exc}")
            elif not is_test_env():
                raise ApiException(503, "MILVUS_UNAVAILABLE", "Milvus 服务不可用，文档入库中止")

    @classmethod
    def delete_by_build(
        cls,
        tenant_id: str,
        build_id: str,
        index_epoch: Optional[int] = None,
    ):
        epoch = int(index_epoch) if index_epoch is not None else None
        to_delete = [
            key
            for key, value in cls._mock_store.items()
            if value.get("tenant_id") == tenant_id
            and value.get("build_id") == build_id
            and (epoch is None or int(value.get("index_epoch") or 0) == epoch)
        ]
        for key in to_delete:
            cls._mock_store.pop(key, None)

        client = cls.get_client()
        if client:
            try:
                filter_expr = f'tenant_id == "{tenant_id}" and build_id == "{build_id}"'
                if epoch is not None:
                    filter_expr += f" and index_epoch == {epoch}"
                client.delete(
                    collection_name=COLLECTION_NAME,
                    filter=filter_expr,
                )
            except Exception as e:
                logger.warning("Failed to delete from Milvus by build: %s", e)
                if not is_test_env():
                    raise ApiException(503, "MILVUS_UNAVAILABLE", f"Milvus 删除失败: {e}")

    @classmethod
    def delete_by_document(cls, tenant_id: str, doc_id: str):
        to_delete = [k for k, v in cls._mock_store.items() if v.get("tenant_id") == tenant_id and v.get("document_id") == doc_id]
        for k in to_delete:
            cls._mock_store.pop(k, None)

        client = cls.get_client()
        if client:
            try:
                client.delete(
                    collection_name=COLLECTION_NAME,
                    filter=f'tenant_id == "{tenant_id}" and document_id == "{doc_id}"',
                )
            except Exception as e:
                logger.warning("Failed to delete from Milvus by document: %s", e)
                if not is_test_env():
                    raise ApiException(503, "MILVUS_UNAVAILABLE", f"Milvus 删除失败: {e}")

    @classmethod
    def search(
        cls,
        tenant_id: str,
        query: str,
        allowed_build_ids: List[str],
        top_k: Optional[int] = None,
        dense_top_k: Optional[int] = None,
        sparse_top_k: Optional[int] = None,
        fusion_top_k: Optional[int] = None,
        mode: str = "HYBRID",
        rrf_enabled: bool = True,
        rrf_k: int = 60,
        allowed_build_epochs: Optional[Dict[str, int]] = None,
    ) -> List[Dict[str, Any]]:
        if not allowed_build_ids:
            return []

        mode = str(mode or "HYBRID").upper()
        if mode == "SPARSE":
            mode = "BM25"
        if mode not in {"DENSE", "BM25", "HYBRID"}:
            raise ApiException(409, "INVALID_RETRIEVAL_CONFIG", f"不支持的检索模式: {mode}")
        if mode == "HYBRID" and not rrf_enabled:
            raise ApiException(409, "INVALID_RETRIEVAL_CONFIG", "HYBRID 模式需要 RRF 融合")

        if top_k is not None:
            if dense_top_k is None:
                dense_top_k = top_k * 2
            if sparse_top_k is None:
                sparse_top_k = top_k * 2
            if fusion_top_k is None:
                fusion_top_k = top_k
        dense_top_k = 0 if dense_top_k is None and mode == "BM25" else (dense_top_k if dense_top_k is not None else 30)
        sparse_top_k = 0 if sparse_top_k is None and mode == "DENSE" else (sparse_top_k if sparse_top_k is not None else 30)
        fusion_top_k = fusion_top_k or 20

        output_fields = ["kb_id", "document_id", "version_id", "build_id", "index_epoch", "chunk_id", "text_content"]

        def normalize_result(res):
            hits = []
            rows = res[0] if res and isinstance(res, list) and res and isinstance(res[0], list) else (res or [])
            for item in rows:
                entity = item.get("entity", {}) if isinstance(item, dict) else {}
                chunk_id = entity.get("chunk_id") or (item.get("id") if isinstance(item, dict) else None)
                hits.append({
                    "id": chunk_id,
                    "chunkId": chunk_id,
                    "kbId": entity.get("kb_id"),
                    "documentId": entity.get("document_id"),
                    "versionId": entity.get("version_id"),
                    "buildId": entity.get("build_id"),
                    "indexEpoch": int(entity.get("index_epoch") or 0),
                    "text": entity.get("text_content"),
                    "score": float(item.get("distance", item.get("score", 0.0)) or 0.0) if isinstance(item, dict) else 0.0,
                })
            return hits

        client = cls.get_client()
        if client:
            try:
                if allowed_build_epochs:
                    grouped: Dict[int, List[str]] = {}
                    legacy_builds: List[str] = []
                    for allowed_build_id in allowed_build_ids:
                        expected_epoch = int(allowed_build_epochs.get(allowed_build_id, 0) or 0)
                        if expected_epoch > 0:
                            grouped.setdefault(expected_epoch, []).append(allowed_build_id)
                        else:
                            legacy_builds.append(allowed_build_id)

                    build_clauses = []
                    for epoch, build_ids in grouped.items():
                        ids_expr = ", ".join(f'"{build_id}"' for build_id in build_ids)
                        build_clauses.append(
                            f'(index_epoch == {epoch} and build_id in [{ids_expr}])'
                        )
                    if legacy_builds:
                        ids_expr = ", ".join(f'"{build_id}"' for build_id in legacy_builds)
                        build_clauses.append(f'build_id in [{ids_expr}]')

                    build_filter = " or ".join(build_clauses)
                    filter_expr = (
                        f'tenant_id == "{tenant_id}" and ({build_filter}) '
                        'and representation_type == "ORIGINAL"'
                    )
                else:
                    build_ids_str = ", ".join(f'"{b}"' for b in allowed_build_ids)
                    filter_expr = (
                        f'tenant_id == "{tenant_id}" and build_id in [{build_ids_str}] '
                        'and representation_type == "ORIGINAL"'
                    )

                if mode == "DENSE":
                    query_emb = get_embeddings([query], dim=1024, tenant_id=tenant_id)[0]
                    res = client.search(
                        collection_name=COLLECTION_NAME,
                        data=[query_emb],
                        anns_field="dense_vector",
                        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
                        limit=max(1, dense_top_k),
                        filter=filter_expr,
                        output_fields=output_fields,
                    )
                    return normalize_result(res)[:fusion_top_k]

                if mode == "BM25":
                    res = client.search(
                        collection_name=COLLECTION_NAME,
                        data=[query],
                        anns_field="sparse_vector",
                        search_params={"metric_type": "BM25"},
                        limit=max(1, sparse_top_k),
                        filter=filter_expr,
                        output_fields=output_fields,
                    )
                    return normalize_result(res)[:fusion_top_k]

                query_emb = get_embeddings([query], dim=1024, tenant_id=tenant_id)[0]
                dense_req = AnnSearchRequest(
                    data=[query_emb],
                    anns_field="dense_vector",
                    param={"metric_type": "COSINE", "params": {"ef": 64}},
                    limit=max(1, dense_top_k),
                    expr=filter_expr,
                )
                sparse_req = AnnSearchRequest(
                    data=[query],
                    anns_field="sparse_vector",
                    param={"metric_type": "BM25"},
                    limit=max(1, sparse_top_k),
                    expr=filter_expr,
                )
                res = client.hybrid_search(
                    collection_name=COLLECTION_NAME,
                    reqs=[dense_req, sparse_req],
                    ranker=RRFRanker(k=rrf_k),
                    limit=fusion_top_k,
                    output_fields=output_fields,
                )
                return normalize_result(res)
            except ApiException:
                raise
            except Exception as e:
                logger.error("Milvus search error: %s", e)
                if not is_test_env():
                    raise ApiException(503, "MILVUS_UNAVAILABLE", f"Milvus 检索失败: {e}")

        if not is_test_env():
            raise ApiException(503, "MILVUS_UNAVAILABLE", "Milvus 检索服务不可用")

        bm25_hits: List[Dict[str, Any]] = []
        dense_hits: List[Dict[str, Any]] = []
        query_terms = [t.lower() for t in query.split() if t.strip()]

        def record_is_allowed(item: Dict[str, Any]) -> bool:
            build_id = str(item.get("build_id") or "")
            if item.get("tenant_id") != tenant_id or build_id not in allowed_build_ids:
                return False
            if allowed_build_epochs:
                expected_epoch = int(allowed_build_epochs.get(build_id, 0) or 0)
                if expected_epoch > 0 and int(item.get("index_epoch") or 0) != expected_epoch:
                    return False
            return True

        if mode in {"BM25", "HYBRID"}:
            for item in cls._mock_store.values():
                if not record_is_allowed(item):
                    continue
                if item["representation_type"] != "ORIGINAL":
                    continue
                text_lower = (item["text_content"] + " " + item["normalized_terms"]).lower()
                score = sum(1.0 for t in query_terms if t in text_lower)
                if score > 0:
                    bm25_hits.append({
                        "id": item["chunk_id"],
                        "kbId": item["kb_id"],
                        "documentId": item["document_id"],
                        "versionId": item["version_id"],
                        "buildId": item["build_id"],
                        "indexEpoch": int(item.get("index_epoch") or 0),
                        "chunkId": item["chunk_id"],
                        "text": item["text_content"],
                        "bm25_score": score,
                        "score": score,
                    })
            bm25_hits.sort(key=lambda x: x["bm25_score"], reverse=True)

        if mode in {"DENSE", "HYBRID"}:
            query_emb = get_embeddings([query], dim=1024)[0]
            for item in cls._mock_store.values():
                if not record_is_allowed(item):
                    continue
                if item["representation_type"] != "ORIGINAL":
                    continue
                v = item["dense_vector"]
                cos_sim = sum(a * b for a, b in zip(query_emb, v))
                dense_hits.append({
                    "id": item["chunk_id"],
                    "kbId": item["kb_id"],
                    "documentId": item["document_id"],
                    "versionId": item["version_id"],
                    "buildId": item["build_id"],
                    "indexEpoch": int(item.get("index_epoch") or 0),
                    "chunkId": item["chunk_id"],
                    "text": item["text_content"],
                    "dense_score": cos_sim,
                    "score": cos_sim,
                })
            dense_hits.sort(key=lambda x: x["dense_score"], reverse=True)

        if mode == "DENSE":
            return dense_hits[:min(dense_top_k, fusion_top_k)]
        if mode == "BM25":
            return bm25_hits[:min(sparse_top_k, fusion_top_k)]

        fused = rrf_fuse(
            [dense_hits[:dense_top_k], bm25_hits[:sparse_top_k]],
            weights=[0.5, 0.5],
            rrf_k=rrf_k,
        )
        return fused[:fusion_top_k]

    @classmethod
    def rebuild_from_mysql(cls, tenant_id: str):
        """Idempotently rebuilds Milvus index from active builds in MySQL."""
        with get_db_connection() as conn:
            stmt = (
                select(
                    document_table.c.kb_id,
                    document_table.c.id.label("document_id"),
                    document_table.c.active_build_id,
                    index_build_table.c.version_id,
                    index_build_table.c.fence,
                )
                .select_from(
                    document_table.join(
                        index_build_table,
                        and_(
                            index_build_table.c.tenant_id == document_table.c.tenant_id,
                            index_build_table.c.id == document_table.c.active_build_id,
                        ),
                    )
                )
                .where(
                    and_(
                        document_table.c.tenant_id == tenant_id,
                        document_table.c.status == "LIVE",
                        document_table.c.active_build_id.isnot(None),
                    )
                )
            )
            active_builds = conn.execute(stmt).fetchall()

            for b in active_builds:
                chunk_stmt = select(
                    chunk_table.c.id,
                    chunk_table.c.parent_id,
                    chunk_table.c.text_content,
                ).where(
                    and_(
                        chunk_table.c.tenant_id == tenant_id,
                        chunk_table.c.build_id == b.active_build_id,
                        chunk_table.c.kind == "CHILD",
                    )
                )
                chunks = conn.execute(chunk_stmt).fetchall()
                chunk_dicts = [
                    {
                        "id": str(c.id),
                        "parent_id": str(c.parent_id) if c.parent_id else None,
                        "text_content": c.text_content,
                    }
                    for c in chunks
                ]
                cls.upsert_chunks(
                    tenant_id=tenant_id,
                    build_id=str(b.active_build_id),
                    chunks=chunk_dicts,
                    kb_id=str(b.kb_id),
                    doc_id=str(b.document_id),
                    version_id=str(b.version_id),
                    index_epoch=int(b.fence or 0),
                )
