import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import select, and_, or_, exists, update
from app.db import get_db_connection
from app.errors import ApiException
from app.identity.deps import Actor, can_read_document
from app.knowledge.artifacts import save_artifact, read_artifact, delete_artifact
from app.knowledge.parser import parse_document
from app.knowledge.chunker import chunk_units
from app.models import (
    knowledge_base_table,
    kb_grant_table,
    document_table,
    document_grant_table,
    artifact_table,
    document_version_table,
    config_revision_table,
    index_build_table,
    source_unit_table,
    chunk_table,
    chunk_span_table,
    representation_table,
    job_table,
    tenant_table,
    group_member_table,
    qa_run_table,
    conversation_table,
    run_event_table,
    run_source_table,
    feedback_table,
    audit_event_table,
    deletion_record_table,
)


# Bounded in-process executor for ingestion jobs
_executor = ThreadPoolExecutor(max_workers=4)


class KnowledgeService:
    @staticmethod
    def recover_unfinished_jobs():
        """Recovers any pending ingestion/purge jobs left over from process restart."""
        try:
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                conn.execute(
                    update(job_table)
                    .where(job_table.c.status.in_(["QUEUED", "RUNNING"]))
                    .values(
                        status="FAILED",
                        error_code="PROCESS_RESTARTED",
                        updated_at=now,
                    )
                )
                conn.commit()
        except Exception:
            pass

    @staticmethod
    def list_kbs(actor: Actor) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            stmt = select(
                knowledge_base_table.c.id,
                knowledge_base_table.c.name,
                knowledge_base_table.c.row_version.label("rowVersion"),
            ).where(
                and_(
                    knowledge_base_table.c.tenant_id == actor.tenant_id,
                    knowledge_base_table.c.status == "LIVE",
                )
            )
            rows = conn.execute(stmt).fetchall()
            return [{"id": str(r.id), "name": str(r.name), "rowVersion": int(r.rowVersion)} for r in rows]

    @staticmethod
    def create_kb(actor: Actor, name: str, readers: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
        if not name or len(name) > 255:
            raise ApiException(400, "INVALID_INPUT", "知识库名称无效")

        kb_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        with get_db_connection() as conn:
            conn.execute(
                knowledge_base_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=kb_id,
                    name=name,
                    status="LIVE",
                    row_version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            for r in readers:
                conn.execute(
                    kb_grant_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        kb_id=kb_id,
                        principal_type=r.get("type", "USER"),
                        user_id=r.get("userId"),
                        group_id=r.get("groupId"),
                        created_at=now,
                        updated_at=now,
                    )
                )
            conn.commit()

        return {"id": kb_id, "name": name, "rowVersion": 1}

    @staticmethod
    def get_kb_acl(actor: Actor, kb_id: str) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        with get_db_connection() as conn:
            kb = conn.execute(
                select(knowledge_base_table.c.row_version).where(
                    and_(
                        knowledge_base_table.c.tenant_id == actor.tenant_id,
                        knowledge_base_table.c.id == kb_id,
                    )
                )
            ).first()
            if not kb:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "知识库不存在")

            grants = conn.execute(
                select(kb_grant_table.c.principal_type, kb_grant_table.c.user_id, kb_grant_table.c.group_id)
                .where(
                    and_(
                        kb_grant_table.c.tenant_id == actor.tenant_id,
                        kb_grant_table.c.kb_id == kb_id,
                    )
                )
            ).fetchall()

            readers = [
                {"type": str(g.principal_type), "userId": str(g.user_id) if g.user_id else None, "groupId": str(g.group_id) if g.group_id else None}
                for g in grants
            ]
            return {"rowVersion": int(kb.row_version), "readers": readers}

    @staticmethod
    def update_kb_acl(actor: Actor, kb_id: str, expected_version: int, readers: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            kb = conn.execute(
                select(knowledge_base_table.c.row_version).where(
                    and_(
                        knowledge_base_table.c.tenant_id == actor.tenant_id,
                        knowledge_base_table.c.id == kb_id,
                    )
                )
            ).first()
            if not kb:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "知识库不存在")

            if int(kb.row_version) != expected_version:
                raise ApiException(409, "CONCURRENT_CONFLICT", "知识库版本冲突")

            new_version = expected_version + 1
            conn.execute(
                update(knowledge_base_table)
                .where(
                    and_(
                        knowledge_base_table.c.tenant_id == actor.tenant_id,
                        knowledge_base_table.c.id == kb_id,
                        knowledge_base_table.c.row_version == expected_version,
                    )
                )
                .values(row_version=new_version, updated_at=now)
            )

            # Re-create grants
            conn.execute(
                kb_grant_table.delete().where(
                    and_(
                        kb_grant_table.c.tenant_id == actor.tenant_id,
                        kb_grant_table.c.kb_id == kb_id,
                    )
                )
            )
            for r in readers:
                conn.execute(
                    kb_grant_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        kb_id=kb_id,
                        principal_type=r.get("type", "USER"),
                        user_id=r.get("userId"),
                        group_id=r.get("groupId"),
                        created_at=now,
                        updated_at=now,
                    )
                )
            # Bump security_epoch
            conn.execute(
                update(tenant_table)
                .where(tenant_table.c.id == actor.tenant_id)
                .values(security_epoch=tenant_table.c.security_epoch + 1)
            )

            # Audit event
            conn.execute(
                audit_event_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=str(uuid.uuid4()),
                    actor_id=actor.user_id,
                    action="ACL_UPDATE",
                    object_type="KB",
                    object_id=kb_id,
                    outcome="SUCCESS",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        return {"id": kb_id, "rowVersion": new_version, "readers": readers}

    @staticmethod
    def get_document_acl(actor: Actor, doc_id: str) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        with get_db_connection() as conn:
            doc = conn.execute(
                select(
                    document_table.c.id,
                    document_table.c.row_version,
                    document_table.c.acl_mode,
                ).where(
                    and_(
                        document_table.c.tenant_id == actor.tenant_id,
                        document_table.c.id == doc_id,
                    )
                )
            ).first()
            if not doc:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "文档不存在")

            grants = conn.execute(
                select(
                    document_grant_table.c.principal_type,
                    document_grant_table.c.user_id,
                    document_grant_table.c.group_id,
                ).where(
                    and_(
                        document_grant_table.c.tenant_id == actor.tenant_id,
                        document_grant_table.c.document_id == doc_id,
                    )
                )
            ).fetchall()

            readers = [
                {
                    "type": str(g.principal_type),
                    "userId": str(g.user_id) if g.user_id else None,
                    "groupId": str(g.group_id) if g.group_id else None,
                }
                for g in grants
            ]
            return {
                "id": str(doc.id),
                "rowVersion": int(doc.row_version),
                "mode": str(doc.acl_mode),
                "readers": readers,
            }

    @staticmethod
    def update_document_acl(
        actor: Actor,
        doc_id: str,
        expected_version: int,
        mode: str,
        readers: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")

        if mode not in ("INHERIT", "RESTRICT"):
            raise ApiException(400, "INVALID_INPUT", "ACL模式无效")

        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            doc = conn.execute(
                select(document_table.c.row_version).where(
                    and_(
                        document_table.c.tenant_id == actor.tenant_id,
                        document_table.c.id == doc_id,
                    )
                )
            ).first()
            if not doc:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "文档不存在")

            if int(doc.row_version) != expected_version:
                raise ApiException(409, "CONCURRENT_CONFLICT", "文档版本冲突")

            new_version = expected_version + 1
            conn.execute(
                update(document_table)
                .where(
                    and_(
                        document_table.c.tenant_id == actor.tenant_id,
                        document_table.c.id == doc_id,
                        document_table.c.row_version == expected_version,
                    )
                )
                .values(
                    row_version=new_version,
                    acl_mode=mode,
                    updated_at=now,
                )
            )

            # Re-create document grants
            conn.execute(
                document_grant_table.delete().where(
                    and_(
                        document_grant_table.c.tenant_id == actor.tenant_id,
                        document_grant_table.c.document_id == doc_id,
                    )
                )
            )
            for r in readers:
                ptype = (r.get("type") or "USER").upper()
                if ptype not in ("USER", "GROUP"):
                    raise ApiException(400, "INVALID_INPUT", "文档授权主体无效")
                conn.execute(
                    document_grant_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        document_id=doc_id,
                        principal_type=ptype,
                        user_id=r.get("userId"),
                        group_id=r.get("groupId"),
                        created_at=now,
                        updated_at=now,
                    )
                )

            # Bump security_epoch
            conn.execute(
                update(tenant_table)
                .where(tenant_table.c.id == actor.tenant_id)
                .values(security_epoch=tenant_table.c.security_epoch + 1)
            )

            # Audit event
            conn.execute(
                audit_event_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=str(uuid.uuid4()),
                    actor_id=actor.user_id,
                    action="ACL_UPDATE",
                    object_type="DOCUMENT",
                    object_id=doc_id,
                    outcome="SUCCESS",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        return {
            "id": doc_id,
            "rowVersion": new_version,
            "mode": mode,
            "readers": readers,
        }


    @staticmethod
    def list_documents(actor: Actor, kb_id: Optional[str] = None, admin_mode: bool = False, status: str = "LIVE") -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            stmt = select(
                document_table.c.id,
                document_table.c.kb_id.label("kbId"),
                document_table.c.title,
                document_table.c.status,
                document_table.c.row_version.label("rowVersion"),
                document_table.c.active_build_id.label("activeBuildId"),
            ).where(
                and_(
                    document_table.c.tenant_id == actor.tenant_id,
                    document_table.c.status == status,
                )
            )
            if kb_id:
                stmt = stmt.where(document_table.c.kb_id == kb_id)

            rows = conn.execute(stmt).fetchall()
            result = []
            for r in rows:
                if not admin_mode and not can_read_document(conn, actor, str(r.id)):
                    continue
                result.append({
                    "id": str(r.id),
                    "kbId": str(r.kbId),
                    "title": r.title,
                    "status": str(r.status),
                    "rowVersion": int(r.rowVersion),
                    "activeBuildId": str(r.activeBuildId) if r.activeBuildId else None,
                })
            return result

    @staticmethod
    def get_document(actor: Actor, doc_id: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            doc = conn.execute(
                select(
                    document_table.c.id,
                    document_table.c.kb_id.label("kbId"),
                    document_table.c.title,
                    document_table.c.status,
                    document_table.c.row_version.label("rowVersion"),
                    document_table.c.active_build_id.label("activeBuildId"),
                ).where(
                    and_(
                        document_table.c.tenant_id == actor.tenant_id,
                        document_table.c.id == doc_id,
                    )
                )
            ).first()

            if not doc:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "文档不存在")

            # ACL check for non-admin
            if not actor.admin and not can_read_document(conn, actor, str(doc.id)):
                raise ApiException(404, "RESOURCE_NOT_FOUND", "文档不存在")

            return {
                "id": str(doc.id),
                "kbId": str(doc.kbId),
                "title": doc.title,
                "status": str(doc.status),
                "rowVersion": int(doc.rowVersion),
                "activeBuildId": str(doc.activeBuildId) if doc.activeBuildId else None,
            }

    @staticmethod
    def upload_document(
        actor: Actor,
        kb_id: str,
        document_id: Optional[str],
        filename: str,
        file_bytes: bytes,
        idempotency_key: str,
        build_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
        if len(file_bytes) > 26214400:  # 25MB limit
            raise ApiException(413, "FILE_TOO_LARGE", "文件大小超过限制 (最大25MB)")
        if not idempotency_key or len(idempotency_key) > 80:
            raise ApiException(400, "INVALID_INPUT", "需要最多80字符的 Idempotency-Key")

        now = datetime.now(timezone.utc)
        scoped_idempotency = f"{actor.user_id}:{idempotency_key}"
        req_hash = hashlib.sha256(f"{kb_id}:{document_id}:{filename}:{len(file_bytes)}:{json.dumps(build_config, sort_keys=True)}".encode("utf-8")).digest()

        with get_db_connection() as conn:
            # 1. Check idempotency
            existing_job = conn.execute(
                select(job_table).where(
                    and_(
                        job_table.c.tenant_id == actor.tenant_id,
                        job_table.c.kind == "INGEST",
                        job_table.c.idempotency_key == scoped_idempotency,
                    )
                )
            ).first()
            if existing_job:
                if bytes(existing_job.request_hash) != req_hash:
                    raise ApiException(409, "IDEMPOTENCY_CONFLICT", "相同键对应不同上传")
                chk = existing_job.checkpoint or {}
                return {
                    "documentId": str(existing_job.target_id),
                    "versionId": chk.get("versionId"),
                    "jobId": str(existing_job.id),
                    "status": str(existing_job.status),
                }

            # 2. Check KB
            kb = conn.execute(
                select(knowledge_base_table.c.id).where(
                    and_(
                        knowledge_base_table.c.tenant_id == actor.tenant_id,
                        knowledge_base_table.c.id == kb_id,
                        knowledge_base_table.c.status == "LIVE",
                    )
                )
            ).first()
            if not kb:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "知识库不存在")

            # 3. Create or find Document
            doc_id = document_id or str(uuid.uuid4())
            if not document_id:
                conn.execute(
                    document_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=doc_id,
                        kb_id=kb_id,
                        title=filename,
                        acl_mode="RESTRICT",
                        status="LIVE",
                        active_build_id=None,
                        lifecycle_epoch=0,
                        row_version=1,
                        deleted_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                # Creator gets default grant
                conn.execute(
                    document_grant_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        document_id=doc_id,
                        principal_type="USER",
                        user_id=actor.user_id,
                        group_id=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing_doc = conn.execute(
                    select(document_table.c.id).where(
                        and_(
                            document_table.c.tenant_id == actor.tenant_id,
                            document_table.c.id == doc_id,
                        )
                    )
                ).first()
                if not existing_doc:
                    raise ApiException(404, "RESOURCE_NOT_FOUND", "文档不存在")

            # 4. Save Artifact
            storage_key, sha256_bytes, size_bytes = save_artifact(actor.tenant_id, filename, file_bytes)
            art_id = str(uuid.uuid4())
            conn.execute(
                artifact_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=art_id,
                    document_id=doc_id,
                    kind="ORIGINAL_FILE",
                    storage_key=storage_key,
                    sha256=sha256_bytes,
                    size_bytes=size_bytes,
                    status="READY",
                    created_at=now,
                    updated_at=now,
                )
            )

            # 5. Create Version
            version_id = str(uuid.uuid4())
            max_v = conn.execute(
                select(document_version_table.c.version_no)
                .where(
                    and_(
                        document_version_table.c.tenant_id == actor.tenant_id,
                        document_version_table.c.document_id == doc_id,
                    )
                )
                .order_by(document_version_table.c.version_no.desc())
            ).scalar() or 0
            v_no = max_v + 1

            conn.execute(
                document_version_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=version_id,
                    document_id=doc_id,
                    version_no=v_no,
                    source_artifact_id=art_id,
                    source_hash=sha256_bytes,
                    original_name=filename,
                    media_type="text/markdown",
                    created_by=actor.user_id,
                    created_at=now,
                    updated_at=now,
                )
            )

            # 6. Create Config Revision & Index Build
            cfg_id = str(uuid.uuid4())
            cfg_hash = hashlib.sha256(json.dumps(build_config, sort_keys=True).encode("utf-8")).digest()
            conn.execute(
                config_revision_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=cfg_id,
                    kind="BUILD",
                    name=f"build-cfg-{v_no}",
                    revision=v_no,
                    sha256=cfg_hash,
                    payload=build_config,
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                )
            )

            build_id = str(uuid.uuid4())
            conn.execute(
                index_build_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=build_id,
                    document_id=doc_id,
                    version_id=version_id,
                    build_no=v_no,
                    config_id=cfg_id,
                    status="QUEUED",
                    capabilities={"embedding": bool(build_config.get("embedding", False))},
                    fence=0,
                    published_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

            # 7. Create Job
            job_id = str(uuid.uuid4())
            checkpoint = {
                "documentId": doc_id,
                "versionId": version_id,
                "buildId": build_id,
                "filename": filename,
                "storageKey": storage_key,
            }
            conn.execute(
                job_table.insert().values(
                    tenant_id=actor.tenant_id,
                    id=job_id,
                    kind="INGEST",
                    target_id=doc_id,
                    requested_by=actor.user_id,
                    idempotency_key=scoped_idempotency,
                    request_hash=req_hash,
                    status="QUEUED",
                    stage="PARSING",
                    attempt=0,
                    available_at=now,
                    checkpoint=checkpoint,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.commit()

        # Submit background task
        _executor.submit(
            KnowledgeService._execute_ingestion_job,
            actor.tenant_id,
            job_id,
            doc_id,
            version_id,
            build_id,
            file_bytes,
            filename,
        )

        return {
            "documentId": doc_id,
            "versionId": version_id,
            "buildId": build_id,
            "jobId": job_id,
            "status": "QUEUED",
        }

    @staticmethod
    def _execute_ingestion_job(
        tenant_id: str,
        job_id: str,
        doc_id: str,
        version_id: str,
        build_id: str,
        file_bytes: bytes,
        filename: str,
    ):
        now = datetime.now(timezone.utc)
        try:
            with get_db_connection() as conn:
                conn.execute(
                    update(job_table)
                    .where(job_table.c.id == job_id)
                    .values(status="RUNNING", stage="PARSING", updated_at=now)
                )
                conn.commit()

            # 1. Parse
            raw_units = parse_document(file_bytes, filename)
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                for u in raw_units:
                    unit_id = str(uuid.uuid4())
                    u["id"] = unit_id
                    conn.execute(
                        source_unit_table.insert().values(
                            tenant_id=tenant_id,
                            id=unit_id,
                            build_id=build_id,
                            document_id=doc_id,
                            unit_key=u["unit_key"],
                            kind=u["kind"],
                            text_content=u["text_content"],
                            location=u.get("location", {}),
                            machine_generated=False,
                            artifact_id=None,
                            content_hash=u["content_hash"],
                            created_at=now,
                            updated_at=now,
                        )
                    )
                conn.execute(
                    update(job_table)
                    .where(job_table.c.id == job_id)
                    .values(stage="CHUNKING", updated_at=now)
                )
                conn.commit()

            # 2. Chunk
            chunks, spans = chunk_units(raw_units)
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                for c in chunks:
                    conn.execute(
                        chunk_table.insert().values(
                            tenant_id=tenant_id,
                            id=c["id"],
                            build_id=build_id,
                            parent_id=c["parent_id"],
                            kind=c["kind"],
                            ordinal=c["ordinal"],
                            text_content=c["text_content"],
                            token_count=c["token_count"],
                            content_hash=c["content_hash"],
                            created_at=now,
                            updated_at=now,
                        )
                    )
                for sp in spans:
                    conn.execute(
                        chunk_span_table.insert().values(
                            tenant_id=tenant_id,
                            id=sp["id"],
                            build_id=build_id,
                            chunk_id=sp["chunk_id"],
                            unit_id=sp["unit_id"],
                            ordinal=sp["ordinal"],
                            start_cp=sp["start_cp"],
                            end_cp=sp["end_cp"],
                            location=sp.get("location"),
                            created_at=now,
                            updated_at=now,
                        )
                    )

                # 3. Index to Milvus
                child_chunks = [c for c in chunks if c.get("kind") == "CHILD"]
                if child_chunks:
                    from app.retrieval.milvus_service import MilvusHybridService
                    doc_row = conn.execute(
                        select(document_table.c.kb_id).where(
                            and_(
                                document_table.c.tenant_id == tenant_id,
                                document_table.c.id == doc_id,
                            )
                        )
                    ).first()
                    kb_id = str(doc_row.kb_id) if doc_row else ""
                    MilvusHybridService.upsert_chunks(
                        tenant_id=tenant_id,
                        build_id=build_id,
                        chunks=child_chunks,
                        kb_id=kb_id,
                        doc_id=doc_id,
                        version_id=version_id,
                    )

                # 4. Publish build as active
                conn.execute(
                    update(index_build_table)
                    .where(index_build_table.c.id == build_id)
                    .values(status="READY", published_at=now, updated_at=now)
                )
                conn.execute(
                    update(document_table)
                    .where(document_table.c.id == doc_id)
                    .values(active_build_id=build_id, updated_at=now)
                )
                conn.execute(
                    update(job_table)
                    .where(job_table.c.id == job_id)
                    .values(status="COMPLETED", stage="INDEXING", updated_at=now)
                )
                conn.commit()

        except Exception as e:
            with get_db_connection() as conn:
                conn.execute(
                    update(job_table)
                    .where(job_table.c.id == job_id)
                    .values(status="FAILED", error_code="PARSER_FAILED", updated_at=datetime.now(timezone.utc))
                )
                conn.commit()

    @staticmethod
    def get_job(actor: Actor, job_id: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            job = conn.execute(
                select(job_table).where(
                    and_(
                        job_table.c.tenant_id == actor.tenant_id,
                        job_table.c.id == job_id,
                    )
                )
            ).first()
            if not job:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "作业不存在")

            return {
                "id": str(job.id),
                "kind": str(job.kind),
                "status": str(job.status),
                "stage": str(job.stage),
                "attempt": int(job.attempt),
                "errorCode": job.error_code,
            }

    @staticmethod
    def list_jobs(actor: Actor, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
        with get_db_connection() as conn:
            stmt = select(
                job_table.c.id,
                job_table.c.kind,
                job_table.c.target_id.label("targetId"),
                job_table.c.status,
                job_table.c.stage,
                job_table.c.attempt,
                job_table.c.error_code.label("errorCode"),
                job_table.c.lease_epoch.label("leaseEpoch"),
            ).where(job_table.c.tenant_id == actor.tenant_id)
            if job_id:
                stmt = stmt.where(job_table.c.id == job_id)
            stmt = stmt.order_by(job_table.c.created_at.desc()).limit(100)
            rows = conn.execute(stmt).fetchall()
            return [
                {
                    "id": str(r.id),
                    "kind": str(r.kind),
                    "targetId": str(r.targetId) if r.targetId else None,
                    "status": str(r.status),
                    "stage": str(r.stage),
                    "attempt": int(r.attempt),
                    "errorCode": r.errorCode,
                    "leaseEpoch": int(r.leaseEpoch) if r.leaseEpoch is not None else 0,
                }
                for r in rows
            ]

    @staticmethod
    def control_job(actor: Actor, job_id: str, cancel: bool) -> List[Dict[str, Any]]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            job = conn.execute(
                select(job_table).where(
                    and_(
                        job_table.c.tenant_id == actor.tenant_id,
                        job_table.c.id == job_id,
                    )
                )
            ).first()
            if not job:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "任务不存在")
            state = str(job.status)
            if cancel:
                if state not in ("COMPLETED", "FAILED", "CANCELLED"):
                    conn.execute(
                        update(job_table)
                        .where(and_(job_table.c.tenant_id == actor.tenant_id, job_table.c.id == job_id))
                        .values(
                            status="CANCELLED",
                            cancel_requested_at=now,
                            lease_epoch=job_table.c.lease_epoch + 1,
                            updated_at=now,
                        )
                    )
                    conn.commit()
            else:
                if state not in ("FAILED", "CANCELLED"):
                    raise ApiException(409, "INVALID_STATE", "任务当前不可重试")
                conn.execute(
                    update(job_table)
                    .where(and_(job_table.c.tenant_id == actor.tenant_id, job_table.c.id == job_id))
                    .values(
                        status="QUEUED",
                        cancel_requested_at=None,
                        error_code=None,
                        attempt=0,
                        available_at=now,
                        lease_epoch=job_table.c.lease_epoch + 1,
                        updated_at=now,
                    )
                )
                conn.commit()
            return KnowledgeService.list_jobs(actor, job_id)

    @staticmethod
    def get_preview(actor: Actor, doc_id: str, version_id: str) -> Dict[str, Any]:
        with get_db_connection() as conn:
            build = conn.execute(
                select(index_build_table.c.id).where(
                    and_(
                        index_build_table.c.tenant_id == actor.tenant_id,
                        index_build_table.c.document_id == doc_id,
                        index_build_table.c.version_id == version_id,
                    )
                )
            ).first()
            if not build:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "构建不存在")

            units = conn.execute(
                select(
                    source_unit_table.c.id,
                    source_unit_table.c.unit_key.label("key"),
                    source_unit_table.c.kind,
                    source_unit_table.c.text_content.label("text"),
                    source_unit_table.c.location,
                ).where(source_unit_table.c.build_id == build.id)
            ).fetchall()

            chunks = conn.execute(
                select(
                    chunk_table.c.id,
                    chunk_table.c.kind,
                    chunk_table.c.parent_id.label("parentId"),
                    chunk_table.c.ordinal,
                    chunk_table.c.text_content.label("text"),
                ).where(chunk_table.c.build_id == build.id)
            ).fetchall()

            return {
                "units": [{"id": str(u.id), "key": u.key, "kind": u.kind, "text": u.text, "location": u.location} for u in units],
                "chunks": [{"id": str(c.id), "kind": c.kind, "parentId": str(c.parentId) if c.parentId else None, "ordinal": c.ordinal, "text": c.text} for c in chunks],
                "warnings": [],
            }

    @staticmethod
    def document_lifecycle(actor: Actor, doc_id: str, action: str, expected_version: int) -> Dict[str, Any]:
        if not actor.admin:
            raise ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限")
        if action not in ("trash", "restore", "purge"):
            raise ApiException(400, "INVALID_INPUT", f"未知操作: {action}")

        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            doc = conn.execute(
                select(document_table).where(
                    and_(
                        document_table.c.tenant_id == actor.tenant_id,
                        document_table.c.id == doc_id,
                    )
                )
            ).first()
            if not doc:
                raise ApiException(404, "RESOURCE_NOT_FOUND", "文档不存在")

            if int(doc.row_version) != expected_version:
                raise ApiException(409, "CONCURRENT_CONFLICT", "文档版本冲突")

            state = str(doc.status)
            if action == "trash" and state != "LIVE":
                raise ApiException(409, "INVALID_STATE", "文档状态不允许此操作")
            if action == "restore" and state != "TRASHED":
                raise ApiException(409, "INVALID_STATE", "文档状态不允许此操作")
            if action == "purge" and state != "TRASHED":
                raise ApiException(409, "INVALID_STATE", "文档状态不允许此操作")

            new_fence = int(doc.lifecycle_epoch or 0) + 1
            new_row_version = expected_version + 1

            if action == "trash":
                conn.execute(
                    update(document_table)
                    .where(
                        and_(
                            document_table.c.tenant_id == actor.tenant_id,
                            document_table.c.id == doc_id,
                            document_table.c.row_version == expected_version,
                        )
                    )
                    .values(
                        status="TRASHED",
                        lifecycle_epoch=new_fence,
                        row_version=new_row_version,
                        deleted_at=now,
                        updated_at=now,
                    )
                )
                conn.execute(
                    update(tenant_table)
                    .where(tenant_table.c.id == actor.tenant_id)
                    .values(security_epoch=tenant_table.c.security_epoch + 1)
                )
                conn.execute(
                    audit_event_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        actor_id=actor.user_id,
                        action="DOCUMENT_TRASH",
                        object_type="DOCUMENT",
                        object_id=doc_id,
                        outcome="SUCCESS",
                        metadata_json="{}",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()
                return {"documentId": doc_id, "status": "TRASHED"}

            elif action == "restore":
                if not doc.active_build_id:
                    raise ApiException(409, "RESTORE_NOT_READY", "文档没有已发布构建")

                # Verify artifacts
                artifacts = conn.execute(
                    select(artifact_table.c.storage_key).where(
                        and_(
                            artifact_table.c.tenant_id == actor.tenant_id,
                            artifact_table.c.document_id == doc_id,
                            artifact_table.c.status == "READY",
                        )
                    )
                ).fetchall()
                for art in artifacts:
                    read_artifact(str(art.storage_key))

                # Restore in Milvus if needed
                build_id = str(doc.active_build_id)
                child_chunks = conn.execute(
                    select(
                        chunk_table.c.id,
                        chunk_table.c.text_content,
                    ).where(
                        and_(
                            chunk_table.c.tenant_id == actor.tenant_id,
                            chunk_table.c.build_id == build_id,
                            chunk_table.c.kind == "CHILD",
                        )
                    )
                ).fetchall()
                if child_chunks:
                    from app.retrieval.milvus_service import MilvusHybridService
                    chunks_data = [{"id": str(c.id), "text_content": c.text_content} for c in child_chunks]
                    b_row = conn.execute(
                        select(index_build_table.c.version_id).where(
                            and_(
                                index_build_table.c.tenant_id == actor.tenant_id,
                                index_build_table.c.id == build_id,
                            )
                        )
                    ).first()
                    version_id = str(b_row.version_id) if b_row else ""
                    MilvusHybridService.upsert_chunks(
                        tenant_id=actor.tenant_id,
                        build_id=build_id,
                        chunks=chunks_data,
                        kb_id=str(doc.kb_id),
                        doc_id=doc_id,
                        version_id=version_id,
                    )

                conn.execute(
                    update(document_table)
                    .where(
                        and_(
                            document_table.c.tenant_id == actor.tenant_id,
                            document_table.c.id == doc_id,
                            document_table.c.status == "TRASHED",
                        )
                    )
                    .values(
                        status="LIVE",
                        deleted_at=None,
                        lifecycle_epoch=new_fence,
                        row_version=new_row_version,
                        updated_at=now,
                    )
                )

                if not can_read_document(conn, actor, doc_id):
                    raise ApiException(409, "RESTORE_ACL_INVALID", "恢复前请为维护者配置阅读授权")

                job_id = str(uuid.uuid4())
                conn.execute(
                    job_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=job_id,
                        kind="RESTORE",
                        target_id=doc_id,
                        requested_by=actor.user_id,
                        idempotency_key=str(uuid.uuid4()),
                        request_hash=hashlib.sha256(f"{doc_id}:{new_fence}".encode("utf-8")).digest(),
                        status="COMPLETED",
                        stage="INDEXING",
                        attempt=0,
                        fence=new_fence,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.execute(
                    update(tenant_table)
                    .where(tenant_table.c.id == actor.tenant_id)
                    .values(security_epoch=tenant_table.c.security_epoch + 1)
                )
                conn.execute(
                    audit_event_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        actor_id=actor.user_id,
                        action="DOCUMENT_RESTORE",
                        object_type="DOCUMENT",
                        object_id=doc_id,
                        outcome="SUCCESS",
                        metadata_json="{}",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()
                return {"documentId": doc_id, "status": "LIVE", "jobId": job_id}

            elif action == "purge":
                conn.execute(
                    update(document_table)
                    .where(
                        and_(
                            document_table.c.tenant_id == actor.tenant_id,
                            document_table.c.id == doc_id,
                        )
                    )
                    .values(
                        status="PURGE_PENDING",
                        lifecycle_epoch=new_fence,
                        row_version=new_row_version,
                        updated_at=now,
                    )
                )
                del_rec_id = str(uuid.uuid4())
                conn.execute(
                    deletion_record_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=del_rec_id,
                        document_id=doc_id,
                        origin_system="local",
                        requested_by=actor.user_id,
                        status="PENDING",
                        domains_json={},
                        backup_expiry_at=datetime.fromtimestamp(now.timestamp() + 7 * 86400, timezone.utc),
                        completed_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                job_id = str(uuid.uuid4())
                conn.execute(
                    job_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=job_id,
                        kind="PURGE",
                        target_id=doc_id,
                        requested_by=actor.user_id,
                        idempotency_key=str(uuid.uuid4()),
                        request_hash=hashlib.sha256(f"{doc_id}:{new_fence}".encode("utf-8")).digest(),
                        status="RUNNING",
                        stage="PURGE",
                        attempt=0,
                        fence=new_fence,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.execute(
                    update(tenant_table)
                    .where(tenant_table.c.id == actor.tenant_id)
                    .values(security_epoch=tenant_table.c.security_epoch + 1)
                )
                conn.execute(
                    audit_event_table.insert().values(
                        tenant_id=actor.tenant_id,
                        id=str(uuid.uuid4()),
                        actor_id=actor.user_id,
                        action="DOCUMENT_PURGE",
                        object_type="DOCUMENT",
                        object_id=doc_id,
                        outcome="SUCCESS",
                        metadata_json="{}",
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.commit()

                try:
                    KnowledgeService._execute_purge(actor.tenant_id, doc_id, job_id, del_rec_id)
                except Exception:
                    with get_db_connection() as c:
                        c.execute(
                            update(job_table)
                            .where(job_table.c.id == job_id)
                            .values(status="FAILED", error_code="PURGE_FAILED", updated_at=datetime.now(timezone.utc))
                        )
                        c.execute(
                            update(deletion_record_table)
                            .where(deletion_record_table.c.id == del_rec_id)
                            .values(status="FAILED", updated_at=datetime.now(timezone.utc))
                        )
                        c.commit()
                    raise

                return {"documentId": doc_id, "status": "PURGED", "jobId": job_id}

    @staticmethod
    def _execute_purge(tenant_id: str, doc_id: str, job_id: str, del_rec_id: str):
        now = datetime.now(timezone.utc)
        empty_hash = hashlib.sha256(b"").digest()

        # 1. Delete from Milvus
        from app.retrieval.milvus_service import MilvusHybridService
        MilvusHybridService.delete_by_document(tenant_id, doc_id)

        # 2. Delete private artifact files & scrub MySQL
        with get_db_connection() as conn:
            artifacts = conn.execute(
                select(artifact_table.c.storage_key).where(
                    and_(
                        artifact_table.c.tenant_id == tenant_id,
                        artifact_table.c.document_id == doc_id,
                    )
                )
            ).fetchall()
            for art in artifacts:
                delete_artifact(str(art.storage_key))

            # 3. Scrub MySQL content
            affected_runs_stmt = (
                select(
                    qa_run_table.c.id,
                    qa_run_table.c.conversation_id,
                )
                .select_from(
                    qa_run_table.join(
                        run_source_table,
                        and_(
                            run_source_table.c.tenant_id == qa_run_table.c.tenant_id,
                            run_source_table.c.run_id == qa_run_table.c.id,
                        ),
                    )
                )
                .where(
                    and_(
                        run_source_table.c.tenant_id == tenant_id,
                        run_source_table.c.document_id == doc_id,
                    )
                )
            )
            affected_runs = conn.execute(affected_runs_stmt).fetchall()
            for ar in affected_runs:
                run_id = str(ar.id)
                conv_id = str(ar.conversation_id)
                conn.execute(
                    update(qa_run_table)
                    .where(
                        and_(
                            qa_run_table.c.tenant_id == tenant_id,
                            qa_run_table.c.id == run_id,
                        )
                    )
                    .values(
                        question=None,
                        result_json=None,
                        status="CANCELLED",
                        terminal_reason="SOURCE_PURGED",
                        updated_at=now,
                    )
                )
                conn.execute(
                    update(conversation_table)
                    .where(
                        and_(
                            conversation_table.c.tenant_id == tenant_id,
                            conversation_table.c.id == conv_id,
                        )
                    )
                    .values(title=None, updated_at=now)
                )
                conn.execute(
                    update(run_event_table)
                    .where(
                        and_(
                            run_event_table.c.tenant_id == tenant_id,
                            run_event_table.c.run_id == run_id,
                        )
                    )
                    .values(payload=None, updated_at=now)
                )
                conn.execute(
                    update(run_source_table)
                    .where(
                        and_(
                            run_source_table.c.tenant_id == tenant_id,
                            run_source_table.c.run_id == run_id,
                        )
                    )
                    .values(span_json=None, updated_at=now)
                )
                conn.execute(
                    update(feedback_table)
                    .where(
                        and_(
                            feedback_table.c.tenant_id == tenant_id,
                            feedback_table.c.run_id == run_id,
                        )
                    )
                    .values(comment_text=None, resolution_text=None, updated_at=now)
                )

            # Scrub source_units
            conn.execute(
                update(source_unit_table)
                .where(
                    and_(
                        source_unit_table.c.tenant_id == tenant_id,
                        source_unit_table.c.document_id == doc_id,
                    )
                )
                .values(text_content=None, location={}, updated_at=now)
            )

            # Scrub chunks, spans and representations
            doc_builds_stmt = select(index_build_table.c.id).where(
                and_(
                    index_build_table.c.tenant_id == tenant_id,
                    index_build_table.c.document_id == doc_id,
                )
            )
            doc_builds = [str(b[0]) for b in conn.execute(doc_builds_stmt).fetchall()]
            if doc_builds:
                conn.execute(
                    update(chunk_table)
                    .where(
                        and_(
                            chunk_table.c.tenant_id == tenant_id,
                            chunk_table.c.build_id.in_(doc_builds),
                        )
                    )
                    .values(text_content="", content_hash=empty_hash, updated_at=now)
                )
                conn.execute(
                    update(chunk_span_table)
                    .where(
                        and_(
                            chunk_span_table.c.tenant_id == tenant_id,
                            chunk_span_table.c.build_id.in_(doc_builds),
                        )
                    )
                    .values(location=None, updated_at=now)
                )
                conn.execute(
                    update(representation_table)
                    .where(
                        and_(
                            representation_table.c.tenant_id == tenant_id,
                            representation_table.c.build_id.in_(doc_builds),
                        )
                    )
                    .values(
                        text_content="",
                        vector_bytes=None,
                        dims=None,
                        vector_hash=None,
                        text_hash=empty_hash,
                        updated_at=now,
                    )
                )
                conn.execute(
                    update(index_build_table)
                    .where(
                        and_(
                            index_build_table.c.tenant_id == tenant_id,
                            index_build_table.c.document_id == doc_id,
                        )
                    )
                    .values(quality_warnings=None, review_reason=None, updated_at=now)
                )

            # Scrub artifacts & document_versions
            conn.execute(
                update(artifact_table)
                .where(
                    and_(
                        artifact_table.c.tenant_id == tenant_id,
                        artifact_table.c.document_id == doc_id,
                    )
                )
                .values(status="PURGED", updated_at=now)
            )
            conn.execute(
                update(document_version_table)
                .where(
                    and_(
                        document_version_table.c.tenant_id == tenant_id,
                        document_version_table.c.document_id == doc_id,
                    )
                )
                .values(original_name=None, updated_at=now)
            )

            # Finalize document & deletion record
            conn.execute(
                update(document_table)
                .where(
                    and_(
                        document_table.c.tenant_id == tenant_id,
                        document_table.c.id == doc_id,
                    )
                )
                .values(
                    title=None,
                    active_build_id=None,
                    status="PURGED",
                    row_version=document_table.c.row_version + 1,
                    updated_at=now,
                )
            )
            conn.execute(
                update(deletion_record_table)
                .where(
                    and_(
                        deletion_record_table.c.tenant_id == tenant_id,
                        deletion_record_table.c.id == del_rec_id,
                    )
                )
                .values(
                    status="COMPLETE",
                    completed_at=now,
                    domains_json={"local": "COMPLETE", "backup": "EXPIRES_7_DAYS"},
                    updated_at=now,
                )
            )
            conn.execute(
                update(job_table)
                .where(
                    and_(
                        job_table.c.tenant_id == tenant_id,
                        job_table.c.id == job_id,
                    )
                )
                .values(status="COMPLETED", stage="FINISHED", updated_at=now)
            )
            conn.commit()

