import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from archers.config import Settings
from archers.domain import DomainError, require_permission
from archers.dto import ExportPolicy, Format, StrictDTO
from archers.jobs import database_now
from archers.services import audit, make_cursor, read_cursor
from archers.storage import Document, DocumentVersion, Job, User


class AcceptedIngestion(StrictDTO):
    job_id: UUID
    document_id: UUID
    version_id: UUID


class AcceptedJob(StrictDTO):
    job_id: UUID


class DocumentDTO(StrictDTO):
    id: UUID
    title: str
    origin: Literal["manual", "nas", "connector"]
    availability: Literal["PENDING", "ACTIVE", "SOURCE_DELETED", "SUPPRESSED", "DELETED"]
    revision: int = Field(ge=1)
    active_version_id: UUID | None
    format: Format
    export_policy: ExportPolicy


class DocumentPage(StrictDTO):
    items: list[DocumentDTO]
    next_cursor: str | None
    warnings: list[str]


class JobError(StrictDTO):
    code: str
    message: str
    request_id: UUID
    retryable: bool


class JobDTO(StrictDTO):
    id: UUID
    kind: Literal["scan", "snapshot", "ingest", "cleanup"]
    status: Literal["PENDING", "RUNNING", "RETRY_WAIT", "SUCCEEDED", "FAILED", "CANCELLED"]
    stage: str | None
    attempts: int
    cancel_requested: bool
    error: JobError | None


EXTENSIONS: dict[str, tuple[Format, str]] = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".md": ("markdown", "text/markdown"),
    ".txt": ("text", "text/plain"),
    ".html": ("html", "text/html"),
    ".csv": ("csv", "text/csv"),
}


@dataclass(frozen=True)
class Snapshot:
    locator: str
    hash: str
    size: int
    format: Format
    mime: str


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def snapshot_file(
    settings: Settings,
    tenant_id: UUID,
    document_id: UUID,
    version_id: UUID,
    filename: str,
    stream: BinaryIO,
) -> Snapshot:
    extension = Path(filename).suffix.casefold()
    if extension not in EXTENSIONS or len(filename) > 256:
        raise DomainError("VALIDATION_ERROR", "Unsupported file format.", 422)
    format_key, mime = EXTENSIONS[extension]
    relative = Path(
        "tenants",
        str(tenant_id),
        "documents",
        str(document_id),
        "versions",
        str(version_id),
        "original" + extension,
    )
    directory = settings.artifact_root / relative.parent
    # Generated UUID-only paths are not influenced by the uploaded name.
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    size = 0
    digest = hashlib.sha256()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            while chunk := stream.read(65536):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise DomainError(
                        "UPLOAD_TOO_LARGE", "Uploaded file exceeds configured limit.", 413
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise DomainError("VALIDATION_ERROR", "Uploaded file is empty.", 422)
        with temporary.open("rb") as file:
            prefix = file.read(16)
        binary = prefix.startswith((b"%PDF-", b"PK", b"\xd0\xcf\x11\xe0"))
        if (
            (format_key == "pdf" and not prefix.startswith(b"%PDF-"))
            or (format_key in {"docx", "pptx", "xlsx"} and not prefix.startswith(b"PK\x03\x04"))
            or (format_key in {"text", "markdown", "csv", "html"} and binary)
        ):
            raise DomainError(
                "VALIDATION_ERROR", "File bytes do not match the allowed format.", 422
            )
        final = settings.artifact_root / relative
        os.replace(temporary, final)
        temporary = None
        # Sync the newly created directory chain before committing database references.
        ancestor = directory
        while ancestor != settings.artifact_root.parent:
            sync_directory(ancestor)
            ancestor = ancestor.parent
        return Snapshot(relative.as_posix(), digest.hexdigest(), size, format_key, mime)
    except (DomainError, OSError):
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        (directory / ("original" + extension)).unlink(missing_ok=True)
        directory.rmdir()
        raise


def discard_snapshot(settings: Settings, snapshot: Snapshot) -> None:
    path = settings.artifact_root / snapshot.locator
    path.unlink(missing_ok=True)
    path.parent.rmdir()


def ensure_queue_capacity(db: Session, tenant_id: UUID, maximum: int) -> None:
    pending = db.scalar(
        select(func.count(Job.id)).where(
            Job.tenant_id == tenant_id,
            Job.status.in_(["PENDING", "RUNNING", "RETRY_WAIT"]),
        )
    )
    if pending is None or pending >= maximum:
        raise DomainError("RATE_LIMITED", "Ingestion queue is at capacity.", 429, 30)


def accept_snapshot(
    db: Session,
    actor: User,
    snapshot: Snapshot,
    title: str,
    document_id: UUID,
    version_id: UUID,
    *,
    revision: int | None = None,
    max_pending_jobs: int = 100,
) -> AcceptedIngestion:
    require_permission(actor.role, "ingestion.manage")
    ensure_queue_capacity(db, actor.tenant_id, max_pending_jobs)
    if not title.strip() or len(title) > 300 or any(ord(c) < 32 for c in title):
        raise DomainError("VALIDATION_ERROR", "Title requires 1 to 300 printable characters.", 422)
    if revision is not None:
        document = db.scalar(
            select(Document)
            .where(Document.id == document_id, Document.tenant_id == actor.tenant_id)
            .with_for_update()
        )
        if not document:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        if document.origin != "manual":
            raise DomainError("SOURCE_MANAGED", "Edits belong on the original source.", 409)
        if document.availability in {"DELETED", "SUPPRESSED", "SOURCE_DELETED"}:
            raise DomainError("REVISION_CONFLICT", "Document is withdrawn.", 409)
        if document.revision != revision:
            raise DomainError("REVISION_CONFLICT", "Resource revision changed.", 409)
        document.desired_sequence += 1
        document.revision += 1
        # Running superseded work receives cancellation, not a false completion.
        version_ids = select(DocumentVersion.id).where(
            DocumentVersion.tenant_id == actor.tenant_id,
            DocumentVersion.document_id == document.id,
        )
        db.execute(
            update(Job)
            .where(
                Job.tenant_id == actor.tenant_id,
                Job.version_id.in_(version_ids),
                Job.status.in_(["PENDING", "RUNNING", "RETRY_WAIT"]),
            )
            .values(cancel_requested=True)
        )
    else:
        document = Document(
            id=document_id,
            tenant_id=actor.tenant_id,
            title=title.strip(),
            format=snapshot.format,
        )
        db.add(document)
        db.flush()
    version = DocumentVersion(
        id=version_id,
        tenant_id=actor.tenant_id,
        document_id=document.id,
        sequence=document.desired_sequence,
        source_hash=snapshot.hash,
        bytes=snapshot.size,
        format=snapshot.format,
        mime=snapshot.mime,
        original_locator=snapshot.locator,
    )
    db.add(version)
    db.flush()
    job = Job(
        tenant_id=actor.tenant_id,
        version_id=version.id,
        stage="RECEIVED",
        payload={
            "document_id": str(document.id),
            "version_id": str(version.id),
            "desired_sequence": version.sequence,
        },
    )
    db.add(job)
    db.flush()
    audit(db, actor, "documents.upload" if revision is None else "documents.replace", document.id)
    return AcceptedIngestion(job_id=job.id, document_id=document.id, version_id=version.id)


def document_dto(document: Document) -> DocumentDTO:
    return DocumentDTO.model_validate(
        {
            "id": document.id,
            "title": document.title,
            "origin": document.origin,
            "availability": document.availability,
            "revision": document.revision,
            "active_version_id": document.active_version_id,
            "format": document.format,
            "export_policy": document.export_policy,
        }
    )


def get_document(db: Session, actor: User, document_id: UUID) -> DocumentDTO:
    require_permission(actor.role, "documents.read")
    document = db.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == actor.tenant_id)
    )
    if not document or (document.availability != "ACTIVE" and actor.role == "reader"):
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return document_dto(document)


