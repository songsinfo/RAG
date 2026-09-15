import os
import logging
from typing import List, Dict, Any, Optional
from pymilvus import DataType
from sqlalchemy import select, and_, or_, exists
from app.db import get_db_connection
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

    @classmethod
    def get_client(cls):
        if not cls._client_checked:
            cls._client_checked = True
            milvus_uri = os.getenv("MILVUS_URI")
            if not milvus_uri:
                cls._client = None
                return None
            try:
                from pymilvus import MilvusClient
                client = MilvusClient(uri=milvus_uri, timeout=1.0)
                if not client.has_collection(COLLECTION_NAME):
                    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
                    schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=128, is_primary=True)
                    schema.add_field(field_name="tenant_id", datatype=DataType.VARCHAR, max_length=64)
                    schema.add_field(field_name="kb_id", datatype=DataType.VARCHAR, max_length=64)
                    schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
                    schema.add_field(field_name="version_id", datatype=DataType.VARCHAR, max_length=64)
                    schema.add_field(field_name="build_id", datatype=DataType.VARCHAR, max_length=64)
                    schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, max_length=64)
                    schema.add_field(field_name="representation_type", datatype=DataType.VARCHAR, max_length=32)
                    schema.add_field(field_name="text_content", datatype=DataType.VARCHAR, max_length=65535)
                    schema.add_field(field_name="normalized_terms", datatype=DataType.VARCHAR, max_length=65535)
                    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=1024)

                    index_params = client.prepare_index_params()
                    index_params.add_index(field_name="dense_vector", index_type="HNSW", metric_type="COSINE", params={"M": 16, "efConstruction": 200})
                    client.create_collection(collection_name=COLLECTION_NAME, schema=schema, index_params=index_params)
                cls._client = client
                logger.info("Connected to Milvus server at %s", milvus_uri)
            except Exception as e:
                logger.warning("Milvus connection failed (%s); using in-memory hybrid retrieval fallback.", e)
                cls._client = None
        return cls._client

    @classmethod
    def upsert_chunks(cls, tenant_id: str, build_id: str, chunks: List[Dict[str, Any]], kb_id: str, doc_id: str, version_id: str):
        if not chunks:
            return

        texts = [c["text_content"] for c in chunks]
        embeddings = get_embeddings(texts, dim=1024)

        milvus_records = []
        for chunk, emb in zip(chunks, embeddings):
            chunk_id = str(chunk["id"])
            norm = normalize_terms(chunk["text_content"])
            record = {
                "id": f"{tenant_id}:{build_id}:{chunk_id}",
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "document_id": doc_id,
                "version_id": version_id,
                "build_id": build_id,
                "chunk_id": chunk_id,
                "representation_type": "ORIGINAL",
                "text_content": chunk["text_content"],
                "normalized_terms": norm,
                "dense_vector": emb,
            }
            cls._mock_store[record["id"]] = record
            milvus_records.append(record)

        client = cls.get_client()
        if client:
            try:
                client.upsert(collection_name=COLLECTION_NAME, data=milvus_records)
            except Exception as e:
                logger.warning("Failed to upsert to Milvus: %s", e)

    @classmethod
    def delete_by_build(cls, tenant_id: str, build_id: str):
        prefix = f"{tenant_id}:{build_id}:"
        to_delete = [k for k in cls._mock_store.keys() if k.startswith(prefix)]
        for k in to_delete:
            cls._mock_store.pop(k, None)

        client = cls.get_client()
        if client:
            try:
                client.delete(
                    collection_name=COLLECTION_NAME,
                    filter=f'tenant_id == "{tenant_id}" and build_id == "{build_id}"',
                )
            except Exception as e:
                logger.warning("Failed to delete from Milvus by build: %s", e)

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

    @classmethod
    def search(
        cls,
        tenant_id: str,
        query: str,
        allowed_build_ids: List[str],
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        if not allowed_build_ids:
            return []

        # 1. BM25 Route: keyword matching across text_content and normalized_terms
        bm25_hits: List[Dict[str, Any]] = []
        query_terms = [t.lower() for t in query.split() if t.strip()]

        for item in cls._mock_store.values():
            if item["tenant_id"] != tenant_id or item["build_id"] not in allowed_build_ids:
                continue
            if item["representation_type"] != "ORIGINAL":
                continue

            text_lower = (item["text_content"] + " " + item["normalized_terms"]).lower()
            score = sum(1.0 for t in query_terms if t in text_lower)
            if score > 0:
                bm25_hits.append({
                    "id": item["chunk_id"],
                    "documentId": item["document_id"],
                    "chunkId": item["chunk_id"],
                    "text": item["text_content"],
                    "bm25_score": score,
                })

        bm25_hits.sort(key=lambda x: x["bm25_score"], reverse=True)

        # 2. Dense Route: cosine similarity on embeddings
        query_emb = get_embeddings([query], dim=1024)[0]
        dense_hits: List[Dict[str, Any]] = []

        for item in cls._mock_store.values():
            if item["tenant_id"] != tenant_id or item["build_id"] not in allowed_build_ids:
                continue
            if item["representation_type"] != "ORIGINAL":
                continue

            v = item["dense_vector"]
            cos_sim = sum(a * b for a, b in zip(query_emb, v))
            dense_hits.append({
                "id": item["chunk_id"],
                "documentId": item["document_id"],
                "chunkId": item["chunk_id"],
                "text": item["text_content"],
                "dense_score": cos_sim,
            })

        dense_hits.sort(key=lambda x: x["dense_score"], reverse=True)

        # 3. RRF Fusion
        fused = rrf_fuse([dense_hits[:top_k * 2], bm25_hits[:top_k * 2]], weights=[0.5, 0.5], rrf_k=60)
        return fused[:top_k]

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
                    chunk_table.c.text_content,
                ).where(
                    and_(
                        chunk_table.c.tenant_id == tenant_id,
                        chunk_table.c.build_id == b.active_build_id,
                        chunk_table.c.kind == "CHILD",
                    )
                )
                chunks = conn.execute(chunk_stmt).fetchall()
                chunk_dicts = [{"id": str(c.id), "text_content": c.text_content} for c in chunks]
                cls.upsert_chunks(
                    tenant_id=tenant_id,
                    build_id=str(b.active_build_id),
                    chunks=chunk_dicts,
                    kb_id=str(b.kb_id),
                    doc_id=str(b.document_id),
                    version_id=str(b.version_id),
                )
