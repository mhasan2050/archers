# Archers administration UI — Stage 1

React/TypeScript/Vite, locally bundled with no CDN assets. No query, document,
chat, or ingestion interface is implemented. The authenticated landing says so
explicitly. This directory does not implement backend authorization.

## Local checks and development

Use Node 24.11.1 or a compatible supported Node release (Vite requires
Node 20.19+ or 22.12+), and npm:

```sh
cd web
npm ci
npm test
npm run build
npm run dev
```

The build runs TypeScript checking before producing `dist/`. Tests use synthetic
fixtures and mocked HTTP/backend boundaries; they cover session lifecycle,
deny-by-default role/permission visibility, CSRF, quoted revisions, idempotency
UUIDs, source defaults/policies, pagination encoding, errors, and admin forms.
The standalone eval plan additionally checks the built preview responds on
loopback port 4178. Its HTTP probe is a fallback, not browser E2E evidence.
Dependencies are already installed when replaying that plan; its setup is empty
to avoid unnecessary dependency installation.

Development runs on loopback and proxies `/api/v1` to `127.0.0.1:8000`, preserving
Host/Origin for trusted-origin validation. The backend must explicitly approve
the local development origin and loopback cookie exception. Vite development
and preview servers are not production deployments. The preview server serves
assets only, without an API proxy.

## API behavior

- Cookie sessions remain browser-managed: same-origin credentials, no bearer
  token or local/session storage. CSRF and session metadata exist in memory only.
- Session metadata comes from login and `/auth/me`; focus and expiry update the
  UI. Enabled `admin` plus the specific server-granted permission is required
  for admin controls; unknown roles deny. The server remains the authority.
- User creation requires a 15–1024-character initial password. Editing supports
  role, disable, and optional password reset, then rechecks the current session.
- Each explicit creation sends a fresh UUID `Idempotency-Key`. Updates send
  quoted `If-Match` revisions; authenticated mutations include `X-CSRF-Token`.
  No requests are automatically retried. After an uncertain creation outcome,
  refresh and inspect the list before resubmitting, to avoid duplicate creation.
- Password fields clear on submit. No application logs contain passwords or
  content; API errors show safe codes and local explanations, never raw bodies.
  Conflicts and server/dependency failures are explicit. Refresh clears an edit
  so another save starts from a newly fetched revision.
- Lists support opaque cursor pagination. New sources are approved filesystem
  aliases/sentinel UUIDs (including host-mounted SMB/NFS), allowlisted formats,
  enabled status and provisional scan policy. Both export flags always start
  false. Other connector DTOs can be listed and their policy edited, but remote
  connector creation/credential provisioning is not offered in Stage 1.
- Connector scope is immutable when editing. Existing source export flags can
  be edited only with `export.manage`; absence of that permission preserves them.
  Company approval and deployment gates remain backend/operator responsibilities.

## Container

```sh
docker build -t archers-web ./web
```

The Dockerfile uses the npm lockfile in a multi-stage build and serves only
compiled assets through unprivileged Nginx (UID 101, port 8080). Attach it to the
private API network with `api:8000` resolvable. Publish only `127.0.0.1:8080:8080`
by default. Trusted TLS/LAN approval and secure-cookie policy are deployment
responsibilities; no public-internet deployment is supported.

Nginx preserves API paths and Host, blocks readiness and metrics even with
trailing paths/query strings, disables access logs, sends a restrictive local
CSP, and disables proxy buffering/caching with long timeouts for future SSE.
The image expects its writable `/var/cache/nginx` directory; a read-only
filesystem deployment must mount it separately. Container tags are explicit
versions, not digest-pinned certification; operators must verify/pin registry
digests before release.

Verified in this implementation: `npm test` (36 tests), `npm run build`, and all
three standalone eval assertions (build, tests, preview HTTP 200). Docker runtime
validation was blocked because the Docker daemon socket was unavailable. A
registry tag inspection was also blocked by Docker Hub DNS resolution; image
digests and tags therefore still require operator verification.

Backend-integrated browser flows, Nginx runtime, trusted TLS, and deployment
certification require separate validation.
