# High-Level Design: Configurable Self-hosted Knowledge Assistant

Status: target design with implemented administration/ingestion foundations.
Query generation and production benchmarking remain incomplete.

Implementation context: [contract index](./docs/README.md). This overview and its
diagrams explain the design; the indexed topic contracts own implementation
details and change rules.

The [cloud deployment guide](./deploy/README.md) covers the current Helm chart
and Terraform entry points for existing EKS/AKS clusters, including opt-in bounded
scaling. This does not certify cloud runtime or implement the full target design.

Companion document: [Low-Level Design](./LLD.md).

Editable diagram: [HLD architecture](./HLD.drawio). Open with the draw.io VS Code
extension or a local diagrams.net Desktop installation. Export PNG/SVG locally
from the diagram editor if an image is needed.

## 1. Goals and confirmed constraints

Build a packaged company document assistant that answers using authorized
multi-format evidence, with truthful original-document location citations.

Confirmed requirements:

- One company per installation, with Docker Compose and Kubernetes/Helm packaging.
- Default to local OSS models; external generation and embeddings are independently
  opt-in under explicit export consent, endpoint restrictions, and spending limits.
- Keep sparse encoding and reranking local; no automatic local-to-paid fallback.
- Use LangChain and LangGraph, with a clearly defined LangSmith integration path.
- Use a free, self-hosted vector database.
- Authenticate every interactive user with application-managed local accounts.
- Use RBAC for actions: reader, curator, and administrator.
- Allow every enabled authenticated user to read all active company documents;
  NAS filesystem ACLs are not mirrored into the application.
- Configurable filesystem/SMB/NFS, S3, SharePoint/OneDrive, Google Drive, and upload
  sources; withdraw only after confirmed deletion in an approved healthy scope.
- Allow PDF, DOCX, PPTX, XLSX, Markdown/text, HTML and CSV through validated parsers.
- Normalize content with page/slide/cell/line/row/block provenance, not PDF-only assumptions.
- Benchmark performance and cost; ship versioned releases with upgrade/restore gates.
- Hardware, document count, languages, and concurrency are not yet known.

This design covers ingestion, retrieval, generation, permissions, operations,
and evaluation. It does not claim that a single workstation provides high
availability, enterprise identity management, or unlimited capacity.

## 2. Architectural decisions

| Area | Decision | Reason |
|---|---|---|
| Application | Modular Python backend and worker from one source tree | Shared domain logic without microservice overhead |
| UI | Small React application, served by a local reverse proxy | Chat, uploads, administration, PDF citations |
| API | FastAPI with versioned REST endpoints and SSE | Typed contracts and progress streaming |
| Workflow | LangGraph embedded in the API process | Explicit, bounded query orchestration |
| Integrations | LangChain adapters | Local model and vector-store integration |
| Parsing | Allowlisted format adapters; Docling/local OCR where validated | Structure and format-specific provenance |
| Vector index | Qdrant | Dedicated dense and sparse retrieval with filters |
| Application store | PostgreSQL | Users, ACLs, jobs, versions, conversations, checkpoints |
| Background jobs | PostgreSQL-backed leased queue | Avoid a separate broker for a single-machine pilot |
| Discovery | Typed filesystem/S3/Graph/Drive adapters with durable inventory | Safe change/delete evidence and resumable cursors |
| Files | Local volume for Compose; shared PVC/object artifacts for K8s | Same immutable snapshot semantics across replicas |
| Generation | Local OSS via Ollama; approved paid provider adapter opt-in | Local-first with explicit privacy/budget gates |
| Embeddings/reranking | Local default; dense external opt-in, sparse/rerank local | Reuse models; version indexes when encoders change |
| Observability | Local redacted logs, metrics, evaluation artifacts | No mandatory external service |

### LangSmith limitation

LangSmith is not a free, offline Docker dependency equivalent to LangChain or
LangGraph. Its supported self-hosted platform is an Enterprise add-on and its
current documentation describes a Kubernetes deployment.

The baseline therefore:

1. Uses LangChain and LangGraph locally without hosted LangSmith.
2. Disables LangSmith tracing and all external exporters at runtime.
3. Keeps local evaluation datasets, results, and correlation IDs.
4. Defines optional LangSmith instrumentation, enabled only after a licensed
   deployment and its network/license requirements satisfy company policy.

If active LangSmith use is mandatory in the first release, this constraint must
be resolved through licensing and deployment review. Free software, strict
offline operation, and the complete LangSmith platform are not promised together.
Synthetic-only cloud experiments are a separate, explicitly approved activity,
not part of the production runtime.

## 3. Logical architecture

