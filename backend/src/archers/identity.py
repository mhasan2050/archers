import hashlib
import hmac
import secrets
from datetime import timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from archers.config import Settings
from archers.domain import ROLE_PERMISSIONS, DomainError, normalize_username, require_permission
from archers.dto import SessionInfo, UserDTO
from archers.storage import LoginSession, LoginThrottle, Tenant, User, aware, now_utc

PASSWORD_HASHER = PasswordHasher()
DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 1024:
        raise DomainError("VALIDATION_ERROR", "Passwords require 15 to 1024 characters.", 422)
    return PASSWORD_HASHER.hash(password)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def keyed_digest(key: str, value: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def user_dto(user: User) -> UserDTO:
    return UserDTO(
        id=user.id,
        username=user.username,
        role=user.role,
        disabled=user.disabled_at is not None,
        revision=user.revision,
    )


def session_info(user: User, session: LoginSession) -> SessionInfo:
    return SessionInfo(
        user=user_dto(user),
        permissions=sorted(ROLE_PERMISSIONS.get(user.role, frozenset())),
        csrf_token=session.csrf_secret,
        expires_at=aware(session.expires_at),
    )


def installation_tenant(db: Session, *, lock: bool = False) -> Tenant:
    query = select(Tenant).order_by(Tenant.id)
    if lock:
        query = query.with_for_update()
    rows = db.scalars(query).all()
    if len(rows) != 1:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Explicit installation bootstrap is required.", 503
        )
    return rows[0]


def throttle(db: Session, key: str, limit: int) -> LoginThrottle:
    row = db.get(LoginThrottle, key)
    if row is None:
        row = LoginThrottle(key=key, failed_count=0, window_start=now_utc())
        db.add(row)
        db.flush()
    if (now_utc() - aware(row.window_start)).total_seconds() >= 900:
        row.failed_count = 0
        row.window_start = now_utc()
    if row.failed_count >= limit:
        retry = max(1, int(900 - (now_utc() - aware(row.window_start)).total_seconds()))
        raise DomainError("RATE_LIMITED", "Login temporarily limited.", 429, retry)
    return row


def issue_session(
    db: Session, settings: Settings, username: str, password: str, ip: str
) -> tuple[str, SessionInfo]:
    tenant = installation_tenant(db, lock=True)
    name = normalize_username(username)
    key = settings.signing_key.get_secret_value()
    account = throttle(db, keyed_digest(key, f"account:{tenant.id}:{name}"), 5)
    address = throttle(db, keyed_digest(key, f"ip:{ip}"), 30)
    user = db.scalar(select(User).where(User.tenant_id == tenant.id, User.username == name))
    valid = False
    try:
        valid = PASSWORD_HASHER.verify(user.password_hash if user else DUMMY_HASH, password)
    except VerifyMismatchError:
        valid = False
    except (InvalidHashError, VerificationError) as exc:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Password verification unavailable.", 503
        ) from exc
    if user is None or user.disabled_at is not None or not valid:
        account.failed_count += 1
        address.failed_count += 1
        db.commit()
        raise DomainError("UNAUTHENTICATED", "Invalid username or password.", 401)
    account.failed_count = 0
    if PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = PASSWORD_HASHER.hash(password)
    token = secrets.token_urlsafe(48)
    session = LoginSession(
        tenant_id=tenant.id,
        user_id=user.id,
        token_hash=digest(token),
        csrf_secret=secrets.token_urlsafe(32),
        issued_at=now_utc(),
        expires_at=now_utc() + timedelta(hours=settings.session_hours),
    )
    db.add(session)
    db.flush()
    return token, session_info(user, session)


def authenticate(
    db: Session, token: str | None, permission: str | None = None, *, lock: bool = False
) -> tuple[User, LoginSession]:
    if not token or len(token) > 256:
        raise DomainError("UNAUTHENTICATED", "Authentication required.", 401)
    session = db.scalar(select(LoginSession).where(LoginSession.token_hash == digest(token)))
    if not session or session.revoked_at is not None or aware(session.expires_at) <= now_utc():
        raise DomainError("UNAUTHENTICATED", "Authentication required.", 401)
    if lock:
        db.scalar(select(Tenant).where(Tenant.id == session.tenant_id).with_for_update())
        db.expire(session)
        if session.revoked_at is not None or aware(session.expires_at) <= now_utc():
            raise DomainError("UNAUTHENTICATED", "Authentication required.", 401)
    user = db.scalar(
        select(User).where(User.id == session.user_id, User.tenant_id == session.tenant_id)
    )
    if not user or user.disabled_at is not None:
        raise DomainError("UNAUTHENTICATED", "Authentication required.", 401)
    if permission:
        require_permission(user.role, permission)
    return user, session


def revoke_user_sessions(db: Session, tenant_id: UUID, user_id: UUID) -> None:
    db.execute(
        update(LoginSession)
        .where(LoginSession.tenant_id == tenant_id, LoginSession.user_id == user_id)
        .values(revoked_at=now_utc())
    )
