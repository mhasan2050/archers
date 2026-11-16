import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import login, mutation_headers
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from archers import watcher
from archers.storage import Document, DocumentVersion, Job, Source, SourceFile, SourceScan, now_utc
from archers.watcher import SENTINEL, watch_once


def register(client: TestClient) -> tuple[UUID, Path]:
    session = login(client)
    root = Path(client.app.state.settings.approved_mounts["synthetic"])
    sentinel = uuid4()
    (root / SENTINEL).write_text(str(sentinel))
    response = client.post(
        "/api/v1/admin/sources",
        headers=mutation_headers(session, **{"Idempotency-Key": str(uuid4())}),
        json={
            "connector": {
                "kind": "filesystem", "mount_alias": "synthetic", "sentinel_id": str(sentinel),
            },
            "formats": ["text", "pdf"],
            "enabled": True,
            "scan_policy": {
                "poll_seconds": 30, "stable_seconds": 60,
                "deletion_grace_seconds": 300, "completion_mode": "heuristic_double_hash",
            },
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"]), root


def test_stability_restart_duplicate_and_replacement(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id, root = register(client)
    file = root / "policy.txt"
    file.write_text("Synthetic company leave policy.")
    engine, settings = client.app.state.database, client.app.state.settings
    start = now_utc()
    for offset in (0, 30, 59, 60):
        monkeypatch.setattr(watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset))
        watch_once(engine, settings)
        with Session(engine) as db:
            count = db.scalar(select(func.count(Job.id)))
            assert count == (1 if offset >= 60 else 0)
    for offset in (90, 120):
        monkeypatch.setattr(watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset))
        watch_once(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
        version = db.scalar(select(DocumentVersion))
        assert version is not None
        assert (settings.artifact_root / version.original_locator).read_text() == file.read_text()
        document_id = version.document_id
    # Same size and restored mtime still changes the hash and resets stability.
    old_stat = file.stat()
    file.write_text("Synthetic company sick policy.")
    os.utime(file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    for offset in (150, 180, 210):
        monkeypatch.setattr(watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset))
        watch_once(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count(Document.id))) == 1
        assert db.scalar(select(func.count(DocumentVersion.id))) == 2
        document = db.get(Document, document_id)
        assert document is not None and document.desired_sequence == 2
        jobs = list(db.scalars(select(Job).order_by(Job.created_at)))
        assert jobs[0].cancel_requested
        assert not jobs[1].cancel_requested
        assert db.get(SourceScan, source_id).error_code is None


def test_partial_symlinks_outage_and_missing_do_not_delete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id, root = register(client)
    (root / "valid.txt").write_text("Synthetic safe document.")
    (root / ".hidden.txt").write_text("not ingested")
    (root / "copy.txt.part").write_text("not ingested")
    outside = root.parent / ("outside-" + uuid4().hex)
    outside.mkdir()
    (outside / "private.txt").write_text("not ingested")
    (root / "symlink.txt").symlink_to(outside / "private.txt")
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    engine, settings = client.app.state.database, client.app.state.settings
    start = now_utc()
    for offset in (0, 30, 60):
        monkeypatch.setattr(watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset))
        watch_once(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
    (root / SENTINEL).unlink()
    monkeypatch.setattr(watcher, "database_now", lambda db: start + timedelta(seconds=90))
    watch_once(engine, settings)
    status = client.get(f"/api/v1/sources/{source_id}/status")
    assert status.status_code == 200
    assert status.json()["health"] == "unavailable"
    assert status.json()["deletion_frozen"] is True
    with Session(engine) as db:
        assert db.scalar(select(func.count(DocumentVersion.id))) == 1
        source = db.get(Source, source_id)
        sentinel_id = source.config["sentinel_id"]
    (root / SENTINEL).write_text(sentinel_id)
    (root / "valid.txt").unlink()
    monkeypatch.setattr(watcher, "database_now", lambda db: start + timedelta(seconds=120))
    watch_once(engine, settings)
    assert client.get(f"/api/v1/sources/{source_id}/status").json()["missing_files"] == 1
    with Session(engine) as db:
        assert db.scalar(select(Document.availability)) == "PENDING"
    login(client, "reader")
    assert client.get(f"/api/v1/sources/{source_id}/status").status_code == 403
    (outside / "private.txt").unlink()
    outside.rmdir()


def test_unstable_files_reset_observations_and_queue_full_is_visible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id, root = register(client)
    engine = client.app.state.database
    settings = client.app.state.settings.model_copy(update={"max_pending_ingestion_jobs": 1})
    a, b = root / "a.txt", root / "b.txt"
    a.write_text("First synthetic document.")
    b.write_text("Second synthetic document.")
    start = now_utc()
    for offset in (0, 30):
        monkeypatch.setattr(watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset))
        watch_once(engine, settings)
    a.write_text("First synthetic document changed.")
    for offset in (60, 90, 120):
        monkeypatch.setattr(watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset))
        watch_once(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
        assert db.scalar(select(SourceFile.error_code).where(SourceFile.item_key == "a.txt")) == "RATE_LIMITED"
    status = client.get(f"/api/v1/sources/{source_id}/status").json()
    assert status["snapshot_backlog"] == 1 and status["file_errors"] == 1


def test_network_source_requires_fresh_host_health(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id, root = register(client)
    (root / "document.txt").write_text("Synthetic NAS data.")
    settings = client.app.state.settings.model_copy(update={
        "network_mounts": ["synthetic"],
        "mount_health_files": {"synthetic": str(root.parent / "missing-health.json")},
    })
    watch_once(client.app.state.database, settings)
    status = client.get(f"/api/v1/sources/{source_id}/status").json()
    assert status["health"] == "unavailable"
    with Session(client.app.state.database) as db:
        assert db.scalar(select(func.count(Job.id))) == 0


def test_ready_manifest_and_oversized_file_do_not_block_other_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id, root = register(client)
    engine = client.app.state.database
    settings = client.app.state.settings.model_copy(update={"max_upload_bytes": 64})
    content = b"Synthetic policy with a completion manifest."
    (root / "ready.txt").write_bytes(content)
    (root / "too-big.txt").write_bytes(b"x" * 65)
    with Session(engine) as db, db.begin():
        source = db.get(Source, source_id)
        source.scan_policy = {**source.scan_policy, "completion_mode": "ready_manifest"}
    start = now_utc()
    for offset in (0, 30, 60):
        monkeypatch.setattr(
            watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset)
        )
        watch_once(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 0
        assert db.scalar(select(SourceFile.error_code).where(
            SourceFile.item_key == "ready.txt"
        )) == "SOURCE_MANIFEST_INVALID"
        assert db.scalar(select(SourceFile.error_code).where(
            SourceFile.item_key == "too-big.txt"
        )) == "SOURCE_FILE_LIMIT"
    (root / "ready.txt.ready.json").write_text(json.dumps({
        "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
    }))
    monkeypatch.setattr(watcher, "database_now", lambda db: start + timedelta(seconds=90))
    watch_once(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
