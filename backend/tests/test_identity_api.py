from uuid import uuid4

import pytest
from conftest import login, mutation_headers
from fastapi.testclient import TestClient


def test_anonymous_and_login_cookie(client: TestClient) -> None:
    assert client.get("/api/v1/admin/users").status_code == 401
    session = login(client)
    assert "users.manage" in session["permissions"]  # type: ignore[operator]
    assert client.get("/api/v1/auth/me").status_code == 200
    assert len(client.cookies.get("rag_session", "")) >= 40


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
def test_login_requires_trusted_origin(client: TestClient, origin: str | None) -> None:
    headers = {"Origin": origin} if origin else {}
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "x"}, headers=headers
    )
    assert response.status_code == 403


def test_unknown_host_and_content_type(client: TestClient) -> None:
    assert client.get("/api/v1/health/live", headers={"Host": "evil.example"}).status_code == 400
    assert (
        client.post(
            "/api/v1/auth/login",
            content="{}",
            headers={"Origin": "http://localhost:8080", "Content-Type": "text/plain"},
        ).status_code
        == 415
    )


@pytest.mark.parametrize("role", ["reader", "curator"])
def test_roles_do_not_grant_admin(client: TestClient, role: str) -> None:
    login(client, role)
    for path in ["/api/v1/admin/users", "/api/v1/admin/sources"]:
        assert client.get(path).status_code == 403


def test_csrf_logout_and_revocation(client: TestClient) -> None:
    session = login(client)
    assert (
        client.post("/api/v1/auth/logout", headers={"Origin": "http://localhost:8080"}).status_code
        == 403
    )
    token = client.cookies["rag_session"]
    assert client.post("/api/v1/auth/logout", headers=mutation_headers(session)).status_code == 204
    client.cookies.set("rag_session", token)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_throttle_has_generic_errors(client: TestClient) -> None:
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
            headers={"Origin": "http://localhost:8080"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid username or password."
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "synthetic-password-123"},
        headers={"Origin": "http://localhost:8080"},
    )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_create_user_idempotency_and_revision(client: TestClient) -> None:
    session = login(client)
    body = {"username": "new-reader", "password": "synthetic-password-123", "role": "reader"}
    headers = mutation_headers(session, **{"Idempotency-Key": str(uuid4())})
    first = client.post("/api/v1/admin/users", json=body, headers=headers)
    assert first.status_code == 201, first.text
    again = client.post("/api/v1/admin/users", json=body, headers=headers)
    assert again.json() == first.json()
    assert "password_hash" not in first.json()
    changed = client.post("/api/v1/admin/users", json={**body, "role": "admin"}, headers=headers)
    assert changed.status_code == 409
    user_id = first.json()["id"]
    stale = client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"disabled": True},
        headers=mutation_headers(session, **{"If-Match": '"2"'}),
    )
    assert stale.status_code == 409
    update = client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"disabled": True},
        headers=mutation_headers(session, **{"If-Match": '"1"'}),
    )
    assert update.status_code == 200
    assert update.json()["revision"] == 2


def test_last_admin_cannot_be_disabled(client: TestClient) -> None:
    session = login(client)
    user = session["user"]
    assert isinstance(user, dict)
    response = client.patch(
        f"/api/v1/admin/users/{user['id']}",
        json={"disabled": True},
        headers=mutation_headers(session, **{"If-Match": '"1"'}),
    )
    assert response.status_code == 409
    assert client.get("/api/v1/auth/me").status_code == 200


def test_strict_validation_hides_password(client: TestClient) -> None:
    session = login(client)
    response = client.post(
        "/api/v1/admin/users",
        json={"username": "bad", "password": "secret-value", "role": "superadmin"},
        headers=mutation_headers(session, **{"Idempotency-Key": str(uuid4())}),
    )
    assert response.status_code == 422
    assert "secret-value" not in response.text
    assert "superadmin" not in response.text


def test_operator_endpoints_do_not_accept_session(client: TestClient) -> None:
    login(client)
    assert client.get("/api/v1/health/ready").status_code == 401
    assert client.get("/api/v1/metrics").status_code == 401
    headers = {"Authorization": "Bearer " + "o" * 48}
    ready = client.get("/api/v1/health/ready", headers=headers)
    assert ready.status_code == 503
    assert ready.json()["error"]["code"] == "MODEL_UNAVAILABLE"
    assert client.get("/api/v1/metrics", headers=headers).status_code == 200
