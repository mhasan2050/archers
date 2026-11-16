"""Durable filesystem observations and verified index generations."""

import sqlalchemy as sa
from alembic import op

revision = "0003_automatic_ingestion"
down_revision = "0002_ingestion_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_scan_status",
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("last_healthy_at", sa.DateTime(timezone=True)),
        sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("observed_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )
    op.create_table(
        "source_scans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("observed_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )
    op.create_index("ix_source_scans_source_id", "source_scans", ["source_id"])
    op.create_table(
        "source_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("signature", sa.String(200), nullable=False),
        sa.Column("observed_hash", sa.String(64), nullable=False),
        sa.Column("stable_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_hashed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_hash", sa.String(64)),
        sa.Column("document_id", sa.Uuid()),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("tenant_id", "source_id", "item_key"),
        sa.ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"]
        ),
        sa.CheckConstraint("observations > 0 AND source_revision > 0"),
    )
    op.create_table(
        "index_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("collection", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "document_id", "version_id", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.document_id", "document_versions.id"],
        ),
        sa.CheckConstraint("chunk_count > 0"),
        sa.CheckConstraint("state IN ('STAGING','ACTIVE','RETIRED','FAILED')"),
    )
    op.create_foreign_key(
        "fk_documents_active_generation", "documents", "index_generations",
        ["tenant_id", "id", "active_version_id", "active_generation_id"],
        ["tenant_id", "document_id", "version_id", "id"],
        deferrable=True, initially="DEFERRED",
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text_locator", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.UniqueConstraint("generation_id", "ordinal"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id", "generation_id"],
            ["index_generations.tenant_id", "index_generations.document_id",
             "index_generations.version_id", "index_generations.id"],
        ),
        sa.CheckConstraint("ordinal >= 0 AND token_count > 0"),
    )
    op.create_index("ix_chunks_generation_id", "chunks", ["generation_id"])


def downgrade() -> None:
    raise RuntimeError("Ingestion downgrade is blocked. Restore an isolated backup.")
