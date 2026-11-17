# Project context and contract index

Status: product design contracts with staged implementation, 2026-11-15.

Implementation status (current workspace):
- Stage-one foundation code exists under [backend/](../backend/) and
  [web/](../web/): strict local auth/RBAC, admin users/sources API, safe offline
  format normalization, and local packaging files.
- Stage-two ingestion acceptance adds manual uploads/replacements, immutable
  snapshots, durable PostgreSQL jobs and fenced lease/cancel/retry primitives.
  See [runtime instructions](../README.md).
- Automatic filesystem polling, persistent stability observations, isolated
  parsing, local model-aware chunks/embeddings and verified Qdrant publication
  are implemented with migration 0003. Manual uploads use the same worker.
  NAS requires independent host-health evidence.
- Query orchestration/answers, OCR/review resolution, automatic withdrawals,
  vector/orphan cleanup and paid-provider gateway workflows remain unfinished.
- A portable Helm chart and Terraform entry points for existing EKS/AKS clusters
  package the current implementation with external stores/PVCs, private ingress,
  explicit migration gates and optional bounded HPA. See the
  [cloud deployment guide](../deploy/README.md). Static checks are not cloud,
  scaling, HA or Linux sandbox certification.

## Purpose and authority

These are workspace-specific, team-shared context files for incremental
implementation and maintenance. They describe intended behavior, not proof that
the system implements it. Copilot guidance is in
[project instructions](../.github/copilot-instructions.md).

| Topic | Authoritative contract | Consult when changing |
|---|---|---|
| Frameworks, components, network boundaries | [Architecture](./architecture.md) | Dependencies, modules, Docker services |
| Entities, constraints, index generations | [Data model](./data-model.md) | Schema, persistence, migrations |
| Login, roles, ownership, data visibility | [Security](./security.md) | Any entry point or sensitive-data handling |
| HTTP DTOs, paths, errors, SSE | [OpenAPI](./api-contracts.yaml) | API/backend/frontend contracts |
| NAS, ingestion, deletion, recovery | [Ingestion](./ingestion.md) | Watcher, worker, lifecycle changes |
| Connector/parser extensions and citations | [Sources and formats](./connectors-formats.md) | Source configuration, parsing, normalized provenance |
| Local/paid inference, export, budgets, benchmarks | [Models and efficiency](./models-cost-performance.md) | Provider profiles, costs, performance changes |
| Compose/Helm releases, install/upgrade/restore | [Packaging](./deployment-packaging.md) | Production distribution and operations |
| Tests, evaluations, release evidence | [Testing](./testing.md) | Every behavioral change |
| Rationale and architectural change history | [Decision 0002](./decisions/0002-configurable-production-product.md) | Significant deviations; partially supersedes decision 0001 |

[HLD](../HLD.md), [LLD](../LLD.md), [HLD diagram](../HLD.drawio), and
[LLD diagrams](../LLD.drawio) are explanatory design views. Their duplicated tables
are snapshots, not separate sources of truth. If a mismatch is discovered, stop
the affected implementation, identify the conflict, and align the views with
these contracts; never silently resolve it by weakening a security invariant.
The security contract owns access decisions, the data model owns persisted
constraints, OpenAPI owns wire shapes, and ingestion owns source lifecycle.

## Decision status

**Confirmed requirements:** one company per self-hosted installation, Compose and
Kubernetes; local OSS inference by default, generation/embeddings independently
opt-in to approved paid providers; local reranking; LangChain/LangGraph; Qdrant;
configurable filesystem/SMB/NFS/S3/Graph/Drive/upload connectors and
PDF/DOCX/PPTX/XLSX/Markdown/text/HTML/CSV formats; safe lifecycle reconciliation;
local accounts/RBAC; all authenticated users read the active tenant corpus.

**Adopted design baseline, not implemented:** FastAPI, Qdrant, PostgreSQL, Docling,
Ollama, React/TypeScript, PostgreSQL leased jobs, shared local artifacts, dedicated
watcher and inference containers. SQLAlchemy/Alembic and the Python/TypeScript
tooling in the architecture contract are planned implementation conventions.

**Provisional:** hardware, product/distribution license, platform certification,
model weights/revisions, provider prices/spending ceilings, languages, numerical limits,
retention, quality thresholds, source completion protocol, and scan/deletion
timings. Defaults are for experiments, not measured capacity or approved policy.

**Not included:** active hosted LangSmith, Enterprise LangSmith deployment, SSO,
per-document group ACLs, remote-ACL mirroring, shared SaaS, automatic HA, public-internet deployment, and
arbitrary agent tools. LangSmith remains an optional licensed integration only
after privacy/deployment approval; do not silently substitute hosted tracing.

## Change protocol

1. Identify affected contracts and existing code; make a bounded change proposal.
2. Obtain approval for breaking contracts, security-policy changes, framework
   replacement, or service-boundary changes. Record significant choices in an
   append-only numbered decision file; supersede rather than erase earlier ones.
3. Evolve the contract, implementation, migration, and regression tests together.
4. Preserve data and compatibility; explicitly document a transition if required.
5. Update affected HLD/LLD diagrams after the authoritative contract changes.
6. Record actual verification and limitations. Documentation alone is not enforcement.

Frontend dependency lockfile, backend runtime configuration and two additive
database migrations now exist. Backend dependency locking and CI remain pending.
Do not
invent commands or label unexecuted tests as passing.

## Decisions required before a real pilot

- Host OS, CPU/RAM/GPU, Docker resources, corpus/page count, and languages.
- Approved model licenses, exact weights, checksums, and performance.
- NAS mount alias, sentinel, host health probe, and producer completion protocol.
- Retention, backup destination, restore objectives, scan/deletion thresholds.
- Whether LAN access is needed; trusted TLS and firewall policy if enabled.
- Whether Enterprise self-hosted LangSmith is funded and operationally permitted.
- Deployment network mode, approved connectors/OAuth scopes and format coverage.
- Model export approvals, paid profiles, prices and finite budgets; product/model
  redistribution licenses and Compose/K8s certification matrix.
