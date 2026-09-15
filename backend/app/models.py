from datetime import datetime, timezone
from sqlalchemy import (
    MetaData, Table, Column, String, DateTime, Boolean, BigInteger, LargeBinary
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
