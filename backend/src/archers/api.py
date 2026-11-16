import hmac
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint

from archers import services
from archers.config import Settings, load_settings
from archers.documents import UPLOAD_PATH
from archers.domain import DomainError
from archers.dto import (
    CreateSource,
    CreateUser,
    Health,
    LoginRequest,
    SessionInfo,
    SourceDTO,
    SourcePage,
    UpdateSource,
    UpdateUser,
    UserDTO,
    UserPage,
)
from archers.identity import authenticate, issue_session, session_info
from archers.storage import SCHEMA_REVISION, LoginSession, User, make_engine, now_utc

LOGGER = logging.getLogger("archers")
PREFIX = "/api/v1"


def error_response(request: Request, error: DomainError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    LOGGER.warning("request_failed code=%s request_id=%s", error.code, request_id)
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": request_id,
                "retryable": error.status in {429, 503, 504},
            }
        },
        status_code=error.status,
        headers={"Retry-After": str(error.retry_after or 1)} if error.status == 429 else None,
    )


def check_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != SCHEMA_REVISION:
            raise RuntimeError("Database migration required before starting the application.")


def check_revision(value: str | None) -> int:
    if value is None or not re.fullmatch(r'"[1-9][0-9]*"', value) or len(value) > 22:
        raise DomainError("VALIDATION_ERROR", "If-Match requires a quoted positive revision.", 422)
    return int(value[1:-1])


def current(
    request: Request, db: Session, permission: str | None = None, *, mutation: bool = False
) -> tuple[User, LoginSession]:
    user, session = authenticate(db, request.cookies.get("rag_session"), permission, lock=mutation)
    if mutation:
        csrf = request.headers.get("X-CSRF-Token", "")
        if len(csrf) > 256 or not hmac.compare_digest(csrf.encode(), session.csrf_secret.encode()):
            raise DomainError("FORBIDDEN", "Valid session-bound CSRF token required.", 403)
    return user, session


