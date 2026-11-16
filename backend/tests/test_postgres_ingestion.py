"""Run with ARCHERS_TEST_DATABASE_URL against an explicitly isolated PostgreSQL."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from alembic import command
from conftest import login, mutation_headers
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from archers.api import check_schema, create_app
from archers.cli import migration_config
from archers.config import Settings
from archers.domain import DomainError
from archers.identity import hash_password
from archers.jobs import claim, fail, heartbeat
from archers.storage import Job, Source, Tenant, User, make_engine, now_utc


@pytest.fixture
def postgres_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = os.environ.get("ARCHERS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set ARCHERS_TEST_DATABASE_URL to run real PostgreSQL integration tests.")
    if not make_url(url).database or not str(make_url(url).database).endswith("_validation"):
        pytest.fail("Integration database name must end with _validation.")
    admin = make_engine(url)
    schema = "test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    settings = Settings(
        database_url=scoped.render_as_string(hide_password=False),
        operator_token="synthetic-operator-token-for-local-validation-only",
        signing_key="synthetic-signing-key-for-local-validation-only",
        artifact_root=tmp_path / "artifacts",
        approved_mounts={"synthetic": str(tmp_path)},
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "database_url": settings.database_url.get_secret_value(),
                "operator_token": settings.operator_token.get_secret_value(),
                "signing_key": settings.signing_key.get_secret_value(),
            }
        )
    )
    monkeypatch.setenv("ARCHERS_CONFIG_FILE", str(config_file))
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        command.upgrade(migration_config(), "0001_identity_sources")
        tenant_id, admin_id, source_id = uuid4(), uuid4(), uuid4()
        with Session(engine) as db, db.begin():
            db.add(Tenant(id=tenant_id, name="Synthetic migration company"))
            db.flush()
            db.add(
                User(
                    id=admin_id,
                    tenant_id=tenant_id,
                    username="admin",
                    password_hash=hash_password("synthetic-password-123"),
                    role="admin",
                )
            )
            db.add(
                Source(
                    id=source_id,
                    tenant_id=tenant_id,
                    connector_kind="filesystem",
                    config={
                        "kind": "filesystem",
                        "mount_alias": "synthetic",
                        "sentinel_id": str(uuid4()),
                    },
                    formats=["text"],
                    scan_policy={
                        "poll_seconds": 30,
                        "stable_seconds": 60,
                        "deletion_grace_seconds": 300,
                        "completion_mode": "heuristic_double_hash",
                    },
                    export_policy={
                        "allow_external_generation": False,
                        "allow_external_embeddings": False,
                    },
                    enabled=True,
                )
            )
        command.upgrade(migration_config(), "head")
        command.upgrade(migration_config(), "head")
        check_schema(engine)
        with Session(engine) as db:
            assert db.get(User, admin_id) is not None
            assert db.get(Source, source_id) is not None
        with TestClient(
            create_app(settings, engine=engine), base_url="http://localhost:8080"
        ) as client:
            yield client
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def submit(client: TestClient, session: dict[str, object], key: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/documents",
        headers=mutation_headers(session, **{"Idempotency-Key": key}),
        data={"title": "Synthetic document"},
        files={"file": ("policy.txt", b"Synthetic policy only.", "text/plain")},
    )
    assert response.status_code == 202, response.text
    result: dict[str, str] = response.json()
    return result


def test_postgres_upload_restart_idempotency_and_skip_locked(postgres_client: TestClient) -> None:
    client = postgres_client
    session = login(client)
    accepted = submit(client, session, "upload")
    assert submit(client, session, "upload") == accepted
    engine = client.app.state.database
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
    # Hold the first transaction's row lock: another replica must skip it.
    with Session(engine) as db, db.begin():
        lease = claim(db, "worker-one")
        assert lease is not None

        def other_replica() -> object:
            with Session(engine) as other, other.begin():
                return claim(other, "worker-two")

        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(other_replica).result(timeout=5) is None
    # Closing the DB session and rebuilding the app does not lose durable state.
    with TestClient(
        create_app(client.app.state.settings, engine=engine), base_url="http://localhost:8080"
    ) as restarted:
        restarted.cookies.update(client.cookies)
        response = restarted.get(f"/api/v1/jobs/{accepted['job_id']}")
        assert response.json()["status"] == "RUNNING"
    with Session(engine) as db, db.begin():
        job = db.get(Job, UUID(accepted["job_id"]))
        assert job is not None
        job.lease_expires_at = now_utc() - timedelta(seconds=1)
    with Session(engine) as db, db.begin():
        next_lease = claim(db, "worker-two")
        assert next_lease is not None and next_lease.fence == lease.fence + 1
    with Session(engine) as db, db.begin():
        with pytest.raises(DomainError):
            heartbeat(db, lease)
    with Session(engine) as db, db.begin():
        fail(db, next_lease, "VALIDATION_ERROR", retryable=False)
    retry = client.post(
        f"/api/v1/jobs/{accepted['job_id']}/retry",
        headers=mutation_headers(session, **{"Idempotency-Key": "retry"}),
    )
    assert retry.status_code == 409


def test_postgres_concurrent_upload_same_key_creates_one_job(postgres_client: TestClient) -> None:
    client = postgres_client
    session = login(client)
    cookies = dict(client.cookies)

    def parallel_upload() -> dict[str, str]:
        with TestClient(
            create_app(client.app.state.settings, engine=client.app.state.database),
            base_url="http://localhost:8080",
        ) as replica:
            replica.cookies.update(cookies)
            return submit(replica, session, "concurrent-upload")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(parallel_upload) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with Session(client.app.state.database) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
    artifacts = list(client.app.state.settings.artifact_root.rglob("original.txt"))
    assert len(artifacts) == 1


def test_postgres_concurrent_scanners_create_one_version(
    postgres_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from archers import watcher
    from archers.storage import DocumentVersion, SourceScanEvent

    client = postgres_client
    engine, settings = client.app.state.database, client.app.state.settings
    root = Path(settings.approved_mounts["synthetic"])
    with Session(engine) as db:
        source = db.scalar(select(Source))
        assert source is not None
        sentinel_id = source.config["sentinel_id"]
    (root / watcher.SENTINEL).write_text(sentinel_id)
    (root / "synthetic.txt").write_text("Synthetic leave entitlement is twenty days.")
    start = now_utc() - timedelta(seconds=60)
    for offset in (0, 30, 60):
        monkeypatch.setattr(
            watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset)
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                executor.submit(watcher.watch_once, engine, settings)
                for _ in range(2)
            ]
            assert sum(future.result(timeout=10) for future in results) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
        assert db.scalar(select(func.count(DocumentVersion.id))) == 1
        assert db.scalar(select(func.count(SourceScanEvent.id))) == 3


def test_postgres_real_model_folder_to_qdrant(
    postgres_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = os.environ.get("ARCHERS_TEST_MODEL_PATH")
    vector_url = os.environ.get("ARCHERS_TEST_QDRANT_URL")
    if not model_path or not vector_url:
        pytest.skip("Set explicit local model and Qdrant URL for real indexing validation.")
    from archers import watcher
    from archers.indexing import LocalEncoder, VectorIndex
    from archers.storage import ChunkRecord, Document, IndexGeneration
    from archers.worker import work_once

    client = postgres_client
    engine = client.app.state.database
    settings = client.app.state.settings.model_copy(update={
        "model_path": Path(model_path), "qdrant_url": vector_url,
    })
    root = Path(settings.approved_mounts["synthetic"])
    with Session(engine) as db, db.begin():
        source = db.scalar(select(Source))
        assert source is not None
        source.formats = ["text", "pdf"]
        tenant_id = source.tenant_id
        (root / watcher.SENTINEL).write_text(source.config["sentinel_id"])
    text = "Synthetic policy: employees receive twenty days of paid annual vacation."
    (root / "vacation.txt").write_text(text)
    start = now_utc() - timedelta(seconds=60)
    for offset in (0, 30, 60):
        monkeypatch.setattr(
            watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset)
        )
        watcher.watch_once(engine, settings)
    encoder = LocalEncoder(settings.model_path)
    index = VectorIndex(vector_url, "validation_" + uuid4().hex, encoder.dimension)
    try:
        assert work_once(engine, settings, encoder, index, "real-encoder-validation")
        with Session(engine) as db:
            job = db.scalar(select(Job))
            assert job is not None and job.status == "SUCCEEDED", job.error_code if job else None
            document = db.scalar(select(Document))
            assert document is not None and document.availability == "ACTIVE"
            generation = db.get(IndexGeneration, document.active_generation_id)
            assert generation is not None and generation.state == "ACTIVE"
            assert generation.chunk_count > 0
            document_id, first_version, first_generation = document.id, document.active_version_id, generation.id
        vector = encoder.encode(["What is the annual vacation allowance?"])[0]
        hits = index.search(tenant_id, vector)
        assert hits and hits[0]["payload"]["document_id"] == str(document_id)
        assert "text" not in hits[0]["payload"]
        assert index.search(uuid4(), vector) == []
        (root / "vacation.txt").write_text(
            "Synthetic revised policy: employees receive thirty days of paid annual vacation."
        )
        for offset in (90, 120, 150):
            monkeypatch.setattr(
                watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset)
            )
            watcher.watch_once(engine, settings)
        assert work_once(engine, settings, encoder, index, "replacement-validation")
        with Session(engine) as db:
            document = db.get(Document, document_id)
            assert document is not None and document.availability == "ACTIVE"
            assert document.active_version_id != first_version
            assert document.active_generation_id != first_generation
            assert db.get(IndexGeneration, first_generation).state == "RETIRED"
            assert db.get(IndexGeneration, document.active_generation_id).state == "ACTIVE"
        # Qdrant still contains retired data until cleanup; canonical DB pointers select eligibility.
        payloads = [hit["payload"] for hit in index.search(tenant_id, vector)]
        assert {p["generation_id"] for p in payloads} >= {str(first_generation), str(document.active_generation_id)}
        from test_normalization import _pdf_bytes

        (root / "page.pdf").write_bytes(_pdf_bytes())
        for offset in (180, 210, 240):
            monkeypatch.setattr(
                watcher, "database_now", lambda db, offset=offset: start + timedelta(seconds=offset)
            )
            watcher.watch_once(engine, settings)
        assert work_once(engine, settings, encoder, index, "pdf-validation")
        with Session(engine) as db:
            pdf = db.scalar(select(Document).where(Document.format == "pdf"))
            assert pdf is not None and pdf.availability == "ACTIVE"
            chunk = db.scalar(select(ChunkRecord).where(ChunkRecord.document_id == pdf.id))
            assert chunk is not None and chunk.locations[0]["kind"] == "pdf_page"
            assert chunk.locations[0]["page"] == 1
    finally:
        index.close()
