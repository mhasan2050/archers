# Authentication, RBAC, and privacy contract

Status: required behavior; implementation and security verification are pending.

## Access model

Local administrator-provisioned accounts only, no anonymous corpus access or
self-registration. One role per user: `reader`, `curator`, `admin`.
All enabled authenticated users can read every active document in their tenant.
Remote source ACLs are not mirrored. The company must approve each source as a tenant-wide
corpus before ingestion; role checks cannot make a sensitive folder safe to import.

| Permission | reader | curator | admin |
|---|---|---|---|
| `query`, `documents.read` | Yes | Yes | Yes |
| `conversations.own`, `feedback.own` | Yes | Yes | Yes |
| `ingestion.manage` (manual upload, review, retry, cancel, job status) | No | Yes | Yes |
| `sources.observe`, `sources.rescan` | No | Yes | Yes |
| `users.manage`, `sources.manage`, `documents.withdraw`, `model_policy.observe`, `export.manage` | No | No | Yes |

Role inheritance is explicit; unknown roles/actions deny. Domain entry points
enforce permissions as well as HTTP dependencies. UI hiding is convenience only.
Every resource operation checks tenant membership. Conversations, run events,
messages, and feedback require ownership even for admins. Document reading never
grants permission to read another user's chat.

Watcher is a scoped noninteractive source identity, not a human admin. Worker and
watcher credentials are least-privilege, internal-only; neither can log in or
modify user roles. Every service mutation remains fenced and audited.

## Sessions and account lifecycle

- Argon2id password hashing through maintained libraries; never reversible
  password encryption, plaintext storage, or passwords in logs/audit.
- Initial provisioning/reset password minimum is 15 characters (maximum 1024);
  validate consistently across API and bootstrap without truncation. Revisit
  password policy explicitly when corporate identity integration is approved.
- Opaque cryptographically random session tokens; store only their hashes.
  Absolute expiry is initially eight hours. No long-lived role claims in cookies.
- Session cookie name `rag_session`; HttpOnly, SameSite=Lax, scoped path.
  Secure under TLS; explicit loopback development exception only.
- Validate trusted Host/Origin and JSON request content types. Login checks Origin
  too. SameSite is not sufficient CSRF protection.
- Login returns a session-bound CSRF token; require `X-CSRF-Token` on every
  authenticated unsafe request (including logout and multipart upload).
- GET session metadata can return the token for a legitimate same-origin page;
  no wildcard CORS and never tokens in URLs.
- Initially throttle five failed attempts/account/15 min plus an IP limit.
  Use generic credential errors and finite cooldowns, not account enumeration.
- Role change, password reset, and disable revoke sessions, increment authorization
  revision, and invalidate pending runs. Query finalization rechecks status/role.
- Bootstrap first admin explicitly with protected secret input, no defaults.
  Serialize updates protecting the last enabled admin from demotion/disable.

## Resource and inference safeguards

All protected routes require authentication; only login and minimal liveness are
anonymous. Internal readiness/metrics use a deployment-managed operator credential
on the private network, not an end-user cookie. Do not publish operational ports
or expose diagnostics through the web proxy.

Server builds mandatory tenant filters for both retrieval branches. PostgreSQL
validates active generation, lifecycle, tenant, and current user before chunk
loading/reranking/generation and final delivery. A source outage does not revoke
last committed evidence; confirmed deletion/suppression does.
Changed authorization/content discards the draft and allows at most one bounded
restart, otherwise `AUTHORIZATION_CHANGED`/409. Already delivered text cannot be
recalled; saved history and source links are reauthorized.

All imported content is untrusted data. Do not follow its prompts, execute embedded
scripts, browse its URLs, or let it grant permissions/tool access. Strict model
output parsing, source-ID/provenance checks, local claim checks, and human-reviewed
evaluation are required. No correctness guarantee is inferred from LLM verification.

Sandbox parser/OCR subprocesses with resource/time/archive-expansion limits, no network/root/Docker
socket. Validate uploads and connector snapshots by actual bytes and parser behavior.
Reject path traversal/symlinks and encrypted/corrupt PDFs explicitly.
Serve PDFs with correct content type, nosniff, and a restrictive locally bundled
viewer/CSP; sanitize Markdown/HTML rendering. Do not trust model-generated URLs.
Use sanitized normalized views for other formats; originals download as attachments
with validated MIME. Never execute macros/formulas/HTML scripts or fetch embedded
resources. [Format location rules](./connectors-formats.md) prevent invented pages.

## Local deployment and secrets

Only loopback web exposure by default. LAN requires approval, trusted TLS, firewall
rules, secure cookies, and testing. Never expose the stack directly to the internet.
Use internal authenticated database/vector/inference connections and service
credentials. Protect secrets in local files/host credential storage excluded from
source control; no sample working passwords or keys.

Host/CSI owns SMB/NFS credentials/mounts/health; only watcher gets the approved
read-only mount. Cloud connectors use least-privilege OAuth/object-store secret
references and approved endpoints/scopes. Failed permission checks freeze
reconciliation and raise review; do not infer deletion or remote ACL equivalence.

Models default local OSS. Airgap denies external sources and paid inference.
Connected-source mode permits approved connector downloads/auth only, not model
export. External-model mode separately permits approved generation/embedding
calls through [export/consent/budget gates](./models-cost-performance.md).
Reranking remains local; no automatic vendor/local-to-paid fallback, hosted traces,
runtime downloads, CDNs, or arbitrary exporters. Operators stage verified assets.
Protect destination allowlists, redirects/DNS, private metadata addresses, and
secret refs against SSRF. Private enterprise endpoints require explicit reviewed
allowlist entries, not blanket private-network permission.

External export flags are admin-only per source/manual document and default false.
They cannot override deployment deny. Requester consent is required for remote
questions/history; provider keys never come from chat requests. Revocation stops
queued/new remote work and invalidates in-flight results as far as possible; data
already sent cannot be recalled. Audit policy/profile/export changes without text.
Separate disabled/unknown provider failure from abstention and never bypass spend
limits or switch encoders as a fallback.

Company PDFs, filenames/paths, prompts, answers, evaluation datasets, checkpoints,
and model caches may be sensitive. Store them only in operator-approved private
volumes/object storage; remote model export follows the explicit policy above.
Default logs contain correlation IDs, timings, stage, sanitized codes, and
revision identifiers, not content. Audit account/role/source changes, publication,
withdrawal, and document access without bodies or passwords.

Encrypt host disks and backups. Test deletion cleanup and backup expiry; do not
promise erasure from SSDs/historical backups. Restores invalidate sessions and
reapply post-backup deletion/suppression policy before opening access.
See [testing](./testing.md) for executable enforcement requirements.