```text
Browser
   |
   v
Web / reverse proxy
   |
   v
FastAPI -------------------------------------- PostgreSQL
   |                                             |
   +-- LangGraph query workflow                  +-- Users, roles, sessions
   |       |                                     +-- Documents, versions, source inventory
   |       +-- Local inference: embed/rerank      +-- Durable jobs, checkpoints
   |       +-- Qdrant: dense + sparse search      +-- Conversations, audit
   |       +-- Ollama: local generation
   |
   +-- Upload / source-file access ---- Artifact volume
                                                ^
                                                |
PostgreSQL job queue ---------------------- Ingestion worker
                                                |
                                                +-- Docling / local OCR
                                                +-- Markdown + structured JSON
                                                +-- Local inference
                                                +-- Qdrant

NAS SMB share -> Host read-only mount -> Watcher -> Local immutable PDF snapshot
                                           |
                                           +-> PostgreSQL inventory + ingestion jobs
```

PostgreSQL is the authority for identity, permissions, active versions, and
document lifecycle. Qdrant is a rebuildable search index, not an authorization
authority. Immutable original bytes remain authoritative.

### Product extension boundaries

The text flow above shows the local PDF baseline, retained as the default path.
Reviewed source/parser adapters normalize into one ingestion pipeline; provider
adapters apply capability, export, budget and token gates before configured model
calls. They do not create a new application per source/format/vendor.

- [Sources/formats](./docs/connectors-formats.md): connector identities/cursors,
  format allowlists and truthful citation locators.
- [Models/cost/performance](./docs/models-cost-performance.md): local OSS defaults,
  supported paid providers, export approvals, atomic spending caps, benchmarks.
- [Packaging](./docs/deployment-packaging.md): identical OCI application images
  for Compose and Helm, storage/security/upgrade/restore certification.
- [Decision 0002](./docs/decisions/0002-configurable-production-product.md): approved
  evolution and compatibility of the preimplementation design.

## 4. Docker deployment

All long-running services restart unless explicitly stopped. Versions and image
digests are pinned during implementation; no production image uses `latest`.

| Service | Deployment | Internal interface | Persistent state |
|---|---|---|---|
| `web` | Static UI and reverse proxy | HTTP 8080 | None; optional local TLS state |
| `api` | FastAPI and embedded LangGraph | HTTP 8000 | Shared artifacts; PostgreSQL |
| `worker` | Ingestion, maintenance, job polling | No public port | Shared artifacts and parser cache |
| `watcher` | Configured connectors, snapshots, reconciliation | No public port | PostgreSQL inventory/cursors; artifact staging |
| Provider gateway | Internal capability/export/budget adapter | Internal-only | PostgreSQL profiles, reservations/usage |
| `inference` | Local dense/sparse embeddings and reranking | HTTP 8081 | Preloaded model cache |
| `ollama` | Local text generation | HTTP 11434 | Model volume |
| `postgres` | Application database and job queue | TCP 5432 | PostgreSQL data volume |
| `qdrant` | Vector index | HTTP 6333 | Qdrant data volume |
| `migrate` | One-shot schema migration | None | PostgreSQL |
| `bootstrap` | Explicit one-shot admin setup | None | PostgreSQL |

Default host binding: `127.0.0.1:8080` for `web` only. Internal databases and model
ports are not published. Local inference routes have no browser access.

Airgap runtime has no external egress. Connected sources use scoped connector
egress; optional paid models use separately gated provider egress. Connection to
SharePoint/Drive/S3 is not consent to upload content to an LLM.
Verify host-port access on the selected Docker engine. All UI fonts, scripts,
PDF viewer assets, and worker models are bundled locally.

The host mounts the SMB share using a read-only service account; only `watcher`
receives a read-only bind mount of that folder. SMB credentials remain in the
host's protected credential store, never images or application configuration.
The host's approved NAS connection is an explicit exception to network isolation;
containers have no direct SMB credentials. Cloud connectors/paid profiles need
their separately approved network modes, secrets and budgets. Validate
Docker Desktop mount visibility and recovery on the selected OS.

### Model preparation versus offline runtime

Installation may require internet access to obtain images and model weights.
Do this before loading company documents, or import verified offline bundles.
The normal runtime must never auto-download missing models or use cloud fallback.
Disable Ollama cloud features according to the pinned release configuration.
Missing assets cause a visible readiness or ingestion failure.

### GPU portability

- CPU-only containers are the portable baseline, not a latency guarantee.
- NVIDIA acceleration requires a supported host, drivers, and container toolkit.
- Do not assume Docker Desktop exposes Apple Metal to a Linux Ollama container.
- On Apple Silicon, an optional native Ollama process can improve acceleration,
  but it is an explicit exception to the all-in-Docker deployment. It needs a
  reviewed host-network path and separate egress controls.
- GPU profiles are selected after hardware discovery and tested independently.

## 5. Document ingestion

