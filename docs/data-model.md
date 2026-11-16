# Data model and migration contract

Status: logical schema baseline, not executable DDL. SQL migrations are required
before persistence is implemented. [API DTOs](./api-contracts.yaml) deliberately
omit private storage fields.

Implementation coverage: migrations 0001 and 0002 implement identity, source
configuration, audit/idempotency/throttles and manual documents/versions/jobs.
Migration 0003 adds durable source-file observations, append-only scan summaries,
current `source_scan_status`, index generations and chunk metadata. Active version
and generation foreign keys are tenant/document/version scoped. Source version
linkage and scan revision are also pinned in ingestion job payloads. The worker
creates active generations only after vector verification and current lifecycle
checks; upload acceptance alone never activates a document. Conversation state,
remote cursors and billing tables remain designs. See [runtime scope](../README.md).

## Global rules

- PostgreSQL is authoritative. Qdrant and derived files are rebuildable.
- IDs are UUIDs; timestamps are UTC `timestamptz`; revisions are positive `bigint`;
  hashes are validated SHA-256 hex strings; sizes/counts are nonnegative.
- Every company-scoped row carries `tenant_id`. Use composite unique keys on
  `(tenant_id, id)` and composite foreign keys to prohibit cross-tenant references.
- Persist enums/check constraints for controlled states. Unknown input is rejected.
- Use server-generated file locators; never store user-supplied executable paths.
- `created_at`/`updated_at` apply to mutable records. Audit entries are append-only.
- Public resource revisions are optimistic concurrency tokens. Mutable API
  resources use the wire field `revision`; internal `acl_revision` is the
  authorization/lifecycle revision and is not a document-group ACL.

## Identity

| Entity | Required logical fields | Constraints/ownership |
|---|---|---|
| `tenants` | ID, name, acl_revision, model/export policy revision | One company per installation; no tenant signup |
| `users` | ID, tenant, username, password_hash, role, revision, disabled_at nullable | Tenant-unique normalized username; role reader/curator/admin; Argon2id hash never returned |
| `sessions` | ID, tenant, user, token_hash, csrf_secret, issued_at, expires_at, revoked_at nullable | Globally unique token hash; absolute expiry; no plaintext session token |

No `groups`, `user_groups`, or `document_groups` tables are needed for the
selected visibility policy. Never add per-document restrictions incidentally
through role names: they require a separate approved access-policy change.

## Sources and reconciliation

| Entity | Required logical fields | Constraints/ownership |
|---|---|---|
| `sources` | ID, tenant, connector_kind, typed config JSON, enabled, revision, format allowlist, scan policy, export flags, health_state, last_healthy_scan_at, lease owner/expiry/fencing token | Connector config discriminated/validated; approved scopes/endpoints and secret refs only; no raw secrets |
| `source_scans` | ID, tenant, source, started_at, completed_at, complete, healthy, error_code nullable, observed_count | Completed summaries immutable; failed/incomplete scans cannot prove absence; deletion alerts remain planned |
| `source_scan_status` | Tenant/source, observed_at, last_healthy_at, next_scan_at, error_code, observed_count | Mutable polling schedule/current health; not a substitute for append-only summaries |
| `source_files` | ID, tenant, source, item_key, display_path, remote_revision nullable, document_id nullable until snapshot accepted, desired_revision, observed_size/mtime/hash, stable_since nullable, last_seen_scan_id, missing_since/count, state, suppressed | Unique `(tenant, source, item_key)`; filesystem key is canonical path, remote key is stable provider ID/object key |
| `source_observations` | ID, tenant, scan, source_file or newly observed path, size/mtime/hash nullable, observation_state, error_code nullable | Includes unreadable paths; one finalized path observation/scan |
| `source_cursors` | Tenant/source, cursor kind/value, scope revision, last committed scan/watermark | Advance with durable observation/job transaction; expired cursors resync safely |

Source state: `DISCOVERED`, `STABILIZING`, `SNAPSHOTTING`, `TRACKED`,
`MISSING_PENDING`, `SOURCE_DELETED`, `ERROR`. Suppression is an independent admin
flag; scan/restore logic must not accidentally clear it.

Deduplication scopes to source/path/desired revision and content, not a global
hash-existence service. Same item key + changed bytes creates a new document version.
Filesystem rename creates a new document; stable remote item IDs retain identity
through label changes. Source desired revision increments on replacement, deletion, and
reappearance. Source-scan health is separate from document availability.

