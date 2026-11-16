import base64
import binascii
import hmac
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from archers.config import Settings
from archers.domain import DomainError, normalize_username, require_permission
from archers.dto import (
    CreateSource,
    CreateUser,
    FilesystemConfig,
    SourceDTO,
    SourcePage,
    UpdateSource,
    UpdateUser,
    UserDTO,
    UserPage,
)
from archers.identity import hash_password, keyed_digest, revoke_user_sessions, user_dto
from archers.storage import AuditEntry, IdempotencyRecord, Source, Tenant, User, now_utc


def audit(db: Session, actor: User, action: str, resource_id: UUID) -> None:
    db.add(
        AuditEntry(
            tenant_id=actor.tenant_id, actor_id=actor.id, action=action, resource_id=resource_id
        )
    )


def replay_or_create[DTO: BaseModel](
    db: Session,
    settings: Settings,
    actor: User,
    method_path: str,
    key: str,
    body: dict[str, Any],
    status: int,
    response_type: type[DTO],
    create: Callable[[], DTO],
) -> DTO:
    if not key or len(key) > 128:
        raise DomainError(
            "VALIDATION_ERROR", "Idempotency-Key is required (1 to 128 characters).", 422
        )
    signing_key = settings.signing_key.get_secret_value()
    key_hash = keyed_digest(signing_key, key)
    body_hash = keyed_digest(signing_key, json.dumps(body, sort_keys=True, separators=(",", ":")))
    row = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == actor.tenant_id,
            IdempotencyRecord.actor_id == actor.id,
            IdempotencyRecord.method_path == method_path,
            IdempotencyRecord.key_hash == key_hash,
        )
    )
    if row:
        if not hmac.compare_digest(row.body_hash, body_hash):
            raise DomainError(
                "REVISION_CONFLICT", "Idempotency key was used for different input.", 409
            )
        return response_type.model_validate(row.response)
    result = create()
    db.add(
        IdempotencyRecord(
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            method_path=method_path,
            key_hash=key_hash,
            body_hash=body_hash,
            response=result.model_dump(mode="json"),
            status=status,
        )
    )
    return result


def create_user(db: Session, actor: User, body: CreateUser) -> UserDTO:
    require_permission(actor.role, "users.manage")
    username = normalize_username(body.username)
    if db.scalar(
        select(User.id).where(User.tenant_id == actor.tenant_id, User.username == username)
    ):
        raise DomainError("REVISION_CONFLICT", "Username is already provisioned.", 409)
    user = User(
        tenant_id=actor.tenant_id,
        username=username,
        password_hash=hash_password(body.password.get_secret_value()),
        role=body.role,
    )
    db.add(user)
    db.flush()
    audit(db, actor, "users.create", user.id)
    return user_dto(user)


def update_user(
    db: Session, actor: User, user_id: UUID, body: UpdateUser, revision: int
) -> UserDTO:
    require_permission(actor.role, "users.manage")
    user = db.scalar(
        select(User).where(User.id == user_id, User.tenant_id == actor.tenant_id).with_for_update()
    )
    if not user:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    if user.revision != revision:
        raise DomainError("REVISION_CONFLICT", "Resource revision changed.", 409)
    role = body.role if body.role is not None else user.role
    disabled = body.disabled if body.disabled is not None else user.disabled_at is not None
    if user.role == "admin" and user.disabled_at is None and (role != "admin" or disabled):
        other = db.scalar(
            select(User.id).where(
                User.tenant_id == actor.tenant_id,
                User.id != user.id,
                User.role == "admin",
                User.disabled_at.is_(None),
            )
        )
        if not other:
            raise DomainError(
                "REVISION_CONFLICT", "The last enabled administrator must remain.", 409
            )
    user.role = role
    user.disabled_at = now_utc() if disabled else None
    if body.password is not None:
        user.password_hash = hash_password(body.password.get_secret_value())
    user.revision += 1
    tenant = db.get(Tenant, actor.tenant_id)
    if tenant is None:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Installation state unavailable.", 503)
    tenant.acl_revision += 1
    revoke_user_sessions(db, actor.tenant_id, user.id)
    audit(db, actor, "users.update", user.id)
    db.flush()
    return user_dto(user)


def source_dto(source: Source) -> SourceDTO:
    return SourceDTO.model_validate(
        {
            "id": source.id,
            "connector": source.config,
            "formats": source.formats,
            "enabled": source.enabled,
            "scan_policy": source.scan_policy,
            "export_policy": source.export_policy,
            "revision": source.revision,
        }
    )


