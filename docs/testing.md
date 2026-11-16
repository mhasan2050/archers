# Testing and acceptance contract

Status: acceptance requirements plus verified staged checks; not full product
certification. CI and complete RAG runtime validation remain pending.

## Verified implementation checks

- `backend/.venv/bin/python -m pytest backend/tests -q`
- `backend/.venv/bin/python -m ruff check backend/src backend/tests`
- `backend/.venv/bin/python -m mypy backend/src`
- `cd web && npm test && npm run build`
- With isolated `ARCHERS_TEST_DATABASE_URL` (database name ends `_validation`):
  `backend/.venv/bin/python -m pytest backend/tests/test_postgres_ingestion.py -q`.
  Covers populated 0001-to-0003 upgrade, concurrent duplicate uploads/scanners,
  SKIP LOCKED replicas, durable restart and stale lease fencing. Tests remove only
  their generated schemas.

For actual model/vector integration, also set `ARCHERS_TEST_MODEL_PATH` to an
explicitly prepared local embedding directory and `ARCHERS_TEST_QDRANT_URL` to
an isolated local Qdrant server. `test_indexing.py` checks real semantic similarity
without runtime downloads; `test_postgres_ingestion.py` covers folder discovery,
actual local embeddings, vector retrieval/tenant separation, replacement
activation/retirement and PDF page provenance. Neither test sends document text
to an external service. Without those explicit resources the optional tests skip.

The automatic ingestion suite also covers stability/restart deduplication,
completion manifests, oversized-file isolation, source health, cancellation,
failed-replacement preservation, integrity checks and review-required extraction.
macOS parser tests verify that network connection attempts, unrelated file reads
and original-file writes are denied. Linux Landlock/seccomp behavior still needs
container runtime validation; unsupported sandboxes fail closed.

A live API/watcher/worker run was verified with PostgreSQL 14, Qdrant 1.15.5 and
the pinned CPU model: saving a synthetic file activated it automatically after
the real stability interval, without a manual upload request. This is ingestion
verification, not complete RAG/query or production certification.
Docker daemon remains unavailable for image/build/runtime checks; Compose syntax
can be checked without it.

## Tooling and change workflow

During bootstrap, establish pytest for backend/domain/integration checks, Ruff
and type checking, TypeScript build checks and focused UI tests, Alembic migration
checks, an OpenAPI validator/compatibility check, and local container integration
tests. Pin versions and document exact commands only after executing them.
CI must use synthetic fixtures and preapproved model-free mocks by default;
optional local-model evaluations use preloaded assets, never company uploads.

For each behavioral change:

1. Read the relevant contract and existing tests.
2. Add a regression for the requirement, not merely a nearby proxy.
3. Implement a bounded change preserving existing contracts and stored data.
4. Run the smallest covering suite plus schema/API checks when affected.
5. Report exact commands/results and unverified assumptions; widen only as needed.

## Required executable checks

| Area | Scenarios and expected result |
|---|---|
| Role matrix | Every route/domain action for anonymous, reader, curator, admin, disabled user; reject unknown roles |
| Identity | Correct password hashing, generic login errors, expiry, logout, CSRF, trusted Origin/Host, session revocation and last-admin protection |
| Ownership | Other user's chat/events/feedback/checkpoints rejected even for admin; tenant boundaries enforced |
| Sources | Restart deduplication, health expiry, missing mount/sentinel, permission-denied subtree, nonoverlapping scans, source pause |
| Completion | Partial/paused copy, atomic rename, manifest/hash mismatch, changed-during-copy, same-stat replacement, symlink/path escape |
| Deletion | Exact count/time thresholds, healthy final lookup, bulk freeze, outage reset, reappearance, admin suppression survives curator rescan |
| Leases/publication | Expired worker cannot publish, source revision races, replacement rollback, Qdrant partial write, active-pointer checks |
| Parsing | Text, scanned, rotated, blank, multi-column, table, encrypted, corrupt, oversized PDF; explicit failure/review |
| Retrieval | Tenant filter on both branches, active generation check, no Qdrant text leakage, token budget, compatible sparse/dense fingerprints |
| Answers | Bounded searches, follow-ups, no prior-answer evidence, unsupported/contradictory claims, bad JSON, fabricated citation IDs |
| Run lifecycle | Idempotency, one run/conversation, 429 overflow, cancellation, SSE replay, stale cursor, interrupted run, no duplicated generation |
| Offline | No runtime egress/download/exporter/CDN/cloud fallback; only approved host SMB access |
| Connected modes | Only approved source/model destinations; remote source permission/cursor failures do not trigger deletion; no arbitrary redirect/SSRF |
| Format matrix | PDF/DOCX/PPTX/XLSX/Markdown/text/HTML/CSV fixtures, truthful location citations, archive limits, no macros/formulas/script execution |
| Paid policy | Independent generation/embedding switches, source/document approval, requester consent, mixed evidence refusal, revocation, missing keys, unsupported provider capabilities |
| Budget races | Concurrent API/worker reservations at limit, retries, uncertain billing, price revisions, cross-window accounting, no paid fallback |
| Packaging | Compose and Helm install/upgrade/restore with populated data, shared artifact access, migration fencing, no default secrets, SBOM/license/signature checks |
| Operations | Missing models fail readiness, restarts persist data, disk full, bounded retries, redacted logs, isolated backup restore |
| Contracts | All OpenAPI routes/DTOs match implementation; strict unknown fields; migration upgrade preserves representative prior rows |

Use actual PostgreSQL/Qdrant containers for persistence integration, not only
mock stores. Test each connector's real approved sandbox behavior as well as simulated
filesystem failures. Parser security/resource isolation needs deployment tests.
Measure threshold boundaries (just below/at/above), not only the happy path.

## RAG evaluation

Start with 20-30 representative PDFs and 50-100 human-reviewed questions with
expected supporting pages. Include codes/acronyms, scans, tables, cross-document
questions, conflicting evidence, follow-ups, and unanswerable prompts.
Keep company datasets in approved local storage; use synthetic equivalents in git.

Record dataset revision, parser/chunk/model/index/prompt revisions, hardware,
sample counts, denominators, failures, and human judgments. Measure recall@k,
supported/correct answers, citation provenance/support, abstention, cold/warm
p50/p95 latency, queue wait, throughput, and peak memory/disk.

Provisional pilot gates, to approve against the real corpus:

- Zero unauthorized disclosures in the defined access-control tests.
- Every released citation resolves to an authorized original PDF page.
- At least 90% recall@10 on the answerable subset.
- At least 90% human-reviewed supported/correct answers on the agreed subset.
- At least 90% appropriate abstention on the unanswerable subset.
- Mode-specific egress/privacy/budget checks and encrypted-backup restore succeed
  for both Compose and Helm certified topologies.

No latency SLA until hardware/model selection. Automated/LLM judges assist, not
replace human review or turn a finite test set into a universal safety guarantee.

Run [small/medium/large benchmark profiles](./models-cost-performance.md) with
equal quality gates for local/paid alternatives. Publish measured latency,
throughput, peak memory, freshness, and cost envelopes with release artifacts;
do not claim performance optimization from configuration alone.

## Documentation and contract checks

Check local links, OpenAPI syntax/structure/reference resolution, unique operation
IDs, route coverage, DTO validation, draw.io XML IDs/connectors/page bounds, and
alignment with the security matrix. Future CI should detect incompatible API and
schema changes. Instruction files guide agents but are not an enforcement mechanism.