def operator(request: Request, settings: Settings) -> None:
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + settings.operator_token.get_secret_value()
    if len(auth) > 1024 or not hmac.compare_digest(auth.encode(), expected.encode()):
        raise DomainError("UNAUTHENTICATED", "Operator authentication required.", 401)


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    settings = settings or load_settings()
    owned_engine = engine is None
    database = (
        engine if engine is not None else make_engine(settings.database_url.get_secret_value())
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owned_engine:
            check_schema(database)
        yield
        if owned_engine:
            database.dispose()

    app = FastAPI(
        title="Archers administration API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.database = database
    app.state.settings = settings

    @app.middleware("http")
    async def perimeter(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        host = request.url.hostname
        if host not in settings.hosts:
            return error_response(request, DomainError("VALIDATION_ERROR", "Untrusted host.", 400))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("Origin") not in settings.origins:
                return error_response(
                    request, DomainError("FORBIDDEN", "Trusted Origin required.", 403)
                )
            if not (request.method == "POST" and UPLOAD_PATH.fullmatch(request.url.path)):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > settings.max_json_bytes:
                        return error_response(
                            request,
                            DomainError("UPLOAD_TOO_LARGE", "JSON request exceeds limit.", 413),
                        )
                if (
                    body
                    and request.headers.get("Content-Type", "").split(";")[0].strip()
                    != "application/json"
                ):
                    return error_response(
                        request, DomainError("VALIDATION_ERROR", "JSON content type required.", 415)
                    )
                request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            DomainError("VALIDATION_ERROR", "Request does not match the API contract.", 422),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        status = (
            422
            if exc.status_code == 400 and UPLOAD_PATH.fullmatch(request.url.path)
            else exc.status_code
        )
        return error_response(
            request,
            DomainError(
                "NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR",
                "Resource not found." if exc.status_code == 404 else "Invalid request.",
                status,
            ),
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return error_response(
            request, DomainError("DEPENDENCY_UNAVAILABLE", "Persistence unavailable.", 503)
        )

    @app.exception_handler(OSError)
    async def storage_error(request: Request, exc: OSError) -> JSONResponse:
        return error_response(
            request,
            DomainError("DEPENDENCY_UNAVAILABLE", "Private artifact storage unavailable.", 503),
        )

    @app.post(PREFIX + "/auth/login", response_model=SessionInfo, operation_id="login")
    def login(body: LoginRequest, request: Request, response: Response) -> SessionInfo:
        with Session(database) as db, db.begin():
            ip = request.client.host if request.client else "unknown"
            token, info = issue_session(
                db, settings, body.username, body.password.get_secret_value(), ip
            )
        response.set_cookie(
            "rag_session",
            token,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookie,
            path=PREFIX,
            max_age=settings.session_hours * 3600,
        )
        return info

    @app.get(PREFIX + "/auth/me", response_model=SessionInfo, operation_id="getSession")
    def me(request: Request) -> SessionInfo:
        with Session(database) as db:
            user, session = current(request, db)
            return session_info(user, session)

    @app.post(PREFIX + "/auth/logout", status_code=204, operation_id="logout")
    def logout(request: Request) -> Response:
        with Session(database) as db, db.begin():
            _, session = current(request, db, mutation=True)
            session.revoked_at = now_utc()
        response = Response(status_code=204)
        response.delete_cookie(
            "rag_session", path=PREFIX, secure=settings.secure_cookie, httponly=True, samesite="lax"
        )
        return response

    @app.get(PREFIX + "/admin/users", response_model=UserPage, operation_id="listUsers")
    def users(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> UserPage:
        with Session(database) as db:
            actor, _ = current(request, db, "users.manage")
            return services.list_users(db, settings, actor, limit, cursor)

    @app.post(
        PREFIX + "/admin/users", response_model=UserDTO, status_code=201, operation_id="createUser"
    )
    def provision_user(
        body: CreateUser,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ) -> UserDTO:
        with Session(database) as db, db.begin():
            actor, _ = current(request, db, "users.manage", mutation=True)
            hashed_body = body.model_dump(mode="json")
            hashed_body["password"] = body.password.get_secret_value()
            return services.replay_or_create(
                db,
                settings,
                actor,
                "POST /admin/users",
                idempotency_key,
                hashed_body,
                201,
                UserDTO,
                lambda: services.create_user(db, actor, body),
            )

    @app.patch(PREFIX + "/admin/users/{id}", response_model=UserDTO, operation_id="updateUser")
    def change_user(
        id: UUID,
        body: UpdateUser,
        request: Request,
        if_match: Annotated[str | None, Header()] = None,
    ) -> UserDTO:
        with Session(database) as db, db.begin():
            actor, _ = current(request, db, "users.manage", mutation=True)
            return services.update_user(db, actor, id, body, check_revision(if_match))

    @app.get(PREFIX + "/admin/sources", response_model=SourcePage, operation_id="listSources")
    def sources(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> SourcePage:
        with Session(database) as db:
            actor, _ = current(request, db, "sources.manage")
            return services.list_sources(db, settings, actor, limit, cursor)

    @app.post(
        PREFIX + "/admin/sources",
        response_model=SourceDTO,
        status_code=201,
        operation_id="createSource",
    )
    def register_source(
        body: CreateSource,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ) -> SourceDTO:
        with Session(database) as db, db.begin():
            actor, _ = current(request, db, "sources.manage", mutation=True)
            return services.replay_or_create(
                db,
                settings,
                actor,
                "POST /admin/sources",
                idempotency_key,
                body.model_dump(mode="json"),
                201,
                SourceDTO,
                lambda: services.create_source(db, settings, actor, body),
            )

    @app.patch(
        PREFIX + "/admin/sources/{id}", response_model=SourceDTO, operation_id="updateSource"
    )
    def change_source(
        id: UUID,
        body: UpdateSource,
        request: Request,
        if_match: Annotated[str | None, Header()] = None,
    ) -> SourceDTO:
        with Session(database) as db, db.begin():
            actor, _ = current(request, db, "sources.manage", mutation=True)
            return services.update_source(db, settings, actor, id, body, check_revision(if_match))

    @app.get(PREFIX + "/health/live", response_model=Health, operation_id="liveness")
    def live() -> Health:
        return Health()

    @app.get(PREFIX + "/health/ready", response_model=Health, operation_id="readiness")
    def ready(request: Request) -> Health:
        operator(request, settings)
        check_schema(database)
        raise DomainError(
            "MODEL_UNAVAILABLE", "RAG inference is not implemented in the foundation stage.", 503
        )

    @app.get(PREFIX + "/metrics", response_class=PlainTextResponse, operation_id="metrics")
    def metrics(request: Request) -> PlainTextResponse:
        operator(request, settings)
        return PlainTextResponse(
            "# HELP archers_rag_ready Whether RAG is ready to serve queries.\n"
            "# TYPE archers_rag_ready gauge\narchers_rag_ready 0\n",
            media_type="text/plain; version=0.0.4",
        )

    from archers.ingestion_api import register_routes

    register_routes(app, database, settings)
    return app