def list_documents(
    db: Session, settings: Settings, actor: User, cursor: str | None, limit: int
) -> DocumentPage:
    require_permission(actor.role, "documents.read")
    last_id = read_cursor(settings, actor, "documents", cursor)
    query = select(Document).where(Document.tenant_id == actor.tenant_id)
    if actor.role == "reader":
        query = query.where(Document.availability == "ACTIVE")
    if last_id:
        query = query.where(Document.id > last_id)
    documents = list(db.scalars(query.order_by(Document.id).limit(limit + 1)))
    more = len(documents) > limit
    documents = documents[:limit]
    return DocumentPage(
        items=[document_dto(document) for document in documents],
        next_cursor=make_cursor(settings, actor, "documents", documents[-1].id) if more else None,
        warnings=["RAG query generation is not connected; automatic source withdrawal is disabled."],
    )


def get_job(db: Session, actor: User, job_id: UUID, *, lock: bool = False) -> Job:
    require_permission(actor.role, "ingestion.manage")
    query = select(Job).where(Job.id == job_id, Job.tenant_id == actor.tenant_id)
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if not job:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return job


def job_dto(job: Job, request_id: UUID) -> JobDTO:
    return JobDTO.model_validate(
        {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "stage": job.stage,
            "attempts": job.attempts,
            "cancel_requested": job.cancel_requested,
            "error": None
            if not job.error_code
            else JobError(
                code=job.error_code,
                message="Processing failed; inspect stage and retry policy.",
                request_id=request_id,
                retryable=job.error_retryable,
            ),
        }
    )


def retry_job(
    db: Session, actor: User, job_id: UUID, *, max_pending_jobs: int = 100
) -> AcceptedJob:
    job = get_job(db, actor, job_id, lock=True)
    if (
        job.status != "FAILED"
        or not job.error_retryable
        or job.attempts >= 3
        or job.cancel_requested
    ):
        raise DomainError("REVISION_CONFLICT", "Job is not eligible for retry.", 409)
    ensure_queue_capacity(db, actor.tenant_id, max_pending_jobs)
    version = db.get(DocumentVersion, job.version_id)
    if version is None:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Version unavailable.", 503)
    document = db.get(Document, version.document_id)
    if (
        not document
        or document.desired_sequence != version.sequence
        or document.availability
        in {
            "DELETED",
            "SUPPRESSED",
            "SOURCE_DELETED",
        }
    ):
        raise DomainError(
            "REVISION_CONFLICT", "Job belongs to a superseded or withdrawn version.", 409
        )
    job.status = "PENDING"
    job.available_at = database_now(db)
    job.error_code = None
    job.error_retryable = False
    audit(db, actor, "jobs.retry", job.id)
    return AcceptedJob(job_id=job.id)


UPLOAD_PATH = re.compile(r"^/api/v1/documents(?:/[0-9a-fA-F-]{36}/versions)?$")
