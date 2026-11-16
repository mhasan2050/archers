# Ingestion and source lifecycle contract

Status: required protocol with provisional operational timings. See
[schema](./data-model.md), [security](./security.md), and [API](./api-contracts.yaml).

## Current implementation

The `archers watch` process automatically polls enabled filesystem sources and
creates immutable snapshots and durable jobs for stable new/changed files.
`archers worker` runs isolated extraction, quality checks, tokenizer-bounded
chunking, local dense/sparse encoding, Qdrant staging/verification and fenced
PostgreSQL activation. Manual uploads use the same worker. Prepare local model
assets explicitly; no model downloads happen inside either runtime process.

Local source identity uses `.archers-source-id`. NAS aliases additionally require
`network_mounts` and `mount_health_files` in operator configuration. The protected
host record contains `mount_alias`, `sentinel_id`, `healthy` and timezone-aware
`checked_at`; it expires after 60 seconds. Host probe deployment is operator work.
Paths are opened relative to directory descriptors with no-follow checks.
Private artifact/model subtrees, hidden/temp names and symlinks are excluded.
Completed scan summaries are append-only; current scan status and file
observations survive restarts. Scan limits and deferred file errors are visible
via `GET /sources/{id}/status`.

The initial watcher serializes a bounded scan per tenant using the existing
tenant transaction lock. This favors correctness over scan/API concurrency;
large deployments must measure lock latency and tune scan budgets. Hashing is
limited per cycle; deferred candidates are picked up in following cycles.
Accepted unchanged files are rehashed daily, not on every poll.

Warnings or empty extraction enter `REVIEW_REQUIRED`; OCR and review-resolution
workflows are not implemented. Automatic source withdrawal is frozen, even after
healthy absences. Generation/orphan cleanup is not yet automated. A failed
replacement keeps the previous active generation. Full query orchestration and
answer generation remain separate unfinished work; global RAG readiness stays 503.

The sections below describe the full target protocol. Timings and chunk sizes
are design ceilings; the selected model's actual tokenizer/sequence limit wins.

