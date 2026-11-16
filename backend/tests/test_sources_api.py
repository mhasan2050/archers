from uuid import uuid4

from conftest import login, mutation_headers
from fastapi.testclient import TestClient


def source_body() -> dict[str, object]:
    return {
        "connector": {
            "kind": "filesystem",
            "mount_alias": "synthetic",
            "sentinel_id": str(uuid4()),
        },
        "formats": ["pdf", "text"],
        "enabled": True,
        "scan_policy": {
            "poll_seconds": 30,
            "stable_seconds": 60,
            "deletion_grace_seconds": 300,
            "completion_mode": "heuristic_double_hash",
        },
    }


def test_source_creation_defaults_export_denied(client: TestClient) -> None:
    session = login(client)
    response = client.post(
        "/api/v1/admin/sources",
        json=source_body(),
        headers=mutation_headers(session, **{"Idempotency-Key": str(uuid4())}),
    )
    assert response.status_code == 201, response.text
    source = response.json()
    assert source["export_policy"] == {
        "allow_external_generation": False,
        "allow_external_embeddings": False,
    }
    assert client.get("/api/v1/admin/sources").json()["items"] == [source]
    patched = client.patch(
        f"/api/v1/admin/sources/{source['id']}",
        json={"enabled": False},
        headers=mutation_headers(session, **{"If-Match": '"1"'}),
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False


def test_source_rejects_unapproved_mount_export_and_remote_mode(client: TestClient) -> None:
    session = login(client)
    body = source_body()
    for change in [
        {
            "connector": {
                "kind": "filesystem",
                "mount_alias": "outside",
                "sentinel_id": str(uuid4()),
            }
        },
        {"export_policy": {"allow_external_generation": True, "allow_external_embeddings": False}},
        {
            "scan_policy": {
                "poll_seconds": 30,
                "stable_seconds": 60,
                "deletion_grace_seconds": 300,
                "completion_mode": "remote_revision",
            }
        },
    ]:
        response = client.post(
            "/api/v1/admin/sources",
            json={**body, **change},
            headers=mutation_headers(session, **{"Idempotency-Key": str(uuid4())}),
        )
        assert response.status_code in {403, 422}


def test_signed_cursor_bound_to_requester_and_route(client: TestClient) -> None:
    session = login(client)
    for name in ["first", "second"]:
        response = client.post(
            "/api/v1/admin/users",
            json={"username": name, "password": "synthetic-password-123", "role": "reader"},
            headers=mutation_headers(session, **{"Idempotency-Key": str(uuid4())}),
        )
        assert response.status_code == 201
    page = client.get("/api/v1/admin/users?limit=1").json()
    cursor = page["next_cursor"]
    assert cursor is not None
    assert (
        client.get("/api/v1/admin/users", params={"cursor": cursor, "limit": 1}).status_code == 200
    )
    assert client.get("/api/v1/admin/sources", params={"cursor": cursor}).status_code == 422
    assert client.get("/api/v1/admin/users", params={"cursor": cursor + "x"}).status_code == 422


def test_unimplemented_routes_do_not_claim_success(client: TestClient) -> None:
    login(client)
    assert client.get("/api/v1/conversations").status_code == 404
