from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from conftest import login, mutation_headers
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from archers.api import create_app
from archers.domain import DomainError
from archers.jobs import advance, cancel, claim, heartbeat
from archers.storage import Document, DocumentVersion, Job, now_utc


def upload(
    client: TestClient,
    session: dict[str, object],
    *,
    key: str | None = None,
    content: bytes = b"# Synthetic\n\nCompany policy.",
    path: str = "/api/v1/documents",
    extra: dict[str, str] | None = None,
):
    return client.post(
        path,
        files={"file": ("policy.md", content, "text/markdown")},
        data={"title": "Synthetic policy"},
        headers=mutation_headers(
            session, **{"Idempotency-Key": key or str(uuid4()), **(extra or {})}
        ),
    )


def test_upload_has_durable_linkage_and_idempotency(client: TestClient) -> None:
    session = login(client, "curator")
    key = str(uuid4())
    first = upload(client, session, key=key)
    assert first.status_code == 202, first.text
    again = upload(client, session, key=key)
    assert again.json() == first.json()
    assert upload(client, session, key=key, content=b"Different bytes").status_code == 409
    accepted = first.json()
    with Session(client.app.state.database) as db:
        document = db.get(Document, UUID(accepted["document_id"]))
        version = db.get(DocumentVersion, UUID(accepted["version_id"]))
        job = db.get(Job, UUID(accepted["job_id"]))
        assert document and version and job
        assert document.active_version_id is None
        assert document.availability == "PENDING"
        assert job.status == "PENDING"
        assert (
            client.app.state.settings.artifact_root.joinpath(version.original_locator).read_bytes()
            == b"# Synthetic\n\nCompany policy."
        )
        assert "Synthetic policy" not in str(job.payload)
    status = client.get(f"/api/v1/jobs/{accepted['job_id']}")
    assert status.status_code == 200
    assert status.json()["attempts"] == 0


def test_upload_security_type_and_unknown_fields(client: TestClient) -> None:
    reader = login(client, "reader")
    assert upload(client, reader).status_code == 403
    curator = login(client, "curator")
    assert (
        client.post(
            "/api/v1/documents",
            files={"file": ("x.md", b"text")},
            data={"title": "x"},
            headers={"Origin": "http://localhost:8080", "Idempotency-Key": "x"},
        ).status_code
        == 403
    )
    response = client.post(
        "/api/v1/documents",
        files={"file": ("x.exe", b"binary")},
        data={"title": "x"},
        headers=mutation_headers(curator, **{"Idempotency-Key": "x"}),
    )
    assert response.status_code == 422
    response = client.post(
        "/api/v1/documents",
        files={"file": ("x.md", b"content")},
        data={"title": "x", "allow_external_generation": "true"},
        headers=mutation_headers(curator, **{"Idempotency-Key": "x"}),
    )
    assert response.status_code == 422
    assert upload(client, curator, content=b"%PDF-1.7\n", key="mismatch").status_code == 422


def test_replacement_revision_and_reader_pending_visibility(client: TestClient) -> None:
    session = login(client, "curator")
    original = upload(client, session).json()
    document_id = original["document_id"]
    replaced = upload(
        client,
        session,
        path=f"/api/v1/documents/{document_id}/versions",
        extra={"If-Match": '"1"'},
    )
    assert replaced.status_code == 202, replaced.text
    stale = upload(
        client, session, path=f"/api/v1/documents/{document_id}/versions", extra={"If-Match": '"1"'}
    )
    assert stale.status_code == 409
    login(client, "reader")
    assert client.get(f"/api/v1/documents/{document_id}").status_code == 404
    assert client.get("/api/v1/documents").json()["items"] == []
    assert client.get(f"/api/v1/jobs/{original['job_id']}").status_code == 403


def test_cancel_request_is_distinct_from_completion(client: TestClient) -> None:
    session = login(client, "curator")
    accepted = upload(client, session).json()
    response = client.post(
        f"/api/v1/jobs/{accepted['job_id']}/cancel", headers=mutation_headers(session)
    )
    assert response.status_code == 202
    assert response.json()["cancel_requested"] is True
    assert response.json()["status"] == "PENDING"
    with Session(client.app.state.database) as db, db.begin():
        assert claim(db, "worker-1") is None
    assert client.get(f"/api/v1/jobs/{accepted['job_id']}").json()["status"] == "CANCELLED"


