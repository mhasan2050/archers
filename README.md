# Archers - Ask questions about your company documents

Archers is a self-hosted application being built to help employees find answers
in company documents, with citations back to the source. Instead of searching
through many PDFs manually, users will be able to ask a question in plain language.

It uses **retrieval-augmented generation (RAG)**: find relevant document passages
first, then ask an AI model to answer using those passages. The goal is to run
locally in Docker, using local open-source models by default. Paid models will
be optional and require explicit approval; there is no automatic cloud fallback.

> **Work in progress, not a production RAG release.** Login, administration and
> automatic folder ingestion and vector indexing are implemented. The chat/query
> workflow is not connected yet. Docker runtime validation is also pending.

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
uploads are also processed by the worker. Automatic local-folder ingestion is
implemented; NAS additionally requires an operator-provided live mount-health
record. Cloud connectors remain planned. Parsers support PDF, Word, PowerPoint,
Excel, Markdown, plain text, HTML and CSV. Scanned PDFs needing OCR or files with
extraction warnings go to review instead of being silently indexed incompletely.

## What works today

- Local user login and reader, curator and administrator roles.
- Administration UI for users and filesystem source configuration.
- Manual file upload/replacement **through the API**, with private original-file
  storage and persistent processing jobs.
- Job status, cancellation requests and eligible retry APIs.
- Automatic polling of configured folders, including existing files, new files
  and changed versions. No upload API call is needed for folder-based ingestion.
- Isolated parsing, model-token-aware chunking, local embeddings and verified
  Qdrant indexing, with persistent jobs and replacement-version safeguards.
- Docker Compose configuration for the web app, API, PostgreSQL, watcher, worker
  and Qdrant.

With the watcher/worker, prepared model and Qdrant running, valid documents become
**ACTIVE** after indexing. Files stay pending if the worker is stopped or dependencies
are unavailable. The overall RAG readiness endpoint still returns HTTP 503 because
answer generation is unfinished. Upload and question-answering screens are pending.

## Components and their roles

| Component | What it does | Current status |
|---|---|---|
| React + TypeScript | Browser-based login and administration | Implemented; upload/chat screens pending |
| nginx | Serves the UI and forwards API requests | Configured |
| Python + FastAPI | Authentication, permissions and document/job APIs | Implemented subset |
| PostgreSQL | Stores users, sessions, source observations, document versions, jobs, index generations and chunk metadata | Implemented; chat storage planned |
| SQLAlchemy + Alembic | Database access and versioned schema upgrades | Implemented |
| Private artifact volume | Keeps originals, extracted text, chunk text and citation locations | Implemented |
| Watcher + processing worker | Discovers files and runs extraction, chunking and indexing | Implemented for filesystem sources |
| Format parsers | Extract text and source locations in an isolated child process | Integrated; extraction warnings require review |
| Docling + local OCR | Rich document extraction and reading scanned pages | Planned |
| Qdrant | Free, self-hosted vector database with persistent dense and sparse vectors | Indexing implemented; user-facing retrieval pending |
| Local embedding model | Encodes document chunks without cloud calls | Implemented; explicit model preparation required |
| Local reranking model | Reorders retrieved passages for answering | Planned |
| Ollama + local language model | Generates grounded answers locally | Planned; model license/hardware review required |
| LangChain | Integrates model and retrieval adapters | Planned |
| LangGraph | Controls the bounded question-to-answer workflow | Planned |
| LangSmith | Optional tracing and evaluation integration | Off; separate privacy/licensing approval required |
| Paid-provider gateway | Optional external generation or embeddings with export and budget checks | Planned; disabled by default |
| Docker Compose | Packages services for a local installation | Configuration exists; container runtime validation pending |

PostgreSQL is the source of truth for access and document status. Qdrant
stores chunk vectors and identifying metadata, not raw document text. Originals
and extracted text stay in private artifact storage. The vector index can be
rebuilt from retained documents.

## Chunking strategy

Chunking splits a large document into small, searchable passages. Retrieving
focused passages keeps model input smaller and helps produce specific citations.

