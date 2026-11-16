from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from archers.domain import Role

SCHEMA_REVISION = "0003_automatic_ingestion"


def now_utc() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128))
    acl_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (CheckConstraint("acl_revision > 0"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    username: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "username"),
        CheckConstraint("role IN ('reader', 'curator', 'admin')"),
        CheckConstraint("revision > 0"),
    )


class LoginSession(Base):
    __tablename__ = "sessions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(Uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_secret: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        CheckConstraint("expires_at > issued_at"),
    )


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    connector_kind: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    formats: Mapped[list[str]] = mapped_column(JSON)
    scan_policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    export_policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("connector_kind IN ('filesystem', 's3', 'sharepoint', 'google_drive')"),
        CheckConstraint("revision > 0"),
    )


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["users.tenant_id", "users.id"]),
    )


@event.listens_for(AuditEntry, "before_update")
@event.listens_for(AuditEntry, "before_delete")
def audit_immutable(*args: object) -> None:
    raise ValueError("Audit entries are append-only.")


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (CheckConstraint("failed_count >= 0"),)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(Uuid)
    method_path: Mapped[str] = mapped_column(String(256))
    key_hash: Mapped[str] = mapped_column(String(64))
    body_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["users.tenant_id", "users.id"]),
        UniqueConstraint("tenant_id", "actor_id", "method_path", "key_hash"),
        CheckConstraint("status >= 200 AND status < 300"),
    )


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    origin: Mapped[str] = mapped_column(String(16), default="manual")
    format: Mapped[str] = mapped_column(String(16))
    availability: Mapped[str] = mapped_column(String(16), default="PENDING")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    acl_revision: Mapped[int] = mapped_column(Integer, default=1)
    desired_sequence: Mapped[int] = mapped_column(Integer, default=1)
    active_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    active_generation_id: Mapped[UUID | None] = mapped_column(Uuid)
    export_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=lambda: {"allow_external_generation": False, "allow_external_embeddings": False},
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "id", "active_version_id"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_documents_active_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "id", "active_version_id", "active_generation_id"],
            [
                "index_generations.tenant_id", "index_generations.document_id",
                "index_generations.version_id", "index_generations.id",
            ],
            name="fk_documents_active_generation",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("revision > 0 AND acl_revision > 0 AND desired_sequence > 0"),
        CheckConstraint("origin IN ('manual', 'nas', 'connector')"),
        CheckConstraint(
            "availability IN ('PENDING', 'ACTIVE', 'SOURCE_DELETED', 'SUPPRESSED', 'DELETED')"
        ),
        CheckConstraint("format IN ('pdf','docx','pptx','xlsx','markdown','text','html','csv')"),
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[UUID] = mapped_column(Uuid)
    sequence: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(16))
    mime: Mapped[str] = mapped_column(String(128))
    original_locator: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="RECEIVED")
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "document_id", "id"),
        UniqueConstraint("tenant_id", "document_id", "sequence"),
        ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"]),
        CheckConstraint("sequence > 0 AND bytes >= 0"),
        CheckConstraint("length(source_hash) = 64"),
        CheckConstraint(
            "state IN ('RECEIVED','VALIDATING','EXTRACTING','QUALITY_CHECK','REVIEW_REQUIRED',"
            "'CHUNKING','EMBEDDING','INDEXING','VERIFYING','READY','FAILED',"
            "'CANCEL_REQUESTED','CANCELLED')"
        ),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    version_id: Mapped[UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(16), default="ingest")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    stage: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"], ["document_versions.tenant_id", "document_versions.id"]
        ),
        CheckConstraint("attempts >= 0 AND attempts <= 3 AND fencing_token >= 0"),
        CheckConstraint("kind IN ('scan', 'snapshot', 'ingest', 'cleanup')"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED')"
        ),
    )


class SourceScan(Base):
    __tablename__ = "source_scan_status"
    source_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_healthy_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_scan_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    error_code: Mapped[str | None] = mapped_column(String(64))
    observed_count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )


class SourceScanEvent(Base):
    __tablename__ = "source_scans"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    source_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    healthy: Mapped[bool] = mapped_column(Boolean)
    complete: Mapped[bool] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(64))
    observed_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )


@event.listens_for(SourceScanEvent, "before_update")
@event.listens_for(SourceScanEvent, "before_delete")
def scan_immutable(*args: object) -> None:
    raise ValueError("Completed source scans are append-only.")


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    source_id: Mapped[UUID] = mapped_column(Uuid)
    item_key: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(String(200))
    observed_hash: Mapped[str] = mapped_column(String(64))
    stable_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observations: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_hashed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_hash: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "item_key"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
        ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"]),
        CheckConstraint("observations > 0 AND source_revision > 0"),
    )


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    document_id: Mapped[UUID] = mapped_column(Uuid)
    version_id: Mapped[UUID] = mapped_column(Uuid)
    collection: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(128))
    chunk_count: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="STAGING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "version_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.document_id", "document_versions.id"],
        ),
        CheckConstraint("chunk_count > 0"),
        CheckConstraint("state IN ('STAGING','ACTIVE','RETIRED','FAILED')"),
    )


class ChunkRecord(Base):
    __tablename__ = "chunks"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    document_id: Mapped[UUID] = mapped_column(Uuid)
    version_id: Mapped[UUID] = mapped_column(Uuid)
    generation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text_locator: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)
    locations: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("generation_id", "ordinal"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id", "generation_id"],
            [
                "index_generations.tenant_id", "index_generations.document_id",
                "index_generations.version_id", "index_generations.id",
            ],
        ),
        CheckConstraint("ordinal >= 0 AND token_count > 0"),
    )


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)