1. Configured connectors detect changed supported files, verify completion/version
   evidence, and stage immutable snapshots. NAS polling uses the protocol below.
2. The shared ingestion service validates the snapshot and atomically creates
   a document version plus an ingestion job after durable artifact storage.
   Curator/admin manual uploads remain available through the same service.
3. A worker claims the job using a renewable database lease.
4. Docling extracts content and provenance; OCR is applied where appropriate.
5. The worker writes Markdown, structured JSON, and a quality report.
6. Quality checks flag unreadable pages, missing provenance, or bad extraction.
   Severe failures require review and cannot silently publish.
7. Structure-aware chunks receive format locations and deterministic identifiers.
8. Generate sparse vectors locally; dense vectors use the configured local or
   independently approved external embedding profile with batch spend reservations.
9. The worker writes a staged generation to Qdrant and verifies its completeness.
10. PostgreSQL atomically activates the new version and index generation.
11. Maintenance jobs remove obsolete generations after their retention window.

A failed update does not replace the previous active version. A failed first
upload is visible as failed and is not searchable.

### NAS discovery and source lifecycle

- Poll every 30 seconds initially; filesystem events may wake the poller but
  never replace full reconciliation. Detection is near-real-time, not instant.
- Require stable size/mtime observations over at least 60 seconds. Prefer the
  producer writing a temporary name then atomically renaming, or a ready manifest
  with expected size/hash. Without a producer completion signal, stability checks
  reduce risk but cannot prove a paused copy is complete.
- Stream-copy to local staging with hashing; verify source metadata before/after,
  validate the PDF, then commit a version only if source revision still matches.
- Persist source-path/hash inventory; restart scans do not duplicate jobs.
- Treat the same path with changed content as a replacement. A rename is initially
  a new path plus an absent old path, not an inferred identity move.
- Confirm deletion only after three complete healthy scans spanning at least five
  minutes, with verified share identity and mount health. Recheck before withdrawal.
- Mount failures, incomplete traversals, missing health checks, or suspicious
  bulk disappearance pause deletion and alert administrators; they are not proof
  that every document was deleted. Retain the last active indexed versions during
  an outage and show a freshness warning.
- After confirmed deletion, immediately make the document unavailable in
  PostgreSQL, cancel pending publication, then clean local snapshots and vectors
  asynchronously under the retention policy. The application never deletes NAS files.

### Artifact strategy

Store original bytes, Markdown, parser-native JSON, normalized blocks,
chunk manifest, and extraction quality report.

Markdown is an inspection/export format, not the sole provenance source.
PDF citation page numbers come from structured extraction, not inferred Markdown
positions. Tables retain headers and row relationships. Chart-heavy documents
are flagged for manual review or a later explicitly local visual-processing path.
Other formats cite slides, sheets/cells, lines, rows or normalized blocks; never
invent PDF pagination. Original downloads and sanitized normalized views are authenticated.

## 6. Query workflow

1. Authenticate the user and authorize access to the conversation.
2. Resolve a follow-up question using bounded conversation history.
3. Enforce the role's query permission and derive the tenant filter server-side.
4. Check requester/export policy and budget for external encoding if configured;
   encode using the index's pinned profile, then run filtered hybrid retrieval.
5. Fuse rankings, verify candidate IDs against PostgreSQL, and load only permitted
   active evidence for local reranking.
6. Select a bounded evidence set and evaluate sufficiency.
7. Optionally perform one revised search; never run an unbounded agent loop.
8. Generate via local OSS default or an approved external generation profile,
   only after source/document consent and atomic worst-case cost reservation.
9. Validate citation IDs, format provenance, current authorization/export revisions, and claim support.
10. Return the final answer or an explicit abstention.

The UI receives progress events during processing. Draft answer tokens are not
displayed before validation in the initial release. This avoids presenting
unvalidated citations and makes final authorization checks meaningful.

No evidence, insufficient evidence, and infrastructure failure are different
outcomes. A model outage is an error, not "no information found."

## 7. Security and privacy

- Local, administrator-provisioned accounts; no public registration.
- Password hashing, secure sessions, CSRF defenses, and rate-limited login.
- Deny-by-default server-side role checks and tenant boundaries.
- Reader: chat and sources; curator: reader plus ingestion/review/retry; admin:
  curator plus accounts, roles, source configuration, and document withdrawal.
- All three roles can read all active documents in their tenant. Conversations
  and feedback remain private to their owner; administrators do not bypass this.
- Apply authorization to both retrieval branches before inference.
- Recheck candidates and final evidence against authoritative database state.
- Reauthorize citations, conversation history, source files, and downloads.
- Treat all imported content as untrusted evidence, never system instructions.
- No shell execution, web browsing, or arbitrary tool use by the query graph.
- Restrict parser privileges and enforce file/page/time/resource limits.
- No document text, prompts, passwords, or answer bodies in ordinary logs.
- Use encrypted host storage and encrypted backups; local does not mean encrypted.
- Record administrative actions, document access, and permission changes locally.

