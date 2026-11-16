from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import login, mutation_headers
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from archers import worker
from archers.domain import DomainError
from archers.ingestion.normalization import NormalizedDocument
from archers.storage import ChunkRecord, Document, DocumentVersion, IndexGeneration, Job


@dataclass(frozen=True)
class FakeChunk:
    text: str
    ordinal: int
    token_count: int
    locations: list[dict[str, object]]


class FakeEncoder:
    fingerprint = "synthetic-encoder-for-tests"

    def chunk(self, document: NormalizedDocument) -> list[FakeChunk]:
        return [
            FakeChunk(b.text, i, 20, [{"kind": "lines", "line_start": 1, "line_end": 1}])
            for i, b in enumerate(document.blocks)
        ]

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]

    def sparse(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        return [([1], [1.0]) for _ in texts]


class FakeIndex:
    collection = "synthetic-test-collection"

    def stage(self, *args: object) -> None:
        pass


def upload(client: TestClient, content: bytes = b"Synthetic company leave policy.") -> dict[str, str]:
    session = login(client)
    response = client.post(
        "/api/v1/documents",
        headers=mutation_headers(session, **{"Idempotency-Key": str(uuid4())}),
        data={"title": "Synthetic document"},
        files={"file": ("policy.txt", content, "text/plain")},
    )
    assert response.status_code == 202
    return response.json()


def test_worker_real_parser_artifacts_and_atomic_activation(client: TestClient) -> None:
    result = upload(client)
    engine, settings = client.app.state.database, client.app.state.settings
    assert worker.work_once(engine, settings, FakeEncoder(), FakeIndex(), "unit-worker")
    with Session(engine) as db:
        job = db.get(Job, UUID(result["job_id"]))
        assert job.status == "SUCCEEDED" and job.stage == "READY"
        document = db.get(Document, UUID(result["document_id"]))
        assert document.availability == "ACTIVE"
        generation = db.get(IndexGeneration, document.active_generation_id)
        assert generation is not None and generation.state == "ACTIVE"
        assert generation.version_id == document.active_version_id
        chunk = db.scalar(select(ChunkRecord))
        assert chunk is not None
        assert (settings.artifact_root / chunk.text_locator).is_file()
        assert chunk.generation_id == generation.id
    login(client, "reader")
    assert client.get("/api/v1/documents").json()["items"][0]["id"] == result["document_id"]
    assert not worker.work_once(engine, settings, FakeEncoder(), FakeIndex(), "unit-worker")


def test_failed_replacement_preserves_active_version(client: TestClient) -> None:
    result = upload(client)
    engine, settings = client.app.state.database, client.app.state.settings
    worker.work_once(engine, settings, FakeEncoder(), FakeIndex(), "unit-worker")
    session = login(client)
    document = client.get(f"/api/v1/documents/{result['document_id']}").json()
    replacement = client.post(
        f"/api/v1/documents/{result['document_id']}/versions",
        headers=mutation_headers(
            session, **{"Idempotency-Key": str(uuid4()), "If-Match": f'"{document["revision"]}"'}
        ),
        data={"title": "Replacement"}, files={"file": ("policy.txt", b"New synthetic policy.")},
    ).json()

    class BrokenIndex(FakeIndex):
        def stage(self, *args: object) -> None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Synthetic index fault.", 503)

    worker.work_once(engine, settings, FakeEncoder(), BrokenIndex(), "unit-worker")
    with Session(engine) as db:
        document = db.get(Document, UUID(result["document_id"]))
        assert str(document.active_version_id) == result["version_id"]
        assert document.availability == "ACTIVE"
        job = db.get(Job, UUID(replacement["job_id"]))
        assert job.status == "RETRY_WAIT"


def test_cancellation_between_indexing_and_publish_is_fenced(client: TestClient) -> None:
    result = upload(client)
    engine, settings = client.app.state.database, client.app.state.settings

    class CancelIndex(FakeIndex):
        def stage(self, *args: object) -> None:
            with Session(engine) as db, db.begin():
                db.get(Job, UUID(result["job_id"])).cancel_requested = True

    worker.work_once(engine, settings, FakeEncoder(), CancelIndex(), "unit-worker")
    with Session(engine) as db:
        job = db.get(Job, UUID(result["job_id"]))
        assert job.status == "CANCELLED"
        assert db.get(Document, UUID(result["document_id"])).active_version_id is None


def test_integrity_failure_and_quality_warning_never_publish(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = upload(client)
    engine, settings = client.app.state.database, client.app.state.settings
    with Session(engine) as db:
        version = db.get(DocumentVersion, UUID(result["version_id"]))
        path: Path = settings.artifact_root / version.original_locator
    path.write_text("Synthetic integrity mismatch.")
    worker.work_once(engine, settings, FakeEncoder(), FakeIndex(), "unit-worker")
    with Session(engine) as db:
        assert db.get(Job, UUID(result["job_id"])).status == "FAILED"
        assert db.get(Document, UUID(result["document_id"])).active_version_id is None
    empty = upload(client, b"   \n")
    worker.work_once(engine, settings, FakeEncoder(), FakeIndex(), "unit-worker")
    with Session(engine) as db:
        version = db.get(DocumentVersion, UUID(empty["version_id"]))
        assert version.state == "REVIEW_REQUIRED"
        assert version.quality_report["empty"]
