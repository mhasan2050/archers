import hashlib
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from archers.config import Settings
from archers.documents import sync_directory
from archers.domain import DomainError
from archers.indexing import LocalEncoder, VectorIndex
from archers.jobs import (
    Lease,
    advance,
    cancel,
    claim,
    clear_lease,
    database_now,
    fail,
    heartbeat,
    locked_job,
)
from archers.parsing import parse_isolated
from archers.storage import (
    ChunkRecord,
    Document,
    DocumentVersion,
    IndexGeneration,
    Source,
    SourceFile,
    SourceScan,
    Tenant,
    aware,
)

logger = logging.getLogger(__name__)


@contextmanager
def keep_lease(engine: Engine, lease: Lease, settings: Settings) -> Iterator[None]:
    stop = Event()
    errors: list[DomainError] = []

    def renew() -> None:
        while not stop.wait(20):
            try:
                with Session(engine) as db, db.begin():
                    heartbeat(db, lease, lease_seconds=settings.job_lease_seconds)
            except (DomainError, SQLAlchemyError):
                errors.append(DomainError("REVISION_CONFLICT", "Worker lease lost.", 409))
                logger.warning("worker_lease_lost job_id=%s", lease.job_id)
                return

    thread = Thread(target=renew, name="ingestion-heartbeat", daemon=True)
    thread.start()
    try:
        yield
        if errors:
            raise errors[0]
    finally:
        stop.set()
        thread.join(timeout=5)


def current_document(db: Session, lease: Lease) -> tuple[DocumentVersion, Document]:
    job = locked_job(db, lease)
    version = db.get(DocumentVersion, lease.version_id)
    if version is None or version.tenant_id != lease.tenant_id:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Version linkage unavailable.", 503)
    document = db.get(Document, version.document_id)
    if (
        document is None or document.tenant_id != lease.tenant_id
        or document.desired_sequence != version.sequence
        or document.availability in {"SUPPRESSED", "SOURCE_DELETED", "DELETED"}
    ):
        raise DomainError("AUTHORIZATION_CHANGED", "Version is no longer eligible.", 409)
    if "source_id" in job.payload:
        source_id = UUID(job.payload["source_id"])
        source = db.get(Source, source_id)
        scan = db.get(SourceScan, source_id)
        item = db.get(SourceFile, UUID(job.payload["source_file_id"]))
        if (
            source is None or source.tenant_id != lease.tenant_id or not source.enabled
            or source.revision != job.payload["source_revision"]
            or item is None or item.tenant_id != lease.tenant_id or item.source_id != source.id
            or item.document_id != document.id or item.source_revision != source.revision
            or item.accepted_hash != version.source_hash
            or item.observed_hash != version.source_hash or item.error_code == "SOURCE_FILE_LIMIT"
        ):
            raise DomainError("AUTHORIZATION_CHANGED", "Source revision is no longer eligible.", 409)
        if (
            scan is None or scan.error_code not in {None, "SOURCE_SCAN_BUDGET_DEFERRED"}
            or scan.last_healthy_at is None
            or database_now(db) - aware(scan.last_healthy_at)
            > timedelta(seconds=max(90, int(source.scan_policy["poll_seconds"]) * 3))
        ):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Source scan is unhealthy or stale.", 503)
        if (
            scan.error_code is None and scan.last_healthy_at is not None
            and aware(item.last_seen_at) < aware(scan.last_healthy_at)
        ):
            raise DomainError("AUTHORIZATION_CHANGED", "Source file is no longer present.", 409)
    return version, document


def stage(engine: Engine, lease: Lease, name: str) -> None:
    with Session(engine) as db, db.begin():
        current_document(db, lease)
        advance(db, lease, name)


def write_artifact(directory: Path, name: str, content: str) -> None:
    with (directory / name).open("x", encoding="utf-8") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    sync_directory(directory)


