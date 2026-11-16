# Source connectors and document formats

Status: first-release design scope, not implemented integrations. Owns connector,
parser, and citation extension contracts. [Ingestion](./ingestion.md) owns jobs and
publication; [security](./security.md) owns access and data export.

## Configuration and extension boundaries

One company per installation. Source configuration selects a shipped connector,
approved scope, allowed formats, scan policy, deletion behavior, and secret
reference. Never accept an executable plugin, import path, arbitrary URL crawler,
or parser command from a user/API. New adapters are reviewed, packaged, versioned,
and tested against the common contract. Disabled connectors have no credentials,
network access, or background polling.

First-release connector scope:

| Kind | Scope and identity | Change/completion/deletion evidence |
|---|---|---|
| `filesystem` | Local root or host-mounted SMB/NFS; source + canonical relative path | Stable snapshots; mount health; complete scan + final not-found |
| `s3` | Approved endpoint/region/bucket/prefix; source + object key | Version ID or conditional ETag read; complete paginated list and confirmed object absence |
| `sharepoint` | Approved directory tenant/site/drive/folder; stable remote item ID | Graph delta/change token + version/conditional download; tombstones reconciled under healthy permission scope |
| `google_drive` | Approved drive/folder; stable remote file ID | Change token + revision/conditional download; authoritative removal distinguished from permission loss |
| `upload` | Manual HTTP upload via existing endpoint | Explicit revision/idempotency; manual lifecycle, no remote deletion |

OneDrive uses the `sharepoint` Graph connector with a configured drive, not a
separate pipeline. Source identity/scope edits create a new source and reviewed
transition, never reinterpret existing IDs. Changing formats/filters is
configuration exclusion, not deletion. Rename may update labels for stable
remote IDs; filesystem rename remains new path + confirmed old-path absence.

Cloud-native Google Docs/Sheets/Slides are exported to a configured supported
format (DOCX/XLSX/PPTX) with export format/version recorded, or visibly marked
unsupported. Do not index redirect HTML/login screens as downloaded documents.
S3 ETags are not universally content hashes; hash the downloaded bytes independently.

## Connector interface

Adapters expose typed, versioned operations:

- `capabilities()`: identity, incremental cursor, version/conditional read,
  listing completeness, deletion evidence, export formats, and health semantics.
- `check_health()`: scope identity and credential/permission health, not just HTTP 200.
- `enumerate(cursor, bounded_page_size)`: items/tombstones, next cursor, completeness,
  scope revision, provider watermark, and categorized errors.
- `snapshot(item, expected_revision)`: stream bytes to local staging, size/type
  hints, remote revision, conditional-read result, hashes, and provenance.
- `confirm_absence(item, scope_revision)`: `absent`, `present`, or `unknown`;
  transport/auth/permission/filter errors are `unknown`.

Persist cursor advancement only after page observations and jobs commit. Expired
cursors trigger a healthy full resync, not corpus deletion. Webhooks/events are
authenticated wake-up hints; scans/change feeds remain authoritative. No public
webhook endpoints in v1; polling works in both deployment modes.

On OAuth expiry/401/403, pause the source and alert; do not infer deletion.
Credentials are deployment secret references. Provider OAuth consent/service
accounts are provisioned by operators outside the chat UI and scoped minimally.
Every source must be approved as visible to all authenticated installation users;
remote ACLs are not mirrored. Inaccessible/permission-changed items freeze and
raise review, not silently remain guaranteed-safe or treated as confirmed deletion.

## Parser interface and format allowlist

Parser selection uses an allowlisted format plus byte signature/encoding checks.
File extension/MIME is a hint, never authority. Adapters return normalized blocks,
location references, quality issues, parser version, and artifacts. Reuse Docling
where validated; use narrowly scoped format helpers where needed. Format support
requires fixtures and release certification, not just a parser library claim.

| Format key | Initial extraction | Authoritative location |
|---|---|---|
| `pdf` | Text/layout/tables and conditional local OCR | Physical PDF page; optional box/block |
| `docx` | Headings, paragraphs, tables | Stable section/block ID in immutable normalized snapshot |
| `pptx` | Slide text/tables/notes with notes policy recorded | Slide index + block/shape ID |
| `xlsx` | Sheet names, cell values, tables/headers | Sheet + explicit cell range |
| `markdown` | Headings/text without executing embedded HTML | Line range + normalized block |
| `text` | Explicit encoding, normalized text | Line range |
| `html` | Static extraction, no scripts/network/subresources | Normalized section/block; optional element ID |
| `csv` | Configured/detected dialect with validation; header rows | Row range + columns/header |

Never invent PDF pages for non-PDF content. Store original immutable bytes and
normalized blocks; source links go through authenticated file/viewer routes.
An optional offline rendered derivative can aid viewing but is not original
provenance; pin renderer/version and explicitly label it.

Spreadsheet formulas/macros are never executed; cached formula results may be
stale and must be marked. Do not silently claim numeric aggregation over partial
retrieved rows: exact analytical workloads need explicit structured-table support
and tests, not arbitrary LLM arithmetic. Images/charts/embedded objects that are
not interpreted generate visible coverage warnings. OCR is local and conditional
on extraction evidence, not run on every document. DOCX/PPTX/XLSX archive expansion
limits prevent zip bombs; legacy DOC/PPT/XLS and arbitrary archive uploads are out
of scope. HTML extraction must not turn into a network crawler.

## Publication and configuration updates

Allow formats per source and on manual uploads; unsupported/excluded files get
observable statuses and never disappear through silent catches.
Parsers normalize to a common block/citation model, then use the same chunk,
embedding, generation, and canonical-access checks. Per-source export approval
applies before any external model operation on its content.

Parser, chunker, or embedding changes increment pipeline fingerprints and schedule
controlled reprocessing. Existing active generations serve until replacements
verify. Disabling a format/source pauses new processing; explicit withdrawal is
separate and audited. Remote connector bugs cannot bypass deletion safety gates.
