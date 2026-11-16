# Archers - Ask questions about your company documents

Archers is a self-hosted application being built to help employees find answers
in company documents, with citations back to the source. Instead of searching
through many PDFs manually, users will be able to ask a question in plain language.

It uses **retrieval-augmented generation (RAG)**: find relevant document passages
first, then ask an AI model to answer using those passages. The goal is to run
locally in Docker, using local open-source models by default. Paid models will
be optional and require explicit approval; there is no automatic cloud fallback.

> **Work in progress, not a production RAG release.** Login, administration and
> manual upload acceptance work. Automatic ingestion, vector search and answering
> questions are not connected yet. Docker runtime validation is also pending.

## How it will work

```text
Company documents -> Extract text -> Split into chunks -> Create embeddings
                                                              |
                                                       Store in Qdrant
                                                              |
User question -> Find relevant chunks -> Generate answer with source citations
```

An embedding is a list of numbers representing the meaning of a text passage.
It helps find relevant passages even when a question uses different wording.

The target product will monitor an approved NAS/network folder automatically,
with additional connectors for S3, SharePoint/OneDrive and Google Drive. Manual
uploads are accepted today; automatic connectors are planned. Format parsing
already exists as an offline library for PDF, Word, PowerPoint, Excel, Markdown,
plain text, HTML and CSV, but is not yet connected to uploaded files.

## What works today

- Local user login and reader, curator and administrator roles.
- Administration UI for users and filesystem source configuration.
- Manual file upload/replacement **through the API**, with private original-file
  storage and persistent processing jobs.
- Job status, cancellation requests and eligible retry APIs.
- Docker Compose configuration for the web app, API and PostgreSQL.

Uploaded files remain **pending**: there is no running processing worker, and
no vectors or searchable documents are created yet. The readiness endpoint
intentionally returns HTTP 503. Upload and question-answering screens are pending.

## Components and their roles

| Component | What it does | Current status |
|---|---|---|
| React + TypeScript | Browser-based login and administration | Implemented; upload/chat screens pending |
| nginx | Serves the UI and forwards API requests | Configured |
| Python + FastAPI | Authentication, permissions and document/job APIs | Implemented subset |
| PostgreSQL | Stores users, sessions, document versions and jobs; later, chat and index metadata | Current records implemented; later tables planned |
| SQLAlchemy + Alembic | Database access and versioned schema upgrades | Implemented |
| Private artifact volume | Keeps original documents; later, extracted text and citation data | Original storage implemented |
| Watcher + processing worker | Discovers files and runs extraction, chunking and indexing | Planned; job lifecycle primitives exist |
| Format parsers | Extract text and source locations from supported files | Offline library implemented; worker integration pending |
| Docling + local OCR | Rich document extraction and reading scanned pages | Planned |
| Qdrant | Free, self-hosted vector database for semantic and keyword-based retrieval | Planned; not deployed yet |
| Local embedding and reranking models | Encode text for search and reorder retrieved passages | Planned; models not selected or downloaded |
| Ollama + local language model | Generates grounded answers locally | Planned; model license/hardware review required |
| LangChain | Integrates model and retrieval adapters | Planned |
| LangGraph | Controls the bounded question-to-answer workflow | Planned |
| LangSmith | Optional tracing and evaluation integration | Off; separate privacy/licensing approval required |
| Paid-provider gateway | Optional external generation or embeddings with export and budget checks | Planned; disabled by default |
| Docker Compose | Packages services for a local installation | Configuration exists; container runtime validation pending |

PostgreSQL is the source of truth for access and document status. Qdrant will
store chunk vectors and identifying metadata, not raw document text. Originals
and extracted text stay in private artifact storage. The vector index can be
rebuilt from retained documents.

## Chunking strategy (planned)

Chunking splits a large document into small, searchable passages. Retrieving
focused passages keeps model input smaller and helps produce specific citations.

1. **Extract structured text first.** Preserve headings, paragraphs, table
   headers and original locations rather than splitting raw PDF bytes.
2. **Target about 450 tokens per chunk**, with an initial hard cap of **480 tokens**
   and **up to 60 tokens of overlap** between adjacent chunks. Tokens are model
   text units, not words; overlap helps preserve context across boundaries.
3. **Respect document structure.** Keep heading context and table headers.
   Oversized tables or rows require explicit splitting or review, not silent
   truncation.
4. **Keep citation metadata with each chunk.** Record its document, version,
   heading and contributing locations: physical PDF pages, slide numbers,
   spreadsheet cells, or other format-specific locations.
5. **Embed and index the chunks.** Store dense semantic vectors and sparse
   keyword vectors in Qdrant. Make a replacement searchable only after its new
   index generation has been verified; keep the old active version until then.

These are initial design values for a candidate 512-token embedding model, not
implemented settings or benchmark results. The selected embedding/reranking
models' actual tokenizers and limits govern the final sizes. Changing the
embedding model requires compatible reindexing.

See the [ingestion contract](./docs/ingestion.md) for the full processing and
publication rules.

## Try the current foundation locally

You need Docker with Docker Compose. Run commands from the project root.
This setup is for local evaluation with synthetic documents, not production.

1. Create required secret files under [deploy/secrets/](./deploy/secrets/):
   - `postgres-password.txt`
   - `database-url.txt` (for Compose: `postgresql+psycopg://archers:<URL-encoded-postgres-password>@postgres:5432/archers`)
   - `operator-token.txt` (>= 32 chars)
   - `signing-key.txt` (>= 32 chars, different from operator token)
   Use a strong PostgreSQL password and match it in the database URL. Keep all
   secret files private and out of version control.
2. Start the services:

   ```sh
   docker compose up --build -d
   ```

   Database migrations run before API startup. A failed migration blocks startup.
3. Create the first administrator:

   ```sh
   docker compose exec api archers bootstrap --username admin --company "Synthetic Co"
   ```

   Enter the password at the prompt (15-1024 characters). There is no default
   administrator or password. Bootstrap refuses an existing installation.
4. Open `http://localhost:8080` and log in.

The web UI is served at `http://localhost:8080`.
Only the API sees shared artifacts; source mounts belong to the future watcher,
not the API. Database/model services are never published.

## Diagrams and detailed documentation

- [Architecture diagram](./Architecture.drawio): current components and planned RAG system.
- [Flow diagrams](./Flows.drawio): uploads, automatic ingestion and querying.
- [Documentation index](./docs/README.md): architecture, security, data model and design decisions.
- [API contract](./docs/api-contracts.yaml): full designed API; only the subset described below is implemented.
- [Deployment design](./docs/deployment-packaging.md): target production packaging, not a certified installation guide.

Open the editable `.drawio` files in draw.io / diagrams.net.

## Technical reference

<details>
<summary>Manual ingestion API and safeguards</summary>


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

</details>

<details>
<summary>Developer checks</summary>

- Backend: `backend/.venv/bin/python -m pytest backend/tests -q`
- Backend lint/types: `backend/.venv/bin/python -m ruff check backend/src backend/tests && backend/.venv/bin/python -m mypy backend/src`
- Frontend: `cd web && npm test && npm run build`
- PostgreSQL integration: set `ARCHERS_TEST_DATABASE_URL` to an isolated
  PostgreSQL database whose name ends in `_validation`, then run
  `backend/.venv/bin/python -m pytest backend/tests/test_postgres_ingestion.py -q`.
  Tests create and remove their own generated schema only.

</details>
