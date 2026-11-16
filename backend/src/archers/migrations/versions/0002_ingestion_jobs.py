"""Durable manual document acceptance and fenced ingestion jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0002_ingestion_jobs"
down_revision = "0001_identity_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("availability", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("acl_revision", sa.Integer(), nullable=False),
        sa.Column("desired_sequence", sa.Integer(), nullable=False),
        sa.Column("active_version_id", sa.Uuid()),
        sa.Column("active_generation_id", sa.Uuid()),
        sa.Column("export_policy", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.CheckConstraint("revision > 0 AND acl_revision > 0 AND desired_sequence > 0"),
        sa.CheckConstraint("origin IN ('manual', 'nas', 'connector')"),
        sa.CheckConstraint(
            "availability IN ('PENDING','ACTIVE','SOURCE_DELETED','SUPPRESSED','DELETED')"
        ),
        sa.CheckConstraint("format IN ('pdf','docx','pptx','xlsx','markdown','text','html','csv')"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("original_locator", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("quality_report", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "document_id", "id"),
        sa.UniqueConstraint("tenant_id", "document_id", "sequence"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"]
        ),
        sa.CheckConstraint("sequence > 0 AND bytes >= 0"),
        sa.CheckConstraint("length(source_hash) = 64"),
        sa.CheckConstraint(
            "state IN ('RECEIVED','VALIDATING','EXTRACTING','QUALITY_CHECK','REVIEW_REQUIRED',"
            "'CHUNKING','EMBEDDING','INDEXING','VERIFYING','READY','FAILED',"
            "'CANCEL_REQUESTED','CANCELLED')"
        ),
    )
    op.create_index("ix_document_versions_tenant_id", "document_versions", ["tenant_id"])
    op.create_foreign_key(
        "fk_documents_active_version",
        "documents",
        "document_versions",
        ["tenant_id", "id", "active_version_id"],
        ["tenant_id", "document_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(32)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_retryable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"], ["document_versions.tenant_id", "document_versions.id"]
        ),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3 AND fencing_token >= 0"),
        sa.CheckConstraint("kind IN ('scan','snapshot','ingest','cleanup')"),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED')"
        ),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_available_at", "jobs", ["available_at"])


def downgrade() -> None:
    raise RuntimeError("Ingestion data downgrade is blocked. Restore an isolated backup.")
