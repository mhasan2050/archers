from dataclasses import dataclass
from typing import Literal

Role = Literal["reader", "curator", "admin"]
BASE_PERMISSIONS = frozenset({"query", "documents.read", "conversations.own", "feedback.own"})
CURATOR_PERMISSIONS = BASE_PERMISSIONS | {"ingestion.manage", "sources.observe", "sources.rescan"}
ADMIN_PERMISSIONS = CURATOR_PERMISSIONS | {
    "users.manage",
    "sources.manage",
    "documents.withdraw",
    "model_policy.observe",
    "export.manage",
}
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": BASE_PERMISSIONS,
    "curator": CURATOR_PERMISSIONS,
    "admin": ADMIN_PERMISSIONS,
}


@dataclass
class DomainError(Exception):
    code: str
    message: str
    status: int
    retry_after: int | None = None


def require_permission(role: str, permission: str) -> None:
    if permission not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise DomainError("FORBIDDEN", "Permission denied.", 403)


def normalize_username(username: str) -> str:
    value = username.strip().casefold()
    if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise DomainError("VALIDATION_ERROR", "Invalid username.", 422)
    return value
