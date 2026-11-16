from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.types import Message

from archers import documents
from archers.config import Settings
from archers.domain import DomainError
from archers.services import audit, replay_or_create


def register_routes(app: FastAPI, engine: Engine, settings: Settings) -> None:
    from archers.api import check_revision, current

    def authorize(request: Request) -> UUID:
        with Session(engine) as db:
            actor, _ = current(request, db, "ingestion.manage", mutation=True)
            return actor.tenant_id

    async def upload(
        request: Request, key: str, document_id: UUID | None = None, revision: int | None = None
    ) -> documents.AcceptedIngestion:
        if not key or len(key) > 128:
            raise DomainError("VALIDATION_ERROR", "Idempotency-Key is required.", 422)
        tenant_id = await run_in_threadpool(authorize, request)
        if request.headers.get("Content-Type", "").split(";")[0].strip() != "multipart/form-data":
            raise DomainError("VALIDATION_ERROR", "Multipart upload required.", 415)
        receive = request._receive
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > settings.max_upload_bytes + 8192:
                raise DomainError("UPLOAD_TOO_LARGE", "Multipart request exceeds limit.", 413)
            return message

        request._receive = bounded_receive
        async with request.form(max_files=1, max_fields=1, max_part_size=4096) as form:
            if sorted(form.keys()) != ["file", "title"] or len(form.multi_items()) != 2:
                raise DomainError("VALIDATION_ERROR", "Upload requires only file and title.", 422)
            file = form["file"]
            title = form["title"]
            if not isinstance(file, UploadFile) or not isinstance(title, str):
                raise DomainError("VALIDATION_ERROR", "Invalid upload fields.", 422)
            if not title.strip() or len(title) > 300:
                raise DomainError("VALIDATION_ERROR", "Title requires 1 to 300 characters.", 422)
            target_id = document_id or uuid4()
            version_id = uuid4()
            snapshot = await run_in_threadpool(
                documents.snapshot_file,
                settings,
                tenant_id,
                target_id,
                version_id,
                file.filename or "",
                file.file,
            )

            def commit() -> documents.AcceptedIngestion:
                try:
                    with Session(engine) as db, db.begin():
                        actor, _ = current(request, db, "ingestion.manage", mutation=True)
                        if actor.tenant_id != tenant_id:
                            raise DomainError(
                                "AUTHORIZATION_CHANGED", "Authorization changed.", 409
                            )
                        path = (
                            "POST /documents"
                            if document_id is None
                            else f"POST /documents/{document_id}/versions"
                        )
                        result = replay_or_create(
                            db,
                            settings,
                            actor,
                            path,
                            key,
                            {
                                "hash": snapshot.hash,
                                "title": title,
                                "format": snapshot.format,
                                "revision": revision,
                            },
                            202,
                            documents.AcceptedIngestion,
                            lambda: documents.accept_snapshot(
                                db,
                                actor,
                                snapshot,
                                title,
                                target_id,
                                version_id,
                                revision=revision,
                                max_pending_jobs=settings.max_pending_ingestion_jobs,
                            ),
                        )
                except DomainError:
                    documents.discard_snapshot(settings, snapshot)
                    raise
                if result.version_id != version_id:
                    documents.discard_snapshot(settings, snapshot)
                return result

            return await run_in_threadpool(commit)

    @app.post(
        "/api/v1/documents",
        response_model=documents.AcceptedIngestion,
        status_code=202,
        operation_id="uploadDocument",
    )
    async def upload_document(
        request: Request, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]
    ) -> documents.AcceptedIngestion:
        return await upload(request, idempotency_key)

    @app.post(
        "/api/v1/documents/{id}/versions",
        response_model=documents.AcceptedIngestion,
        status_code=202,
        operation_id="replaceManualDocument",
    )
    async def replace_document(
        id: UUID,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
        if_match: Annotated[str | None, Header()] = None,
    ) -> documents.AcceptedIngestion:
        return await upload(request, idempotency_key, id, check_revision(if_match))

    @app.get(
        "/api/v1/documents", response_model=documents.DocumentPage, operation_id="listDocuments"
    )
    def list_documents(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> documents.DocumentPage:
        with Session(engine) as db:
            actor, _ = current(request, db, "documents.read")
            return documents.list_documents(db, settings, actor, cursor, limit)

    @app.get(
        "/api/v1/documents/{id}", response_model=documents.DocumentDTO, operation_id="getDocument"
    )
    def get_document(id: UUID, request: Request) -> documents.DocumentDTO:
        with Session(engine) as db:
            actor, _ = current(request, db, "documents.read")
            return documents.get_document(db, actor, id)

    @app.get("/api/v1/jobs/{id}", response_model=documents.JobDTO, operation_id="getJob")
    def get_job(id: UUID, request: Request) -> documents.JobDTO:
        with Session(engine) as db:
            actor, _ = current(request, db, "ingestion.manage")
            return documents.job_dto(
                documents.get_job(db, actor, id), UUID(request.state.request_id)
            )

    @app.post(
        "/api/v1/jobs/{id}/cancel",
        response_model=documents.JobDTO,
        status_code=202,
        operation_id="cancelJob",
    )
    def cancel_job(id: UUID, request: Request) -> documents.JobDTO:
        with Session(engine) as db, db.begin():
            actor, _ = current(request, db, "ingestion.manage", mutation=True)
            job = documents.get_job(db, actor, id, lock=True)
            if job.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise DomainError("REVISION_CONFLICT", "Job is already terminal.", 409)
            job.cancel_requested = True
            audit(db, actor, "jobs.cancel", job.id)
            return documents.job_dto(job, UUID(request.state.request_id))

    @app.post(
        "/api/v1/jobs/{id}/retry",
        response_model=documents.AcceptedJob,
        status_code=202,
        operation_id="retryJob",
    )
    def retry_job(
        id: UUID,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ) -> documents.AcceptedJob:
        with Session(engine) as db, db.begin():
            actor, _ = current(request, db, "ingestion.manage", mutation=True)
            return replay_or_create(
                db,
                settings,
                actor,
                f"POST /jobs/{id}/retry",
                idempotency_key,
                {"job_id": str(id)},
                202,
                documents.AcceptedJob,
                lambda: documents.retry_job(
                    db,
                    actor,
                    id,
                    max_pending_jobs=settings.max_pending_ingestion_jobs,
                ),
            )
