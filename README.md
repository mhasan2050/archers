# Archers local foundation

[Editable architecture diagram](./Architecture.drawio): implemented deployment,
manual ingestion component flow, and planned complete RAG architecture.

[Editable flow diagrams](./Flows.drawio): manual upload acceptance (implemented),
automatic source ingestion and authenticated RAG querying (planned), including
decision branches, failure handling and publication safeguards.

This repository includes the **stage-one foundation and stage-two ingestion
acceptance increment**:

- FastAPI backend with strict local authentication/RBAC and CSRF.
- Admin user and source configuration endpoints (filesystem connector only).
- Safe offline normalization for `pdf/docx/pptx/xlsx/markdown/text/html/csv`.
- React administration UI packaged behind nginx.
- Docker Compose baseline for local loopback deployment.
- Authenticated manual uploads/replacements, immutable originals and durable
  PostgreSQL ingestion jobs with lease/fencing/cancellation/retry primitives.

**Not yet operational:** parser/index worker, automatic source scanning, vector
publication/search, RAG queries, paid gateway and Helm. Accepted jobs remain
pending until the worker is implemented; no documents are marked ACTIVE/READY.
The API readiness endpoint intentionally returns 503. Do not use this increment
as a production RAG release.

## Quick start (synthetic local validation)

1. Create required secret files under [deploy/secrets/](./deploy/secrets/):
   - `postgres-password.txt`
   - `database-url.txt` (for Compose: `postgresql+psycopg://archers:<URL-encoded-postgres-password>@postgres:5432/archers`)
   - `operator-token.txt` (>= 32 chars)
   - `signing-key.txt` (>= 32 chars, different from operator token)
2. Start stack:
   - `docker compose up --build -d`
   - The one-shot migration service runs before API startup. A migration failure
     blocks the API rather than starting against an outdated schema.
4. Bootstrap first admin:
   - `docker compose exec api archers bootstrap --username admin --company "Synthetic Co"`
   - Enter the initial password at the protected prompt (15–1024 characters).
     Never put a password in command-line arguments. Bootstrap refuses an
     existing installation. No default admin is created.

The web UI is served at `http://localhost:8080`.
Only the API sees shared artifacts; source mounts belong to the future watcher,
not the API. Database/model services are never published.

## Stage-two manual ingestion API

Log in and retain the session cookie and CSRF token. Curator/admin requests:

- `POST /api/v1/documents`: multipart `file` and `title` only; send
  `X-CSRF-Token`, trusted `Origin`, and `Idempotency-Key`.
- `POST /api/v1/documents/{id}/versions`: same fields/headers plus quoted
  `If-Match` revision. Only manual documents can be replaced.
- `GET /api/v1/jobs/{id}`: inspect durable job state.
- `POST /api/v1/jobs/{id}/cancel`: CSRF/Origin; requests cancellation, does not
  claim the worker already stopped.
- `POST /api/v1/jobs/{id}/retry`: CSRF/Origin/idempotency key; only eligible
  failed, retryable, non-superseded jobs below the three-attempt ceiling.
- `GET /api/v1/documents` and `GET /api/v1/documents/{id}`: readers see active
  metadata only; curator/admin can inspect pending metadata.

Acceptance returns document/version/job IDs after immutable bytes and DB linkage
commit, not an ingestion completion. Byte signatures are checked at acceptance;
full parsing/security isolation is pending the worker. Format/file limits default
to 25 MiB, configurable up to 100 MiB. Snapshot names come from generated IDs,
not user filenames. No source paths, content or credentials are logged.
The installation-wide pending ingestion queue defaults to 100 jobs; overflow
returns 429 with Retry-After. PostgreSQL serialization prevents replicas from
each admitting a separate allowance. Idempotent replay does not use another slot.

Idempotency is scoped to tenant/actor/method/path and content. Same key with
different bytes/title/format/revision returns 409. Concurrent duplicate uploads
produce one version/job. In the event of uncertain DB commit errors, snapshots
are retained conservatively; orphan sweeping/retention is a later worker task.

## Developer checks

- Backend: `backend/.venv/bin/python -m pytest backend/tests -q`
- Backend lint/types: `backend/.venv/bin/python -m ruff check backend/src backend/tests && backend/.venv/bin/python -m mypy backend/src`
- Frontend: `cd web && npm test && npm run build`
- PostgreSQL integration: set `ARCHERS_TEST_DATABASE_URL` to an isolated
  PostgreSQL database whose name ends in `_validation`, then run
  `backend/.venv/bin/python -m pytest backend/tests/test_postgres_ingestion.py -q`.
  Tests create and remove their own generated schema only.