def publish(engine: Engine, lease: Lease, generation_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        # Match the API/watcher lock order before acquiring the job fence.
        db.execute(select(Tenant).where(Tenant.id == lease.tenant_id).with_for_update())
        version = db.get(DocumentVersion, lease.version_id)
        if version is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Version unavailable.", 503)
        document = db.scalar(select(Document).where(
            Document.id == version.document_id, Document.tenant_id == lease.tenant_id,
        ).with_for_update())
        current_document(db, lease)
        generation = db.get(IndexGeneration, generation_id)
        if generation is None or generation.version_id != version.id or generation.state != "STAGING":
            raise DomainError("REVISION_CONFLICT", "Generation is not eligible.", 409)
        if document is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Document unavailable.", 503)
        incompatible = db.scalar(select(IndexGeneration.id).where(
            IndexGeneration.tenant_id == lease.tenant_id,
            IndexGeneration.state == "ACTIVE",
            IndexGeneration.fingerprint != generation.fingerprint,
        ).limit(1))
        if incompatible:
            raise DomainError("MODEL_UNAVAILABLE", "Embedding profile change requires reindex.", 503)
        if document.active_generation_id:
            old = db.get(IndexGeneration, document.active_generation_id)
            if old:
                old.state = "RETIRED"
        document.active_generation_id = generation.id
        document.active_version_id = version.id
        document.availability = "ACTIVE"
        document.format = version.format
        document.revision += 1
        generation.state = "ACTIVE"
        version.state = "READY"
        job = locked_job(db, lease)
        if job.stage != "VERIFYING":
            raise DomainError("REVISION_CONFLICT", "Generation is not verified.", 409)
        job.stage = "READY"
        job.status = "SUCCEEDED"
        clear_lease(job)


def process(
    engine: Engine, settings: Settings, encoder: LocalEncoder, index: VectorIndex, lease: Lease,
) -> None:
    with Session(engine) as db, db.begin():
        version, document = current_document(db, lease)
        locator, format_key, source_hash = version.original_locator, version.format, version.source_hash
        document_id = document.id
    stage(engine, lease, "VALIDATING")
    original = settings.artifact_root / locator
    digest = hashlib.sha256()
    with original.open("rb") as file:
        while block := file.read(65536):
            digest.update(block)
    if digest.hexdigest() != source_hash:
        raise DomainError("VALIDATION_ERROR", "Original snapshot failed integrity check.", 422)
    stage(engine, lease, "EXTRACTING")
    normalized = parse_isolated(original, format_key, settings)
    stage(engine, lease, "QUALITY_CHECK")
    if normalized.warnings or not normalized.blocks:
        with Session(engine) as db, db.begin():
            version, _ = current_document(db, lease)
            fail(db, lease, "VALIDATION_ERROR", retryable=False)
            version.state = "REVIEW_REQUIRED"
            version.quality_report = {
                "warnings": [asdict(w) for w in normalized.warnings],
                "empty": not normalized.blocks,
            }
        return
    stage(engine, lease, "CHUNKING")
    chunks = encoder.chunk(normalized)
    if not chunks or len(chunks) > 50000:
        raise DomainError("VALIDATION_ERROR", "Chunk limits exceeded.", 422)
    generation_id = uuid4()
    relative = Path(locator).parent / "generations" / str(generation_id)
    directory = settings.artifact_root / relative
    directory.mkdir(parents=True, mode=0o700)
    write_artifact(directory, "normalization.json", json.dumps(asdict(normalized)))
    write_artifact(directory, "document.md", "\n\n".join(b.text for b in normalized.blocks))
    write_artifact(directory, "chunks.jsonl", "".join(
        json.dumps(asdict(chunk)) + "\n" for chunk in chunks
    ))
    sync_directory(directory.parent)
    with Session(engine) as db, db.begin():
        current_document(db, lease)
        db.add(IndexGeneration(
            id=generation_id, tenant_id=lease.tenant_id, document_id=document_id,
            version_id=lease.version_id, collection=index.collection,
            fingerprint=encoder.fingerprint, chunk_count=len(chunks),
        ))
        db.flush()
        for chunk in chunks:
            db.add(ChunkRecord(
                tenant_id=lease.tenant_id, document_id=document_id, version_id=lease.version_id,
                generation_id=generation_id, ordinal=chunk.ordinal,
                text_locator=(relative / "chunks.jsonl").as_posix(),
                text_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
                token_count=chunk.token_count, locations=chunk.locations,
            ))
    stage(engine, lease, "EMBEDDING")
    texts = [chunk.text for chunk in chunks]
    dense = encoder.encode(texts)
    sparse = encoder.sparse(texts)
    stage(engine, lease, "INDEXING")
    index.stage(lease.tenant_id, document_id, lease.version_id, generation_id, chunks, dense, sparse)
    stage(engine, lease, "VERIFYING")
    publish(engine, lease, generation_id)


def work_once(
    engine: Engine, settings: Settings, encoder: LocalEncoder, index: VectorIndex, owner: str,
) -> bool:
    with Session(engine) as db, db.begin():
        lease = claim(db, owner, lease_seconds=settings.job_lease_seconds)
    if lease is None:
        return False
    try:
        with keep_lease(engine, lease, settings):
            process(engine, settings, encoder, index, lease)
        logger.info("worker_finished job_id=%s", lease.job_id)
    except (DomainError, OSError, ValueError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "DEPENDENCY_UNAVAILABLE"
        logger.error("worker_failed job_id=%s code=%s", lease.job_id, code)
        try:
            with Session(engine) as db, db.begin():
                job = locked_job(db, lease, allow_cancel=True)
                if job.cancel_requested or code in {"AUTHORIZATION_CHANGED", "REVISION_CONFLICT"}:
                    cancel(db, lease)
                else:
                    retryable = code in {
                        "DEPENDENCY_UNAVAILABLE", "MODEL_UNAVAILABLE", "DEADLINE_EXCEEDED",
                    }
                    fail(db, lease, code, retryable=retryable)
        except DomainError:
            logger.warning("worker_result_fenced job_id=%s", lease.job_id)
    return True