## Corpus and generations

| Entity | Required logical fields | Constraints/ownership |
|---|---|---|
| `documents` | ID, tenant, title, origin (`manual`/`nas`/`connector`), manual export flags, active_version_id nullable, active_generation_id nullable, revision, acl_revision, availability | NAS retained as legacy filesystem origin; source export policy authoritative for source-managed content |
| `document_versions` | ID, tenant, document, sequence, source_hash, bytes, format, MIME, original filename, page_count nullable, artifact_root, parser_revision, source_file/revision nullable, state, quality_report | Source-managed versions require source linkage; immutable original; page_count only meaningful for PDF |
| `index_generations` | ID, tenant, version, collection, model/pipeline fingerprints, expected_chunk_count, state, attempt/fencing references | A generation belongs to exactly one version and attempt |
| `chunks` | ID, tenant, document, version, generation, ordinal, text_locator, heading_path, token_count, text_hash, typed locations JSON | Unique generation/ordinal; format-specific locations checked against immutable normalized/original snapshot |

Document availability: `PENDING`, `ACTIVE`, `SOURCE_DELETED`, `SUPPRESSED`,
`DELETED`. Processing state lives on the version, not on the active document:
a failed replacement must not mark the existing active version failed.

Version states: `RECEIVED`, `VALIDATING`, `EXTRACTING`, `QUALITY_CHECK`,
`REVIEW_REQUIRED`, `CHUNKING`, `EMBEDDING`, `INDEXING`, `VERIFYING`, `READY`,
`FAILED`, `CANCEL_REQUESTED`, `CANCELLED`. Generation states: `STAGING`,
`VERIFIED`, `ACTIVE`, `RETIRED`, `FAILED`. Enforce allowed transitions in domain
logic and verify them transactionally.

An active pointer is switched only after index verification and row-locked
source/version/fencing checks. Cleanup must not delete active/live-leased
generations. No distributed transaction or exactly-once delivery is assumed.

### Provenance and artifacts

Each chunk records contributing block IDs, format-specific locations, extraction
method, and optional boxes. PDF uses physical pages; other formats use slide,
sheet/cell, line, row, or normalized block locators per
[formats](./connectors-formats.md). Printed page labels are separate.
Markdown is not an authoritative location map.

Artifact layout:

```text
/data/tenants/<tenant>/documents/<document>/versions/<version>/
  original.<validated-format-extension>
  document.md
  docling.json
  blocks.jsonl
  quality.json
  generations/<generation>/chunks.jsonl
```

Existing PDF `source.pdf` locators remain valid; do not rename retained artifacts
destructively. New versions record the actual original locator and MIME.
Private snapshots allow answering during connector outages. Write temp -> flush/
durable finalization -> atomic rename -> DB reference commit. Sweep abandoned
staging files after a grace period; never expose or index them as complete.
For S3-compatible artifact storage use immutable object IDs and verified manifests
before DB reference commit; do not assume filesystem rename across replicas.

## Jobs and conversations

| Entity | Required logical fields | Constraints/ownership |
|---|---|---|
| `jobs` | ID, tenant, kind, typed payload, idempotency key, state, attempts, cancel_requested, available_at, lease_owner/expiry, fencing_token, heartbeat, sanitized error | Unique scoped idempotency; payload contains IDs/revisions, not PDF bodies |
| `conversations` | ID, tenant, owner, title, revision, expires_at, deleted_at nullable | Only owner reads/mutates, including when requester is admin |
| `query_runs` | ID, tenant, conversation, requester, client_request_id, status, consent, pinned model/export/index revisions, cancel_requested, deadline, authorization_revision, run lease, sanitized error | Unique conversation/client_request_id; consent never inferred; at most one active run/conversation |
| `messages` | ID, tenant, conversation, run nullable, role, content, status, evidence manifest, model/prompt revisions | Final assistant message unique/run; source-dependent content gated on history reads |
| `run_events` | tenant, run, sequence, event_type, payload, created_at | Primary key `(tenant, run, sequence)`; durable before delivery |
| `idempotency_records` | ID, tenant, actor/service, method, route/resource scope, key, request fingerprint, result reference, expiry | Unique scope/key; covers non-job mutations too; conflicting payload returns 409 |
| `feedback` | ID, tenant, owner, assistant_message, rating, comment nullable | Requester owns message/conversation; do not allow feedback on another user's content |
| `audit_events` | ID, tenant, actor/service, action, resource ID, outcome, request ID, created_at | No raw text, secrets, or mutable history |