Loopback-only HTTP is a development boundary. Before enabling LAN access, add
TLS with a company-trusted certificate, host firewall rules, session hardening,
and a security review. Do not expose this Compose stack directly to the internet.

Revocation takes effect for requests authorized after its database commit.
Recheck before generation and final delivery; discard stale runs. Previously
delivered answers cannot be recalled. Saved answers are hidden if their evidence
is no longer authorized; document deletion schedules derived-state cleanup.

## 8. Capacity and performance planning

These are planning envelopes, not measured requirements or promises.

| Profile | Suggested host | Intended workload |
|---|---|---|
| Evaluation | 16 GB RAM, 4+ CPU cores, SSD | Small representative corpus, one query, small quantized model |
| Pilot baseline | 32 GB RAM, 8+ CPU cores, SSD | Initial company pilot with controlled concurrency |
| Larger local pilot | 64 GB RAM, GPU with suitable VRAM, SSD | Larger corpus and/or larger models after benchmarking |

Reserve memory for the OS and Docker VM as well as OCR, inference, databases, and
model context/KV caches. Start with one active generation and one ingestion worker.
Pause or throttle heavy ingestion during interactive usage.

For orientation, one million dense vectors with 768 float32 dimensions occupy
about 3.07 GB (2.86 GiB) before vector-index, sparse-index, payload, and storage
overheads. PDFs, OCR artifacts, model weights, backups, and headroom are additional.
Measure actual chunks/page and bytes/chunk before sizing the full corpus.

Begin with a local instruction model in the approximately 3B-8B range at a suitable
quantization. Benchmark a 768-dimensional English embedding model such as
`BAAI/bge-base-en-v1.5` and a compatible small local cross-encoder. These are
evaluation candidates, not fixed choices. Confirm languages, licenses, token
limits, and quality before freezing model revisions.

## 9. Reliability and operations

- Separate liveness from dependency/model readiness.
- Use durable jobs, heartbeats, bounded retries, and idempotent writes.
- Use per-document publication serialization and generation-specific indexing.
- Persist state outside container writable layers.
- Back up PostgreSQL, original PDFs, derived artifacts/configuration, and model
  manifests. Qdrant snapshots shorten recovery; indexes must also be rebuildable.
- Test restore to an isolated local deployment, not just backup creation.
- A single host is a single point of failure; define pilot RPO/RTO after restore
  drills rather than assuming high availability.
- Retain errors and metrics locally with disk quotas and rotation.

## 10. Quality gates and rollout

1. Extraction spike: 20-30 representative PDFs, including scans and tables.
2. Retrieval baseline: 50-100 human-reviewed questions with expected source pages.
3. End-to-end slice: NAS discovery, stable snapshot, job tracking, cited answers.
4. Pilot hardening: local login/RBAC, source reconciliation, account revocation,
   deletion, offline operation, backup/restore.
5. Certify format/connector matrices, Compose/Helm upgrades and restores.
6. Publish measured local/paid quality, latency/resource/cost benchmark envelopes.

Provisional release gates:

- No unauthorized content in adversarial permission and cross-user tests.
- All released citations resolve to authorized original locations.
- At least 90% retrieval recall@10 on the agreed answerable evaluation subset.
- At least 90% human-reviewed supported/correct answers on the agreed subset.
- At least 90% abstention on the agreed unanswerable subset.
- Airgap has no outbound calls; other modes allow only approved connectors and
  capability-specific paid calls within consent/budget limits. No hosted tracing.
  Test connector loss without accidental withdrawal.
- Successful recovery of a representative backup.

Report counts, denominators, corpus composition, and failures with every score.
Quality targets require stakeholder approval; latency targets follow hardware
selection and include cold/warm p50/p95 and queue wait.

## 11. Deferred features

Shared SaaS, SSO/remote ACL mirroring, automatically configured HA databases,
multimodal chart interpretation, unrestricted agents, and active licensed LangSmith
remain outside the first release. K8s shared storage/replicas require certification;
Kubernetes and package availability alone do not guarantee production performance.

## 12. References

Public documentation consulted on 2026-11-14:

- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Self-hosted LangSmith and licensing](https://docs.langchain.com/langsmith/self-hosted)
- [Ollama Docker deployment](https://docs.ollama.com/docker)
- [Ollama configuration FAQ](https://docs.ollama.com/faq)
- [Docling](https://docling-project.github.io/docling/)
- [Qdrant hybrid retrieval](https://qdrant.tech/documentation/search/hybrid-queries/)
