# Architecture and framework contract

Status: target architecture with staged implementation. Filesystem watcher,
isolated parser worker, local chunk/embedding adapters and verified Qdrant
publication now exist alongside authentication/admin/upload APIs. Query
orchestration, generation, OCR, cloud connectors, paid providers and automated
cleanup remain unfinished. See [current runtime](../README.md) and
[context authority](./README.md).

## Boundaries

Use a modular monolith with separate process entry points, not a distributed
microservice decomposition. API, watcher, worker, migration, and bootstrap use
one backend source tree/image and shared domain rules.

| Component | Framework/responsibility | Must not do |
|---|---|---|
| `web` | React/TypeScript UI, locally bundled PDF viewer, reverse proxy | Enforce authorization by UI visibility alone; fetch CDN assets |
| `api` | FastAPI/Pydantic; sessions, HTTP, LangGraph orchestration | Parse huge PDFs inline; expose storage directly |
| `watcher` | Typed filesystem/S3/Graph/Drive connectors, snapshots/reconciliation | Execute configured plugins; infer deletion from permission/transport failures |
| `worker` | Allowlisted parsers/OCR, chunking, index staging, cleanup | Read sources directly; bypass source revision fencing |
| `inference` | Shared local dense/sparse encoding and reranking | Download weights at runtime; accept public traffic |
| `ollama` | Preloaded local generation and claim verification | Cloud fallback or arbitrary tools |
| Provider gateway | Internal generation/embedding adapters, export and budget gates | Bypass consent, leak secrets, or silently switch vendors |
| `postgres` | Identity, authorization/lifecycle, jobs, runs, checkpoints | Store credentials in plaintext |
| `qdrant` | Dense/sparse search and RRF; rebuildable index | Be the authority for access or active versions |
| Artifact volume | Immutable source snapshots and derived evidence | Serve files without the authenticated API |
| Host mount/probe | SMB credentials, share identity, live mount health | Hand credentials or Docker socket to containers |

Only `web` publishes `127.0.0.1:8080`. Internal ports: API 8000, inference 8081,
Ollama 11434, PostgreSQL 5432, Qdrant 6333, optional model gateway 8082. Model/database routes have no public
ports. Network mode defaults to local models; approved connectors and separately
approved paid capabilities use restricted egress through the provider/connector
gateways, never unrestricted internet. Airgap allows neither. Mount only the approved folder read-only
into watcher, not all host storage. Source-code bind mounts are not production
mounts. Apply [security requirements](./security.md).

## Module and dependency direction

Planned backend packages:

- `api`: routing, authentication middleware, DTO conversion, SSE transport.
- `domain`: lifecycle, permission checks, identifiers, typed errors, invariants.
- `sources`: scan observations, source revisions, stable snapshot reconciliation.
- `providers`: typed model capabilities, profile selection, export and budget gates.
- `ingestion`: extraction, normalization, quality checks, chunking.
- `retrieval`: mandatory filters, candidate checks, fusion, evidence selection.
- `graph`: typed LangGraph state, bounded nodes/edges, result validation.
- `storage`: SQLAlchemy repositories, filesystem and typed Qdrant adapters.
- `jobs`: leases, fencing, retries, scheduling, handlers.
- `evaluation`: local datasets and scoring.

Domain rules do not import FastAPI, React, LangChain, or LangGraph. Entry points
call application/domain services; adapters implement persistence/model operations.
Avoid a generic repository framework or service bus until a concrete need exists.
Reuse one permission policy and one ingestion/publication service across HTTP,
batch, watcher, and worker entry points.

[Connector/parser contracts](./connectors-formats.md) normalize new formats/sources
without rewriting ingestion. [Provider policy](./models-cost-performance.md) routes
generation and dense embeddings independently; sparse encoding/reranking stay local.
Gateway is an internal adapter/service boundary, not a public model proxy.
In external-model mode deploy an internal `model-gateway` process from the backend
image (port 8082, never published) as the sole model egress path. It validates
authenticated service calls by run/job identity and rereads policy/export/budget
state; caller-supplied approval booleans are not authority. Disabled in airgap;
local capability adapters remain directly usable without internet.

## Planned implementation tooling

Use Python with Pydantic v2, SQLAlchemy 2, Alembic migrations, pytest, Ruff,
and static type checking. Use React with TypeScript and the existing frontend
toolchain once established. Choose a mutually supported Python minor and exact
package versions during bootstrap; do not guess compatibility from this document.
Produce lockfiles and pin container digests and model revisions then.
Do not install multiple competing package managers, ORMs, or queue systems.
Exact runnable commands belong in [testing](./testing.md) after verification.

LangChain owns integrations, not domain authorization. LangGraph is embedded in
the API process and owns explicit query state/control flow. Use PostgreSQL
checkpoints with authorized rehydration, not an in-memory production checkpointer.
LangSmith cloud tracing is off. Its self-hosted platform needs a separate licensed
deployment review; it is not assumed to be a free Compose service.

## Query invariants

Authenticate -> current RBAC/ownership -> model/export/budget policy -> resolve follow-up -> configured encode ->
tenant-filtered dense and sparse search -> canonical DB checks -> load permitted
evidence -> local rerank -> sufficiency -> configured generation -> validate claims/
citations -> final authorization/content check -> commit -> completed SSE.

- At most two searches total; no autonomous agent loop, shell, or web tools.
- Previous assistant replies are not authoritative document evidence.
- Return validated final answers, not unvalidated draft token streaming.
- Insufficient/unsupported evidence yields an explicit abstention.
- Dependency errors, cancellation, and authorization failure are distinct outcomes.
- One active run per conversation; reconnect replays events, not generation.
- Persist source IDs/format-specific locations, provider/policy/price/model
  revisions, and evidence manifest.

Initial tuning values, not fixed architecture: 40 candidates/branch, 50 fused IDs,
bounded expansion up to 200/branch, up to 50 reranked passages/eight selected chunks,
8,192 context tokens with roughly 1,024 output reserve if supported by model/host.
Count all prompts/history/evidence with the actual tokenizer; no silent truncation.

## Reliability and evolution

Use PostgreSQL leases and fencing, generation-specific writes, and bounded
idempotent retries. There is no Qdrant/PostgreSQL transaction; only a verified
generation's PostgreSQL active pointer makes evidence eligible.
See [publication protocol](./ingestion.md).

Start with one parser worker and one generation/verification run, at most four
pending generations; overflow returns 429. Initial total run budget is 150 s;
proxy timeout must be longer and SSE buffering disabled. Benchmark before tuning.
Do not retry uncertain model requests by submitting a second generation blindly.

Persist volumes, rotate local logs, health-check dependencies and loaded models,
and make missing weights a readiness failure. Reserve resources for the host.
CPU-only Docker is portable but not a speed promise; Apple Metal is not assumed
inside Linux containers. Native host inference requires an approved exception.

An added feature should extend an existing module and contract. A new vector model
requires a new fingerprinted collection and controlled reindexing, not a database
replacement. A new field requires a migration/DTO/test update, not an application
rewrite. Significant deviations follow [decision 0002](./decisions/0002-configurable-production-product.md).
Compose and Helm share the same modules/images; see [packaging](./deployment-packaging.md)
for shared artifact storage, global concurrency/budgets, and release gates.