The implemented local profile uses all-MiniLM-L6-v2 at pinned revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41` (model card: Apache-2.0).
Explicit setup downloads only reviewed checksum-verified assets. Runtime
embedding uses ONNX CPU mean pooling and normalization, producing 384 dimensions.
Chunks split each normalized block at original tokenizer character offsets,
retaining contributing format locations. The 256-token model limit includes
two special tokens, allowing at most 254 content tokens with up to 60 overlap.
Cross-block heading reconstruction and repeated table headers are not yet
implemented. Sparse encoding uses deterministic hashed log-term-frequency
weights, not corpus-trained BM25. Any profile/pipeline change requires a distinct
fingerprinted collection and controlled reindex; incompatible active profiles
cannot be mixed by publication.

Source adapters, format allowlists and normalized citations are defined in
[connectors/formats](./connectors-formats.md). The filesystem protocol below
specializes common health/snapshot/publication rules; remote sources use their
authoritative cursors/conditional reads instead of guessed SMB completion rules.
Source connectivity is independent of [model export](./models-cost-performance.md).

## Source boundary

Filesystem/SMB/NFS is mounted read-only by the host/CSI using protected credentials. Only watcher
gets the approved bind mount at `/sources/company-pdfs`; parser/API/model services
use immutable local snapshots. Never mount SMB inside a privileged container.
Create sources only from deployment-allowlisted mount aliases, not arbitrary
URLs/paths. Cloud sources select shipped adapters, approved scopes, and secret refs.
All imported documents are visible to all authenticated tenant users.

Require a known share sentinel UUID plus a host-generated live mount-health
record. The host checks expected server/share identity and uncached accessibility.
Treat records older than 60 seconds as unhealthy. A directory existing in a
container or a cached sentinel alone does not prove a healthy SMB mount.
Disable automatic creation of an empty host bind-mount directory.
Host probe implementation remains OS-specific and must be verified before pilot.

## Discovery and snapshots

1. Lease one scanner per source with renewable fencing. Poll recursively every
   30 seconds initially for mounted sources; remote polling respects provider
   quotas/cursors/backoff. Do not overlap scans or rely on SMB notifications.
2. Check fresh mount health/identity before and after the scan. Skip excluded formats,
   partial/temp, symlink, escaped, and nonregular paths. Enforce traversal budgets.
3. Persist observations including errors. Any listing/stat/access failure marks
   deletion reconciliation incomplete and produces a visible alert.
4. Use `(source, normalized relative path)` identity with explicit share case
   semantics. New path -> new document; same path + new hash -> new version.
5. Require three unchanged size/mtime observations spanning at least 60 seconds.
   Prefer producer atomic final rename or ready manifest with expected hash/size.
6. Claim a fenced snapshot job. Stream to local temp while hashing; verify actual
   bytes, pre/post source stat, format/resource limits, parser validity, and manifest if any.
   Changed-during-copy files return to stability observation.
7. If no producer completion protocol exists, require a second independent source
   hash after the stability interval before publishing. This remains a heuristic:
   paused producers can appear stable. Surface this limitation to source admins.
8. Atomically finalize snapshot, then transactionally verify source enabled state,
   desired revision, lease, suppression, and lifecycle before creating the version/
   ingestion job. Duplicate content/revision must not create duplicate jobs.

Preserve pending version identity across restarts. Daily full hash reconciliation
initially catches same-size/same-mtime replacements. Compare against source
inventory, not just active chunks. Rename is new path plus confirmed old-path
absence; no automatic identity transfer.

## Durable ingestion

State sequence: `RECEIVED -> VALIDATING -> EXTRACTING -> QUALITY_CHECK ->
CHUNKING -> EMBEDDING -> INDEXING -> VERIFYING -> READY`.
Quality failures go to `REVIEW_REQUIRED`; permanent errors to `FAILED`;
cancellation uses `CANCEL_REQUESTED -> CANCELLED`. Do not skip bad pages silently.

Claim jobs with PostgreSQL `FOR UPDATE SKIP LOCKED`, commit ownership and a fencing
token, initially lease for 120 seconds and heartbeat every 20. A stale owner cannot
commit stages/publication. At most three attempts for transient faults; permanent
validation failures do not loop. Attempt-specific staging isolates stale workers.

Allowlisted parsers (Docling/local OCR where appropriate) produce original snapshot, Markdown, parser JSON, normalized
blocks, quality report, and chunks. Flag empty/nonblank pages, broken reading order,
missing provenance, OCR errors, and malformed tables. Curator/admin review is
audited and cannot invent location mappings. No PDF page fabrication for other formats.

Initial chunk target 450 tokens, hard cap 480 for the candidate 512-token embedding
model, up to 60 overlap. Preserve headings, table headers, and contributing
format locations. Actual embedding/reranker tokenizer limits govern; reject silent
truncation. Oversized tables/rows need explicit split/review.

Encode sparsely locally and densely through the pinned local or explicitly approved
external profile. Paid calls require source approval, budget reservations and
endpoint/secret gates with no encoder fallback. Validate finite values,
dimensions, sparse indices, output cardinality, and fingerprints. Reuse matching
query/document BM25 vocabulary/configuration. Stage a generation in Qdrant, wait
for write completion, verify expected IDs/count, then row-lock the document and
source-file revision to activate in PostgreSQL.

No Qdrant/SQL shared transaction exists. Queries reject staged/retired generations
through canonical DB checks. Failed replacement leaves the old active version.
Newer intended revisions, source deletion, suppression, cancellation, or expired
leases prevent stale publication. Retire old generations and clean asynchronously.

## Deletion, outages, and reappearance

Remote sources use complete scoped enumeration or provider deletion tombstones
plus confirmed absence, with provider-specific health and permission checks.
403/expired cursor/filter exclusion never proves deletion. Cursor checkpoints
commit only with durable observations/jobs. Filesystem thresholds below remain
the default conservative absence policy; other adapters must certify equivalent
proof, review, and bulk-deletion safeguards.

- Missing counters advance only on complete healthy scans with fresh positive
  mount identity/health evidence before and after traversal.
- Confirm with three healthy absences spanning at least five minutes plus a direct
  final healthy not-found lookup. Timeout/permission/root errors are not not-found.
- Reset absence confirmation across outages. Keep the last active corpus and
  display a freshness warning while ingestion/deletion reconciliation pauses.
- Freeze withdrawals for over 20% disappearing files or a newly empty nonempty
  corpus. Admin review plus a fresh healthy rescan is mandatory; approval never
  bypasses mount-health checks.
- Lock document/source-file, recheck desired revision, mark `SOURCE_DELETED`,
  increment revision, cancel publication, and enqueue local/vector/derived-state
  cleanup. Source files on NAS are never modified/deleted by this application.
- Reappearance increments desired revision and requires fresh successful ingestion.
  It does not resurrect old answers/checkpoints or a manually suppressed document.
- Source pause is not deletion. Curator rescan cannot clear admin suppression.
  Explicit admin restore schedules a new snapshot; absence can still block it.
- Manual documents are independent of connector deletion. Replacing source-managed
  documents through uploads returns 409; edits belong on their original source.

## Operational visibility

Persist scan status, freshness, backlog, stability decisions, error codes, deletion
alerts, and lifecycle audit. Curator/admin can inspect detailed status and request
rescan; readers receive only general stale-corpus warnings. Document paths may
contain sensitive information and must not enter ordinary logs.

Timings, limits, and bulk-deletion thresholds are provisional: tune and approve
them explicitly, not by weakening safety checks during incident recovery.
