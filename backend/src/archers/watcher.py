"""Polling filesystem adapter. No source bytes are ever modified by scanning."""

import hashlib
import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from archers.config import Settings
from archers.documents import EXTENSIONS, discard_snapshot, ensure_queue_capacity, snapshot_file
from archers.domain import DomainError
from archers.dto import FilesystemConfig, ScanPolicy, SourceStatus
from archers.jobs import database_now
from archers.storage import (
    Document,
    DocumentVersion,
    Job,
    Source,
    SourceFile,
    SourceScan,
    SourceScanEvent,
    Tenant,
    aware,
)

logger = logging.getLogger(__name__)
SENTINEL = ".archers-source-id"


class ScanError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@contextmanager
def open_root(settings: Settings, config: FilesystemConfig) -> Iterator[int]:
    path = settings.approved_mounts.get(config.mount_alias)
    if path is None:
        raise ScanError("SOURCE_NOT_APPROVED")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_relative(root: int, relative: str) -> Iterator[BinaryIO]:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {"..", "."} for p in parts):
        raise ScanError("SOURCE_PATH_INVALID")
    descriptor = os.dup(root)
    try:
        for component in parts[:-1]:
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(file_descriptor, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ScanError("SOURCE_PATH_INVALID")
            yield file
    finally:
        os.close(descriptor)


def check_health(
    settings: Settings, config: FilesystemConfig, root: int, now: datetime
) -> None:
    current = os.stat(settings.approved_mounts[config.mount_alias], follow_symlinks=False)
    opened = os.fstat(root)
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise ScanError("SOURCE_IDENTITY_MISMATCH")
    with open_relative(root, SENTINEL) as file:
        if file.read(128).decode("ascii").strip() != str(config.sentinel_id):
            raise ScanError("SOURCE_IDENTITY_MISMATCH")
    if config.mount_alias in settings.network_mounts:
        path = Path(settings.mount_health_files[config.mount_alias])
        if path.stat().st_size > 4096:
            raise ScanError("SOURCE_HEALTH_INVALID")
        record = json.loads(path.read_text())
        observed = datetime.fromisoformat(record["checked_at"])
        if (
            observed.tzinfo is None
            or not -5 <= (now - observed).total_seconds() <= 60
            or record.get("healthy") is not True
            or record.get("sentinel_id") != str(config.sentinel_id)
            or record.get("mount_alias") != config.mount_alias
        ):
            raise ScanError("SOURCE_HEALTH_UNAVAILABLE")


def signature(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def paths(root: int, maximum: int, excluded: set[str]) -> Iterator[str]:
    count = 0

    def walk(directory: int, prefix: str, depth: int) -> Iterator[str]:
        nonlocal count
        if depth > 32:
            raise ScanError("SOURCE_TRAVERSAL_LIMIT")
        # scandir streams entries rather than allocating an unbounded directory listing.
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > maximum:
                    raise ScanError("SOURCE_TRAVERSAL_LIMIT")
                if entry.name.startswith((".", "~")) or entry.is_symlink():
                    continue
                relative = prefix + entry.name
                if relative in excluded:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    try:
                        yield from walk(child, relative + "/", depth + 1)
                    finally:
                        os.close(child)
                elif entry.is_file(follow_symlinks=False):
                    if len(relative.encode()) > 1024:
                        raise ScanError("SOURCE_PATH_LIMIT")
                    yield relative

    yield from walk(root, "", 0)


def read_hash(file: BinaryIO, limit: int) -> tuple[str, str, int]:
    before = signature(os.fstat(file.fileno()))
    digest = hashlib.sha256()
    size = 0
    while block := file.read(65536):
        size += len(block)
        if size > limit:
            raise ScanError("SOURCE_FILE_LIMIT")
        digest.update(block)
    if signature(os.fstat(file.fileno())) != before:
        raise ScanError("SOURCE_FILE_CHANGING")
    return digest.hexdigest(), before, size


def enqueue(
    db: Session, settings: Settings, source: Source, item: SourceFile, root: int,
    policy: ScanPolicy,
) -> None:
    ensure_queue_capacity(db, source.tenant_id, settings.max_pending_ingestion_jobs)
    document = db.get(Document, item.document_id) if item.document_id else None
    if document and document.availability in {"SUPPRESSED", "DELETED", "SOURCE_DELETED"}:
        item.error_code = "DOCUMENT_WITHDRAWN"
        return
    document_id = document.id if document else uuid4()
    version_id = uuid4()
    with open_relative(root, item.item_key) as file:
        before = signature(os.fstat(file.fileno()))
        snapshot = snapshot_file(
            settings, source.tenant_id, document_id, version_id, Path(item.item_key).name, file
        )
        after = signature(os.fstat(file.fileno()))
    with open_relative(root, item.item_key) as current:
        current_stamp = signature(os.fstat(current.fileno()))
    if (
        before != item.signature or after != before or current_stamp != before
        or snapshot.hash != item.observed_hash
    ):
        discard_snapshot(settings, snapshot)
        raise ScanError("SOURCE_FILE_CHANGING")
    if policy.completion_mode == "ready_manifest":
        try:
            with open_relative(root, item.item_key + ".ready.json") as manifest:
                record = json.loads(manifest.read(4097))
            if record != {"sha256": snapshot.hash, "bytes": snapshot.size}:
                raise ScanError("SOURCE_MANIFEST_INVALID")
        except (OSError, ValueError, ScanError):
            discard_snapshot(settings, snapshot)
            raise ScanError("SOURCE_MANIFEST_INVALID") from None
    if document is None:
        document = Document(
            id=document_id, tenant_id=source.tenant_id, title=Path(item.item_key).name,
            origin="nas", format=snapshot.format, export_policy=source.export_policy,
        )
        db.add(document)
        db.flush()
    else:
        document.desired_sequence += 1
        document.revision += 1
        previous = select(DocumentVersion.id).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.tenant_id == source.tenant_id,
        )
        db.execute(
            update(Job).where(
                Job.tenant_id == source.tenant_id, Job.version_id.in_(previous),
                Job.status.in_(["PENDING", "RUNNING", "RETRY_WAIT"]),
            ).values(cancel_requested=True)
        )
    version = DocumentVersion(
        id=version_id, tenant_id=source.tenant_id, document_id=document.id,
        sequence=document.desired_sequence, source_hash=snapshot.hash, bytes=snapshot.size,
        format=snapshot.format, mime=snapshot.mime, original_locator=snapshot.locator,
    )
    db.add(version)
    db.flush()
    db.add(Job(
        tenant_id=source.tenant_id, version_id=version.id, stage="RECEIVED",
        payload={
            "document_id": str(document.id), "version_id": str(version.id),
            "desired_sequence": version.sequence, "source_id": str(source.id),
            "source_revision": source.revision, "source_file_id": str(item.id),
        },
    ))
    item.document_id = document.id
    item.accepted_hash = snapshot.hash
    item.error_code = None


def scan_source(db: Session, settings: Settings, source: Source, scan: SourceScan) -> None:
    now = database_now(db)
    config = FilesystemConfig.model_validate(source.config)
    policy = ScanPolicy.model_validate(source.scan_policy)
    if policy.completion_mode == "remote_revision":
        raise ScanError("SOURCE_COMPLETION_UNSUPPORTED")
    total = 0
    observed: list[tuple[str, str, str, bool, str | None]] = []
    inventory = {item.item_key: item for item in db.scalars(select(SourceFile).where(
        SourceFile.tenant_id == source.tenant_id, SourceFile.source_id == source.id,
    ))}
    deferred = False
    with open_root(settings, config) as root:
        check_health(settings, config, root, now)
        source_root = Path(settings.approved_mounts[config.mount_alias]).resolve()
        excluded: set[str] = set()
        for private in (settings.artifact_root, settings.model_path):
            private = private.resolve()
            if source_root.is_relative_to(private):
                raise ScanError("SOURCE_PATH_PRIVATE")
            if private.is_relative_to(source_root):
                excluded.add(private.relative_to(source_root).as_posix())
        for relative in paths(root, settings.scan_max_entries, excluded):
            extension = Path(relative).suffix.casefold()
            if extension not in EXTENSIONS or EXTENSIONS[extension][0] not in source.formats:
                continue
            try:
                with open_relative(root, relative) as file:
                    info = os.fstat(file.fileno())
                    previous = inventory.get(relative)
                    if info.st_size > settings.max_upload_bytes:
                        observed.append((
                            relative, previous.observed_hash if previous else "0" * 64,
                            signature(info), False, "SOURCE_FILE_LIMIT",
                        ))
                        continue
                    if (
                        previous is not None and previous.accepted_hash == previous.observed_hash
                        and previous.signature == signature(info)
                        and previous.source_revision == source.revision
                        and now - aware(previous.last_hashed_at) < timedelta(days=1)
                    ):
                        observed.append((
                            relative, previous.observed_hash, previous.signature, False, None,
                        ))
                        continue
                    if total + info.st_size > settings.scan_max_bytes:
                        deferred = True
                        continue
                    digest, stamp, size = read_hash(file, settings.max_upload_bytes)
            except ScanError as exc:
                if exc.code == "SOURCE_FILE_CHANGING":
                    continue
                raise
            total += size
            if total > settings.scan_max_bytes:
                raise ScanError("SOURCE_SCAN_BYTE_LIMIT")
            observed.append((relative, digest, stamp, True, None))
        check_health(settings, config, root, database_now(db))
        scan.observed_count = len(observed)
        for relative, digest, stamp, hashed, error in observed:
            item = inventory.get(relative)
            if item is None:
                item = SourceFile(
                    tenant_id=source.tenant_id, source_id=source.id, item_key=relative,
                    source_revision=source.revision, signature=stamp, observed_hash=digest,
                    stable_since=now, observations=1, last_seen_at=now, last_hashed_at=now,
                )
                db.add(item)
            elif (
                item.signature != stamp or item.observed_hash != digest
                or item.source_revision != source.revision
                or (now - aware(item.last_seen_at)).total_seconds() > max(policy.poll_seconds * 3, 90)
            ):
                if item.source_revision != source.revision:
                    item.accepted_hash = None
                item.signature, item.observed_hash = stamp, digest
                item.source_revision = source.revision
                item.stable_since, item.observations = now, 1
                item.last_seen_at = now
            else:
                item.observations += 1
                item.last_seen_at = now
            if hashed:
                item.last_hashed_at = now
            if error:
                item.error_code = error
                continue
            if item.error_code == "SOURCE_FILE_LIMIT":
                item.error_code = None
            if (
                item.accepted_hash != digest and item.observations >= 3
                and (now - aware(item.stable_since)).total_seconds() >= max(60, policy.stable_seconds)
            ):
                db.flush()
                try:
                    enqueue(db, settings, source, item, root, policy)
                except (DomainError, ScanError) as exc:
                    item.error_code = exc.code
                    logger.warning("source_item_deferred source_id=%s code=%s", source.id, exc.code)
        check_health(settings, config, root, database_now(db))
    scan.last_healthy_at = now
    scan.error_code = "SOURCE_SCAN_BUDGET_DEFERRED" if deferred else None


def watch_once(engine: Engine, settings: Settings) -> int:
    with Session(engine) as db:
        source_ids = list(db.scalars(
            select(Source.id).where(Source.enabled.is_(True), Source.connector_kind == "filesystem")
        ))
    scanned = 0
    for source_id in source_ids:
        try:
            with Session(engine) as db, db.begin():
                tenant_id = db.scalar(select(Source.tenant_id).where(Source.id == source_id))
                # Same lock order as API mutations. One bounded scan per tenant at a time.
                tenant = db.scalar(
                    select(Tenant).where(Tenant.id == tenant_id).with_for_update(skip_locked=True)
                )
                if tenant is None:
                    continue
                source = db.scalar(select(Source).where(
                    Source.id == source_id, Source.enabled.is_(True)
                ).with_for_update())
                if source is None:
                    continue
                now = database_now(db)
                scan = db.get(SourceScan, source.id)
                if scan is not None and aware(scan.next_scan_at) > now:
                    continue
                if scan is None:
                    scan = SourceScan(source_id=source.id, tenant_id=source.tenant_id)
                    db.add(scan)
                scan.observed_at = now
                scan.next_scan_at = now + timedelta(seconds=int(source.scan_policy["poll_seconds"]))
                try:
                    with db.begin_nested():
                        scan_source(db, settings, source, scan)
                except (
                    OSError, ScanError, ValueError, UnicodeError, KeyError, TypeError,
                    ValidationError,
                ) as exc:
                    scan.error_code = exc.code if isinstance(exc, ScanError) else "SOURCE_UNAVAILABLE"
                    # Reset observations after outages; old evidence remains active.
                    db.execute(update(SourceFile).where(
                        SourceFile.source_id == source.id,
                        SourceFile.tenant_id == source.tenant_id,
                    ).values(observations=1, stable_since=now))
                    logger.warning("source_scan_failed source_id=%s code=%s", source.id, scan.error_code)
                db.add(SourceScanEvent(
                    tenant_id=source.tenant_id, source_id=source.id, started_at=now,
                    completed_at=database_now(db),
                    healthy=scan.error_code in {None, "SOURCE_SCAN_BUDGET_DEFERRED"},
                    complete=scan.error_code is None, error_code=scan.error_code,
                    observed_count=scan.observed_count,
                ))
                scanned += 1
        except OSError:
            logger.error("source_storage_failed source_id=%s", source_id)
            raise
    return scanned


def source_status(db: Session, tenant_id: UUID, source_id: UUID) -> SourceStatus:
    source = db.scalar(select(Source).where(
        Source.id == source_id, Source.tenant_id == tenant_id
    ))
    if source is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    scan = db.get(SourceScan, source.id)
    query = select(func.count(SourceFile.id)).where(
        SourceFile.tenant_id == tenant_id, SourceFile.source_id == source_id
    )
    tracked = db.scalar(query) or 0
    backlog = db.scalar(query.where(
        (SourceFile.accepted_hash.is_(None)) | (SourceFile.accepted_hash != SourceFile.observed_hash)
    )) or 0
    errors = db.scalar(query.where(SourceFile.error_code.is_not(None))) or 0
    missing = 0
    if scan and scan.last_healthy_at:
        missing = db.scalar(query.where(SourceFile.last_seen_at < scan.last_healthy_at)) or 0
    health: str = "unknown"
    code = scan.error_code if scan else None
    if not source.enabled:
        health = "paused"
    elif scan:
        if code == "SOURCE_SCAN_BUDGET_DEFERRED":
            health = "degraded"
        elif code:
            health = "unavailable"
        elif (
            scan.last_healthy_at is None
            or database_now(db) - aware(scan.last_healthy_at)
            > timedelta(seconds=max(90, int(source.scan_policy["poll_seconds"]) * 3))
        ):
            health, code = "degraded", "SOURCE_SCAN_STALE"
        else:
            health = "degraded" if errors else "healthy"
    return SourceStatus.model_validate({
        "source_id": source_id, "health": health,
        "last_healthy_scan_at": scan.last_healthy_at if scan else None,
        "tracked_files": tracked, "snapshot_backlog": backlog, "missing_files": missing,
        "error_code": code, "file_errors": errors,
    })