def test_expired_lease_and_fence_prevent_stale_writes(client: TestClient) -> None:
    session = login(client, "curator")
    accepted = upload(client, session).json()
    engine = client.app.state.database
    with Session(engine) as db, db.begin():
        lease = claim(db, "worker-1")
        assert lease is not None and lease.attempt == 1
    with Session(engine) as db, db.begin():
        job = db.get(Job, UUID(accepted["job_id"]))
        assert job is not None
        job.lease_expires_at = now_utc() - timedelta(seconds=1)
    with Session(engine) as db, db.begin():
        with pytest.raises(DomainError, match="Lease"):
            heartbeat(db, lease)
    with Session(engine) as db, db.begin():
        next_lease = claim(db, "worker-2")
        assert next_lease is not None and next_lease.fence > lease.fence
        assert next_lease.attempt == 2
    with Session(engine) as db, db.begin():
        with pytest.raises(DomainError):
            advance(db, lease, "EXTRACTING")
    with Session(engine) as db, db.begin():
        heartbeat(db, next_lease)
        advance(db, next_lease, "VALIDATING")
    with Session(engine) as db, db.begin():
        cancel(db, next_lease)
    job = client.get(f"/api/v1/jobs/{accepted['job_id']}").json()
    assert job["status"] == "CANCELLED"


def test_retry_only_retryable_failure_is_idempotent(client: TestClient) -> None:
    from archers.jobs import fail

    session = login(client, "curator")
    accepted = upload(client, session).json()
    engine = client.app.state.database
    with Session(engine) as db, db.begin():
        lease = claim(db, "worker")
        assert lease is not None
        fail(db, lease, "DEPENDENCY_UNAVAILABLE", retryable=True)
    # Automatic retry wait cannot be manually replayed while queued.
    response = client.post(
        f"/api/v1/jobs/{accepted['job_id']}/retry",
        headers=mutation_headers(session, **{"Idempotency-Key": "retry"}),
    )
    assert response.status_code == 409


def test_manual_retry_of_retryable_terminal_job(client: TestClient) -> None:
    from archers.jobs import fail

    session = login(client, "curator")
    accepted = upload(client, session).json()
    with Session(client.app.state.database) as db, db.begin():
        lease = claim(db, "worker")
        assert lease is not None
        fail(db, lease, "MODEL_UNAVAILABLE", retryable=True, automatic_retry=False)
    path = f"/api/v1/jobs/{accepted['job_id']}/retry"
    headers = mutation_headers(session, **{"Idempotency-Key": "manual-retry"})
    response = client.post(path, headers=headers)
    assert response.status_code == 202
    assert client.post(path, headers=headers).json() == response.json()
    with Session(client.app.state.database) as db, db.begin():
        retried = claim(db, "next-worker")
        assert retried is not None and retried.attempt == 2


def test_queue_backpressure_does_not_break_idempotent_replay(client: TestClient) -> None:
    app = create_app(
        client.app.state.settings.model_copy(update={"max_pending_ingestion_jobs": 1}),
        engine=client.app.state.database,
    )
    with TestClient(app, base_url="http://localhost:8080") as bounded:
        session = login(bounded, "curator")
        first = upload(bounded, session, key="one")
        assert first.status_code == 202
        assert upload(bounded, session, key="one").json() == first.json()
        overflow = upload(bounded, session, key="two")
        assert overflow.status_code == 429
        assert overflow.headers["Retry-After"] == "30"


def test_upload_limits_at_boundary_and_body_cap(client: TestClient) -> None:
    app = create_app(
        client.app.state.settings.model_copy(update={"max_upload_bytes": 4}),
        engine=client.app.state.database,
    )
    with TestClient(app, base_url="http://localhost:8080") as small:
        session = login(small, "curator")
        assert upload(small, session, content=b"abcd").status_code == 202
        assert upload(small, session, content=b"abcde").status_code == 413
        assert upload(small, session, content=b"x" * 9000).status_code == 413


def test_job_stages_cannot_skip_or_ignore_cancel(client: TestClient) -> None:
    session = login(client, "curator")
    accepted = upload(client, session).json()
    with Session(client.app.state.database) as db, db.begin():
        lease = claim(db, "worker")
        assert lease is not None
    with Session(client.app.state.database) as db, db.begin():
        with pytest.raises(DomainError):
            advance(db, lease, "INDEXING")
    client.post(f"/api/v1/jobs/{accepted['job_id']}/cancel", headers=mutation_headers(session))
    with Session(client.app.state.database) as db, db.begin():
        with pytest.raises(DomainError):
            heartbeat(db, lease)


def test_expired_jobs_stop_after_three_attempts(client: TestClient) -> None:
    session = login(client, "curator")
    accepted = upload(client, session).json()
    engine = client.app.state.database
    for attempt in range(1, 4):
        with Session(engine) as db, db.begin():
            lease = claim(db, f"worker-{attempt}")
            assert lease is not None and lease.attempt == attempt
            job = db.get(Job, lease.job_id)
            assert job is not None
            job.lease_expires_at = now_utc() - timedelta(seconds=1)
    with Session(engine) as db, db.begin():
        assert claim(db, "worker-4") is None
    assert client.get(f"/api/v1/jobs/{accepted['job_id']}").json()["status"] == "FAILED"