def validate_source(settings: Settings, body: CreateSource) -> None:
    if not isinstance(body.connector, FilesystemConfig):
        raise DomainError(
            "PROVIDER_CAPABILITY_UNSUPPORTED",
            "Remote source adapters are not implemented in this stage.",
            422,
        )
    if body.connector.mount_alias not in settings.approved_mounts:
        raise DomainError("VALIDATION_ERROR", "Mount alias is not approved by the operator.", 422)
    if body.scan_policy.completion_mode == "remote_revision":
        raise DomainError(
            "VALIDATION_ERROR", "Filesystem requires a file completion protocol.", 422
        )
    if body.scan_policy.stable_seconds < 60 or body.scan_policy.deletion_grace_seconds < 300:
        raise DomainError(
            "VALIDATION_ERROR", "Scan policy is below required safety thresholds.", 422
        )
    if body.export_policy.allow_external_embeddings or body.export_policy.allow_external_generation:
        raise DomainError(
            "EXTERNAL_PROCESSING_NOT_APPROVED",
            "External processing is disabled in this stage.",
            403,
        )


def create_source(db: Session, settings: Settings, actor: User, body: CreateSource) -> SourceDTO:
    require_permission(actor.role, "sources.manage")
    validate_source(settings, body)
    source = Source(
        tenant_id=actor.tenant_id,
        connector_kind=body.connector.kind,
        config=body.connector.model_dump(mode="json"),
        formats=list(body.formats),
        scan_policy=body.scan_policy.model_dump(mode="json"),
        export_policy=body.export_policy.model_dump(mode="json"),
        enabled=body.enabled,
    )
    db.add(source)
    db.flush()
    audit(db, actor, "sources.create", source.id)
    return source_dto(source)


def update_source(
    db: Session,
    settings: Settings,
    actor: User,
    source_id: UUID,
    body: UpdateSource,
    revision: int,
) -> SourceDTO:
    require_permission(actor.role, "sources.manage")
    source = db.scalar(
        select(Source)
        .where(Source.id == source_id, Source.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if not source:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    if source.revision != revision:
        raise DomainError("REVISION_CONFLICT", "Resource revision changed.", 409)
    data = source_dto(source).model_dump(mode="json")
    data.pop("id")
    data.pop("revision")
    data.update(body.model_dump(mode="json", exclude_unset=True))
    proposed = CreateSource.model_validate(data)
    validate_source(settings, proposed)
    source.enabled = proposed.enabled
    source.formats = list(proposed.formats)
    source.scan_policy = proposed.scan_policy.model_dump(mode="json")
    source.export_policy = proposed.export_policy.model_dump(mode="json")
    source.revision += 1
    audit(db, actor, "sources.update", source.id)
    db.flush()
    return source_dto(source)


def make_cursor(settings: Settings, actor: User, route: str, last_id: UUID) -> str:
    payload = json.dumps(
        {
            "tenant": str(actor.tenant_id),
            "actor": str(actor.id),
            "route": route,
            "last_id": str(last_id),
            "expires": int((now_utc() + timedelta(hours=1)).timestamp()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = keyed_digest(settings.signing_key.get_secret_value(), encoded)
    return f"{encoded}.{signature}"


def read_cursor(settings: Settings, actor: User, route: str, cursor: str | None) -> UUID | None:
    if cursor is None:
        return None
    if len(cursor) > 512:
        raise DomainError("VALIDATION_ERROR", "Invalid pagination cursor.", 422)
    try:
        encoded, signature = cursor.split(".")
        expected = keyed_digest(settings.signing_key.get_secret_value(), encoded)
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise ValueError("Invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (
            not isinstance(payload, dict)
            or payload.get("tenant") != str(actor.tenant_id)
            or payload.get("actor") != str(actor.id)
            or payload.get("route") != route
            or not isinstance(payload.get("expires"), int)
            or payload["expires"] <= int(now_utc().timestamp())
        ):
            raise ValueError("Invalid binding")
        return UUID(payload["last_id"])
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError) as exc:
        raise DomainError("VALIDATION_ERROR", "Invalid pagination cursor.", 422) from exc


def list_users(
    db: Session, settings: Settings, actor: User, limit: int, cursor: str | None
) -> UserPage:
    require_permission(actor.role, "users.manage")
    last_id = read_cursor(settings, actor, "users", cursor)
    query = select(User).where(User.tenant_id == actor.tenant_id).order_by(User.id).limit(limit + 1)
    if last_id:
        query = query.where(User.id > last_id)
    users = list(db.scalars(query))
    more = len(users) > limit
    users = users[:limit]
    return UserPage(
        items=[user_dto(user) for user in users],
        next_cursor=make_cursor(settings, actor, "users", users[-1].id) if more else None,
    )


def list_sources(
    db: Session, settings: Settings, actor: User, limit: int, cursor: str | None
) -> SourcePage:
    require_permission(actor.role, "sources.manage")
    last_id = read_cursor(settings, actor, "sources", cursor)
    query = (
        select(Source)
        .where(Source.tenant_id == actor.tenant_id)
        .order_by(Source.id)
        .limit(limit + 1)
    )
    if last_id:
        query = query.where(Source.id > last_id)
    sources = list(db.scalars(query))
    more = len(sources) > limit
    sources = sources[:limit]
    return SourcePage(
        items=[source_dto(source) for source in sources],
        next_cursor=make_cursor(settings, actor, "sources", sources[-1].id) if more else None,
    )
