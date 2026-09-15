from datetime import datetime, timezone
from sqlalchemy import (
    MetaData, Table, Column, String, DateTime, Boolean, BigInteger, Integer, LargeBinary, Text, JSON
)
from sqlalchemy.sql import func

metadata = MetaData()

tenant_table = Table(
    "tenant",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("code", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("security_epoch", BigInteger, nullable=False, default=0, server_default="0"),
    Column("active_retrieval_config_id", String(36), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

app_user_table = Table(
    "app_user",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("login_name", String(128), nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("is_admin", Boolean, nullable=False, default=False, server_default="0"),
    Column("status", String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

auth_session_table = Table(
    "auth_session",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), nullable=False),
    Column("token_hash", LargeBinary(32), nullable=False, unique=True),
    Column("expires_at", DateTime(6), nullable=False),
    Column("revoked_at", DateTime(6), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

user_group_table = Table(
    "user_group",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

group_member_table = Table(
    "group_member",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("group_id", String(36), nullable=False),
    Column("user_id", String(36), nullable=False),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

knowledge_base_table = Table(
    "knowledge_base",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("status", String(16), nullable=False, default="LIVE", server_default="LIVE"),
    Column("row_version", BigInteger, nullable=False, default=0, server_default="0"),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

kb_grant_table = Table(
    "kb_grant",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("kb_id", String(36), nullable=False),
    Column("principal_type", String(8), nullable=False),
    Column("user_id", String(36), nullable=True),
    Column("group_id", String(36), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

document_table = Table(
    "document",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("kb_id", String(36), nullable=False),
    Column("title", String(512), nullable=True),
    Column("acl_mode", String(16), nullable=False, default="RESTRICT", server_default="RESTRICT"),
    Column("status", String(20), nullable=False, default="LIVE", server_default="LIVE"),
    Column("active_build_id", String(36), nullable=True),
    Column("lifecycle_epoch", BigInteger, nullable=False, default=0, server_default="0"),
    Column("row_version", BigInteger, nullable=False, default=0, server_default="0"),
    Column("deleted_at", DateTime(6), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

document_grant_table = Table(
    "document_grant",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False),
    Column("principal_type", String(8), nullable=False),
    Column("user_id", String(36), nullable=True),
    Column("group_id", String(36), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

artifact_table = Table(
    "artifact",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("storage_key", String(512), nullable=False, unique=True),
    Column("sha256", LargeBinary(32), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("status", String(16), nullable=False, default="READY", server_default="READY"),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

document_version_table = Table(
    "document_version",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("source_artifact_id", String(36), nullable=True),
    Column("source_hash", LargeBinary(32), nullable=False),
    Column("original_name", String(512), nullable=True),
    Column("media_type", String(128), nullable=False),
    Column("created_by", String(36), nullable=False),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

config_revision_table = Table(
    "config_revision",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("kind", String(24), nullable=False),
    Column("name", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("sha256", LargeBinary(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

index_build_table = Table(
    "index_build",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), nullable=False),
    Column("version_id", String(36), nullable=False),
    Column("build_no", Integer, nullable=False),
    Column("config_id", String(36), nullable=False),
    Column("status", String(24), nullable=False, default="QUEUED", server_default="QUEUED"),
    Column("model_fingerprint", String(255), nullable=True),
    Column("embedding_dims", Integer, nullable=True),
    Column("capabilities", JSON, nullable=False),
    Column("manifest_hash", LargeBinary(32), nullable=True),
    Column("fence", BigInteger, nullable=False, default=0, server_default="0"),
    Column("published_at", DateTime(6), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

source_unit_table = Table(
    "source_unit",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("build_id", String(36), nullable=False),
    Column("document_id", String(36), nullable=False),
    Column("unit_key", String(191), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("text_content", Text, nullable=True),
    Column("location", JSON, nullable=False),
    Column("machine_generated", Boolean, nullable=False, default=False, server_default="0"),
    Column("artifact_id", String(36), nullable=True),
    Column("content_hash", LargeBinary(32), nullable=False),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

chunk_table = Table(
    "chunk",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("build_id", String(36), nullable=False),
    Column("parent_id", String(36), nullable=True),
    Column("kind", String(8), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("text_content", Text, nullable=False),
    Column("token_count", Integer, nullable=False, default=0, server_default="0"),
    Column("content_hash", LargeBinary(32), nullable=False),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

chunk_span_table = Table(
    "chunk_span",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("build_id", String(36), nullable=False),
    Column("chunk_id", String(36), nullable=False),
    Column("unit_id", String(36), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("start_cp", Integer, nullable=False),
    Column("end_cp", Integer, nullable=False),
    Column("location", JSON, nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

representation_table = Table(
    "representation",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("build_id", String(36), nullable=False),
    Column("chunk_id", String(36), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("ordinal", Integer, nullable=False, default=0, server_default="0"),
    Column("text_content", Text, nullable=False),
    Column("text_hash", LargeBinary(32), nullable=False),
    Column("model_fingerprint", String(255), nullable=True),
    Column("dims", Integer, nullable=True),
    Column("vector_bytes", LargeBinary, nullable=True),
    Column("vector_hash", LargeBinary(32), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)

job_table = Table(
    "job",
    metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("id", String(36), primary_key=True),
    Column("kind", String(24), nullable=False),
    Column("target_id", String(36), nullable=False),
    Column("requested_by", String(36), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_hash", LargeBinary(32), nullable=False),
    Column("status", String(24), nullable=False, default="QUEUED", server_default="QUEUED"),
    Column("stage", String(32), nullable=False, default="PARSING", server_default="PARSING"),
    Column("attempt", Integer, nullable=False, default=0, server_default="0"),
    Column("available_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("lease_until", DateTime(6), nullable=True),
    Column("lease_epoch", BigInteger, nullable=False, default=0, server_default="0"),
    Column("fence", BigInteger, nullable=True),
    Column("checkpoint", JSON, nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("cancel_requested_at", DateTime(6), nullable=True),
    Column("created_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column("updated_at", DateTime(6), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
)