1. **Extract text blocks first.** Keep original text and source locations rather
   than splitting raw PDF bytes. Each block is split independently.
2. **Use at most 256 tokens per chunk with the current model**, including two
   special tokens (up to 254 content tokens), and **up to 60 tokens of overlap**
   within a long block. Tokens are model
   text units, not words; overlap helps preserve context across boundaries.
   The actual chunk size is reduced to fit the selected model, including its
   special tokens. A smaller model never receives silently truncated chunks.
3. **Keep the original characters and provenance.** Long blocks are split at
   tokenizer offsets, without dropping text or silently truncating it.
   Cross-block heading context and repeated table headers are future improvements;
   the current chunker does not reconstruct missing document structure.
4. **Keep citation metadata with each chunk.** Record its document, version,
   contributing locations: physical PDF pages, slide numbers,
   spreadsheet cells, or other format-specific locations.
5. **Embed and index the chunks.** Store dense semantic vectors and sparse
   keyword vectors in Qdrant. Make a replacement searchable only after its new
   index generation has been verified; keep the old active version until then.

The earlier 450-token target and 480-token cap remain design ceilings for larger
models, not the current MiniLM chunk size or measured performance results.
The selected model's actual tokenizer and limits govern the emitted chunks.
Changing the embedding model or chunking pipeline requires compatible reindexing.

The prepared model is **sentence-transformers/all-MiniLM-L6-v2**, whose model
card declares **Apache-2.0**, pinned to revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. It runs locally on CPU through
ONNX Runtime and produces 384-dimensional vectors. Setup verifies every asset's
SHA-256 checksum; the model card and manifest are kept with the ignored local
weights. This English model is an initial implementation choice, not a benchmark
or multilingual quality guarantee. Sparse vectors currently use deterministic
hashed lexical term frequencies, **not BM25**.

See the [ingestion contract](./docs/ingestion.md) for the full processing and
publication rules.

## Try the current foundation locally

You need Docker with Docker Compose. Run commands from the project root.
This setup is for local evaluation with synthetic documents, not production.
The Linux parser requires a kernel with Landlock support and `libseccomp2`
(included in the backend image). Unsupported sandbox environments fail closed;
they do not run the parser unsandboxed. macOS development uses `sandbox-exec`.

1. Create required secret files under [deploy/secrets/](./deploy/secrets/):
   - `postgres-password.txt`
   - `database-url.txt` (for Compose: `postgresql+psycopg://archers:<URL-encoded-postgres-password>@postgres:5432/archers`)
   - `operator-token.txt` (>= 32 chars)
   - `signing-key.txt` (>= 32 chars, different from operator token)
   Use a strong PostgreSQL password and match it in the database URL. Keep all
   secret files private and out of version control.
2. Prepare the source folder and embedding model using the instructions below.
3. Start the services:

   ```sh
   docker compose up --build -d
   ```

   Database migrations run before API startup. A failed migration blocks startup.
4. Create the first administrator:

   ```sh
   docker compose exec api archers bootstrap --username admin --company "Synthetic Co"
   ```

   Enter the password at the prompt (15-1024 characters). There is no default
   administrator or password. Bootstrap refuses an existing installation.
5. Open `http://localhost:8080` and log in.

The web UI is served at `http://localhost:8080`.
The API and worker share private artifacts; only the watcher reads source folders.
Database/model/vector services are never published.

## Automatic folder ingestion

The default Compose source alias is `inbox`, pointing to the host folder
`deploy/data/inbox`. Register that alias in the administration UI once; after
that, saving a supported file in that folder is enough to start ingestion.

1. Create `deploy/data/inbox` and `deploy/models/embedding` on the host. Do not
   store company documents in tracked project folders.
2. Prepare the reviewed embedding model explicitly before starting the worker.
   Runtime workers never download weights. See the model preparation command
   below.
3. Put a file named `.archers-source-id` in the source folder containing a UUID.
   Use that same UUID as the source's **sentinel ID** in the UI. This prevents
   accidentally scanning a different folder. The `archers source-init
   --mount-alias inbox` operator command can initialize a writable local mount;
   normal watcher mounts remain read-only.
