from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from archers.api import create_app
from archers.config import Settings
from archers.identity import hash_password
from archers.storage import SCHEMA_REVISION, Base, Tenant, User


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": SCHEMA_REVISION},
        )
    tenant_id = uuid4()
    with Session(engine) as db, db.begin():
        db.add(Tenant(id=tenant_id, name="Synthetic company"))
        db.flush()
        for username, role in [("admin", "admin"), ("curator", "curator"), ("reader", "reader")]:
            db.add(
                User(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    username=username,
                    password_hash=hash_password("synthetic-password-123"),
                    role=role,
                    created_at=datetime.now(UTC),
                )
            )
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        operator_token="o" * 48,
        signing_key="k" * 48,
        approved_mounts={"synthetic": str(tmp_path)},
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url="http://localhost:8080") as test_client:
        yield test_client
    engine.dispose()


def login(client: TestClient, username: str = "admin") -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
        headers={"Origin": "http://localhost:8080"},
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def mutation_headers(session: dict[str, object], **extra: str) -> dict[str, str]:
    return {
        "Origin": "http://localhost:8080",
        "X-CSRF-Token": str(session["csrf_token"]),
        **extra,
    }
