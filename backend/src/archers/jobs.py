from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from archers.domain import DomainError
from archers.storage import DocumentVersion, Job, aware, now_utc


@dataclass(frozen=True)
class Lease:
    job_id: UUID
    tenant_id: UUID
    version_id: UUID
    owner: str
    fence: int
    attempt: int


def database_now(db: Session) -> datetime:
    # Database time is authoritative across workers; SQLite is only a unit-test adapter.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy import func

        value = db.scalar(select(func.clock_timestamp()))
        if isinstance(value, datetime):
            return aware(value)
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Database time unavailable.", 503)
    return now_utc()


def claim(db: Session, owner: str, *, lease_seconds: int = 120) -> Lease | None:
    if not owner or len(owner) > 128 or not 60 <= lease_seconds <= 600:
        raise ValueError("Invalid worker lease parameters.")
    now = database_now(db)
    query = (
        select(Job)
        .where(
            Job.kind == "ingest",
            or_(
                and_(Job.status.in_(["PENDING", "RETRY_WAIT"]), Job.available_at <= now),
                and_(Job.status == "RUNNING", Job.lease_expires_at <= now),
            ),
        )
        .order_by(Job.available_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    while (job := db.scalar(query)) is not None:
        if job.cancel_requested:
            job.status = "CANCELLED"
            job.stage = "CANCELLED"
            version = db.get(DocumentVersion, job.version_id)
            if version:
                version.state = "CANCELLED"
            clear_lease(job)
            db.flush()
            continue
        if job.attempts >= 3:
            job.status = "FAILED"
            job.error_code = "DEADLINE_EXCEEDED"
            job.error_retryable = False
            version = db.get(DocumentVersion, job.version_id)
            if version:
                version.state = "FAILED"
            clear_lease(job)
            db.flush()
            continue
        job.attempts += 1
        job.fencing_token += 1
        job.lease_owner = owner
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.heartbeat_at = now
        job.status = "RUNNING"
        job.stage = "RECEIVED"
        job.error_code = None
        job.error_retryable = False
        version = db.get(DocumentVersion, job.version_id)
        if not version or version.tenant_id != job.tenant_id:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Job linkage unavailable.", 503)
        version.state = "RECEIVED"
        db.flush()
        return Lease(job.id, job.tenant_id, job.version_id, owner, job.fencing_token, job.attempts)
    return None


def locked_job(db: Session, lease: Lease, *, allow_cancel: bool = False) -> Job:
    job = db.scalar(
        select(Job)
        .where(Job.id == lease.job_id, Job.tenant_id == lease.tenant_id)
        .with_for_update()
    )
    now = database_now(db)
    if (
        not job
        or job.status != "RUNNING"
        or job.lease_owner != lease.owner
        or job.fencing_token != lease.fence
        or job.version_id != lease.version_id
        or job.lease_expires_at is None
        or aware(job.lease_expires_at) <= now
    ):
        raise DomainError("REVISION_CONFLICT", "Lease is expired or superseded.", 409)
    if job.cancel_requested and not allow_cancel:
        raise DomainError("AUTHORIZATION_CHANGED", "Job cancellation requested.", 409)
    return job


def heartbeat(db: Session, lease: Lease, *, lease_seconds: int = 120) -> None:
    if not 60 <= lease_seconds <= 600:
        raise ValueError("Invalid lease duration.")
    job = locked_job(db, lease)
    now = database_now(db)
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)


STAGES = (
    "RECEIVED",
    "VALIDATING",
    "EXTRACTING",
    "QUALITY_CHECK",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "VERIFYING",
)


def advance(db: Session, lease: Lease, stage: str) -> None:
    job = locked_job(db, lease)
    if (
        job.stage not in STAGES
        or stage not in STAGES
        or STAGES.index(stage) != STAGES.index(job.stage) + 1
    ):
        raise DomainError("REVISION_CONFLICT", "Invalid processing transition.", 409)
    version = db.get(DocumentVersion, lease.version_id)
    if version is None or version.tenant_id != lease.tenant_id:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Version unavailable.", 503)
    version.state = stage
    job.stage = stage


def clear_lease(job: Job) -> None:
    job.lease_owner = None
    job.lease_expires_at = None


def cancel(db: Session, lease: Lease) -> None:
    job = locked_job(db, lease, allow_cancel=True)
    job.cancel_requested = True
    job.status = "CANCELLED"
    job.stage = "CANCELLED"
    version = db.get(DocumentVersion, lease.version_id)
    if version is not None:
        version.state = "CANCELLED"
    clear_lease(job)


FAILURES = {
    "DEPENDENCY_UNAVAILABLE",
    "MODEL_UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "PDF_INVALID",
    "PDF_ENCRYPTED",
    "VALIDATION_ERROR",
    "UPLOAD_TOO_LARGE",
}


def fail(
    db: Session, lease: Lease, code: str, *, retryable: bool, automatic_retry: bool = True
) -> None:
    if code not in FAILURES:
        raise ValueError("Only sanitized failure codes can be stored.")
    job = locked_job(db, lease)
    job.error_code = code
    job.error_retryable = retryable
    job.status = "RETRY_WAIT" if retryable and automatic_retry and job.attempts < 3 else "FAILED"
    job.available_at = database_now(db) + timedelta(seconds=20 * job.attempts)
    version = db.get(DocumentVersion, lease.version_id)
    if version is not None:
        version.state = "FAILED"
    clear_lease(job)