4. In the UI, add an enabled filesystem source with mount alias `inbox`, the
   sentinel ID and allowed formats. Defaults are a 30-second poll, a 60-second
   stability period and the selected completion protocol.
5. Start the stack and copy a supported file into the folder. Prefer writing to
   a temporary name such as `policy.pdf.part`, then renaming to `policy.pdf`
   after the copy completes.

Run the one-time setup after creating the secret files:

```sh
mkdir -p deploy/data/inbox deploy/models/embedding
docker compose --profile setup run --build --rm --no-deps prepare-model
docker compose --profile setup run --rm --no-deps prepare-source
docker compose up --build -d
```

The source preparation command prints the sentinel ID to enter in the UI.
The model preparation service has internet access only for the explicit model
download and mounts no documents or secrets. Normal services remain on private
networks, with model assets mounted read-only in the worker.

The watcher waits for at least **three unchanged observations over 60 seconds**
before taking an immutable snapshot. The worker extracts, checks, chunks, embeds
and indexes it. A changed file creates a new version of the same document;
unchanged files do not create duplicate jobs. Unchanged accepted files are
rehashed daily; new/changed candidates are hashed during stability observations.

If you select `ready_manifest`, provide `filename.ext.ready.json` with exactly
`{"sha256": "<file SHA-256>", "bytes": <file size>}`. Without a producer completion
signal, stability plus repeated hashes is still a heuristic: a paused file copy
can appear complete.

To use another folder, change the watcher bind mount and the matching
`approved_mounts` entry in [settings.compose.yaml](./deploy/config/settings.compose.yaml).
For a NAS alias, also configure `network_mounts` and `mount_health_files`. The
host must independently verify share identity and live accessibility, then update
a protected JSON record containing `mount_alias`, `sentinel_id`, `healthy: true`
and an ISO-8601 `checked_at` timestamp with timezone. Records expire after 60 seconds.
A cached sentinel or existing directory alone does not establish NAS health.

### Monitor ingestion

- `docker compose logs -f watcher worker` shows job/source IDs and safe error codes.
- `GET /api/v1/sources/{id}/status` reports source health, file counts and deferred
  errors to authenticated curators/admins.
- In the Sources UI, **Check ingestion status** displays the same scan health
  and deferred-file counts.
- `GET /api/v1/jobs/{id}` reports stages, attempts and completion.
- `GET /api/v1/documents` lists active documents for readers and pending documents
  for curators/admins.

Source deletion is deliberately **frozen** in this increment. Missing files and
outages do not remove previously indexed content. Parser warnings, including
missing OCR, require review; there is not yet a review-resolution UI. Staged and
retired vector cleanup, cloud connectors, answer generation and production
certification remain follow-up work.

## Diagrams and detailed documentation

- [Architecture diagram](./Architecture.drawio): current components and planned RAG system.
- [Flow diagrams](./Flows.drawio): uploads, automatic ingestion and querying.
- [Documentation index](./docs/README.md): architecture, security, data model and design decisions.
- [API contract](./docs/api-contracts.yaml): full designed API; only the subset described below is implemented.
- [Deployment design](./docs/deployment-packaging.md): target production packaging, not a certified installation guide.

Open the editable `.drawio` files in draw.io / diagrams.net.

## AWS / Azure Kubernetes deployment

Use the [cloud deployment guide](./deploy/README.md) for a Helm chart and Terraform
entry points targeting **existing EKS or AKS clusters**, with operator-provided
databases, private storage and Secrets. Optional bounded autoscaling covers the
API, web and ingestion workers; the watcher stays single-replica.

Defaults are private, local-model-only and single-replica. This packages today's
ingestion/admin functionality, not a completed chat product. Cloud runtime,
multi-node storage and scaling still require deployment-specific validation.

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
commit, not an ingestion completion. The separate worker performs isolated
parsing and indexing asynchronously. Format/file limits default
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