## Model policies, costs, and safe caches

| Entity | Fields/invariants |
|---|---|
| `model_profiles` | Deployment-imported ID/revision, capability/provider/model, dimensions/token limits, endpoint/secret references, price revision; no raw key; immutable revision |
| `model_policy` | Tenant active generation/embedding profile revisions, external capability switches, policy revision, active corpus/index routing; runtime API cannot enable deployment egress |
| `price_catalog` | Operator-imported provider/model/currency/unit rates, effective interval/revision; integer micro-units, unknown price cannot be zero |
| `budget_windows` | Tenant/capability/month UTC, finite allowance, committed/reserved totals, revision; row-locked atomic checks |
| `cost_reservations` | Run/job/call ID, policy/profile/price/window revisions, worst-case amount, status, actual units/cost nullable; unique call; uncertainty remains reserved |
| `model_usage` | Call ID, provider/model revision, input/output/cached/embedding units, confirmed/estimated charge, latency/error; no prompt/text |
| `artifact_cache` | Tenant/content/parser/chunker/encoder fingerprint, artifact reference, expiry; no cross-tenant hash existence |

See [cost policy](./models-cost-performance.md) for reservations, reconciliation,
export revocation and atomic corpus embedding-profile promotion. Rollouts pin
profiles/index revisions and must not mix query encoders across active collections.
Provider keys and secret values never live in profile rows or API responses.

Job states: `PENDING`, `RUNNING`, `RETRY_WAIT`, `SUCCEEDED`, `FAILED`,
`CANCELLED`. Run statuses: `queued`, `running`, `answered`, `abstained`,
`failed`, `cancelled`. Terminal states never revert automatically.

Job kinds are `scan`, `snapshot`, `ingest`, and `cleanup`. They share leases and
error semantics but have distinct validated payloads. Version/generation stage
progress is not a new job state. A cancellation request is recorded separately
until the handler acknowledges it; 202 never implies immediate termination.

Claim idempotency keys and create referenced mutations in the same transaction.
Persist references rather than response bodies containing secrets or raw content;
rehydrate under current authorization on replay. Compute keyed fingerprints for
sensitive request fields so a stored fingerprint is not an offline password
oracle. Run creation uses its conversation/client request ID instead of a second
HTTP idempotency key.

LangGraph PostgreSQL checkpoints use a separate schema and the library's
supported migrations. Namespace by tenant/conversation and authorize before every
load. Persist evidence references rather than raw passages where feasible;
never return sensitive checkpoint state through diagnostics.

## Indexes and Qdrant

Create indexes for ready jobs, expired leases, tenant/source/path, scan completion,
active generations, source missing counters, owner/conversation history, event
replay, and session expiry. Index selection is verified with representative data.

Qdrant collections are fingerprinted by embedding and pipeline. Point UUIDs derive
from tenant/version/generation/ordinal. The current profile stores named vectors:
`dense` (384-dimensional cosine, checksum-pinned all-MiniLM-L6-v2 ONNX with mean
pooling/L2 normalization) and `sparse` (stable SHA-256 term hashing with log-TF
weights). The current sparse adapter is not BM25; a corpus-aware BM25 profile
requires its own fingerprint and reindex. The earlier 768-dimensional candidate
is not an executable default. Payload IDs: tenant, document, version, generation,
ordinal. Text and source locations stay in private artifacts/PostgreSQL.
Index tenant/document/generation payloads; do not return raw text from Qdrant.
Never query a collection with an incompatible encoder/tokenizer.

## Migration and retention rules

Use Alembic versioned migrations once bootstrapped. No reset/recreate workflow
against populated data. Test upgrade from prior revision with representative rows;
use nullable/defaulted expansion, backfill, validation, then contraction.
Do not rewrite applied migrations. Breaking changes require approved transition.
The logical model becomes a migration-backed schema, not a second hand-maintained
DDL file.

Withdrawal is immediate logical unavailability followed by durable cleanup.
Delete affected persisted answers/checkpoints under the approved policy while
retaining minimal non-content audit evidence. Backup retention is not physical
erasure. Initial retention proposals and restore safeguards remain in
[LLD operations](../LLD.md#12-observability-retention-and-recovery) until approved.
