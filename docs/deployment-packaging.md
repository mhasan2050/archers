# Production deployment and packaging contract

Status: release specification, not executable packaging. One company per install;
not shared multi-company SaaS. See [security](./security.md) and
[model policy](./models-cost-performance.md).

## Release artifact

Ship versioned OCI images for web, backend (API/watcher/worker/admin commands),
inference, and compatible pinned infrastructure references. The backend image
also supplies the optional internal `model-gateway` command for external-model
mode; only that process has paid-model egress. Release includes:

- Compose bundle for a single host; Helm chart for Kubernetes using the same
  images/application contracts, not separate implementations.
- Versioned configuration schema/defaults, no working secrets/default admin,
  per-profile examples (local CPU, local GPU, connected sources, opt-in paid).
- Migrations, compatibility matrix, release notes, upgrade/restore instructions.
- SBOM, license/redistribution notices, image signatures/checksums, vulnerability
  remediation record, and tested install/upgrade evidence.
- Operator runbooks: bootstrap, secret rotation, source health, billing limits,
  backup, isolated restore, deletion, incident handling, and diagnostics redaction.

Distribution license, organization/product naming, supported platform versions,
and exact model bundle licenses still require decisions. No claim of redistributable
model weights without review. Offer verified offline model/image import packages;
optional setup-time downloads happen before content ingestion with consent.
Never runtime auto-download. Defaults use local OSS generation and embeddings,
cloud traces off, auth on, no publicly exposed database/model services.

## Configuration ownership

Deployment-owned: secrets, endpoint allowlists, model profiles, export master
switches, budgets/prices, storage, TLS, network mode, platform resources. Admin
API-owned: allowed connector scope, format subset, source enabled state, scan policy,
source/document export flags under the deployment's upper bounds.

Configuration is versioned and strict: reject unknown keys, unsupported provider
capabilities, bad aliases, missing secrets/models, incompatible index encoders,
unapproved endpoints, and external profiles without budgets/approval. Supply
redacted validation errors. Public API never accepts raw keys or arbitrary model
URLs. Model profiles are immutable revisions; activation and migrations are explicit.

Three network modes:

- `airgap`: local sources/models only; no external connector or paid endpoint.
- `connected_sources`: only approved connector endpoints/OAuth; models stay local.
- `external_models`: connector endpoints as configured plus individually approved
  generation/embedding egress. Reranking stays local; tracing still off.

Cloud source connections need outbound auth/content downloads even with local
models. Connecting an external source does not approve sending its content to
an LLM provider. Scope DNS/HTTP redirect resolution, proxies, and certificates to
approved destinations, with SSRF/metadata protection and audited exceptions for
private S3 endpoints. Deny arbitrary fetching from retrieved HTML.

## Compose

Single-host persistent volumes; default loopback web, production LAN only with TLS/
firewall. Separate private stores/models from egress-enabled connector and provider
routes using an explicit egress proxy/firewall, not just environment variables.
Containers need least privilege, resource limits, health checks, log rotation,
and no Docker socket. Mounted filesystem source is read-only and existing;
host retains SMB/NFS credentials/health checks.

Compose is a usable small production deployment only within a measured envelope;
it is not HA. Shipping images is not evidence of backup/upgrade reliability.
Local volume storage is adequate on one host.

## Kubernetes

Helm values expose replicas/resources, CPU/GPU requests, PVCs/storage classes,
ingress/TLS, secrets references, network policies, model cache, queue limits, and
service endpoints. Default single replicas until correctness/scaling tests pass.
Run schema migration as an explicit gated Job before compatible rollout.

- API/web/connector/provider services: Deployments; workers scale only within
  global job/model limits. Per-source leases and global cost/concurrency claims
  prevent duplicate scans and unbounded billing across replicas.
- PostgreSQL/Qdrant: PVC-backed single-instance defaults; allow compatible
  operator-managed services. HA requires separately validated topology/backups.
- Artifacts: shared RWX PVC or approved S3-compatible artifact backend for multi-
  replica operation. Never have replicas reading different node-local snapshots.
  Immutable object finalization/manifest checks replace filesystem rename semantics.
- Local GPU serving: node constraints, model preload, startup probes, and bounded
  concurrency; replicas consume additional model memory, not free capacity.
- Mounted NAS: prefer administrator-provisioned read-only SMB/NFS CSI volume and
  appropriate health/identity probes, not arbitrary `hostPath`. Credentials stay
  with operators/CSI. Connector semantics remain identical to Compose.
- Deny-by-default NetworkPolicies require a supporting CNI. Domain allowlists
  need an egress gateway/proxy or verified CNI policy, not vanilla policies alone.
- Pod security, anti-affinity/disruption budgets when scaling, session/event
  state in PostgreSQL, and sticky-free SSE reconnect from durable run IDs.

Externalized stores/HA/multiple replicas must be certified combinations. Kubernetes
alone does not supply HA or prove throughput/cost savings.

## Install, upgrades, backup and removal

Preflight validates hardware/disk/platform, secrets, models, connector permissions,
export policy, budget table, TLS, storage and schema compatibility. Initialize
admin explicitly. Admit traffic only after readiness and contract smoke tests.
Installation must not require editing application source code.

Release semantic versions with image/chart/config/API/schema compatibility ranges.
Test N-1 -> N with populated data. Use expand/backfill/contract, drain incompatible
jobs/runs, backup before migrations, record progress, and fence old workers.
Do not equate Helm rollback with database downgrade. Restore verified backups or
use a tested reversible migration; never discard customer volumes.

Quiesce writes/watcher/publication or use tested consistent snapshot mechanisms;
back up DB, artifact manifests/objects, index snapshots, model/config fingerprints
and required secrets under encryption. Test isolated restore, sessions invalidation,
post-backup deletion/suppression reapplication, RPO/RTO, and reindex.

Uninstall stops services and revokes connector/model credentials. Data retention/
erasure is a separate explicit operator action. Never run destructive volume
deletion as the default uninstall or recovery path.
