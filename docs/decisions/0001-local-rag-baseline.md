# ADR 0001: Local RAG baseline and incremental evolution

Date: 2026-11-14

Superseded in part by [ADR 0002](./0002-configurable-production-product.md):
multi-format/source packaging and opt-in paid inference replace the pilot-only
scope. Original rationale below is historical; retained invariants are listed there.

Status: accepted design baseline; implementation pending. Numerical defaults,
weights, hardware, and retention are provisional.

## Context

Company PDFs arrive automatically on a host-mounted SMB NAS share. Users need
authenticated, cited document answers. Company content must not leave the local
environment. All enabled authenticated users read the same active tenant corpus;
roles restrict actions, not per-document content.

## Decision

- Use a modular Python application: FastAPI/Pydantic, LangChain integrations,
  embedded LangGraph query workflow, Docling/OCR ingestion, PostgreSQL authority
  and durable leases, Qdrant vectors, local Ollama, shared local inference, and
  a React/TypeScript UI.
- Run separate API, watcher, worker, inference, and infrastructure processes in
  local Docker Compose. Only loopback web is published.
- Host manages SMB mount/credentials and live health; watcher gets read-only
  access. Poll/reconcile safely and ingest immutable local snapshots.
- Enforce local sessions and reader/curator/admin RBAC server-side, plus tenant
  and conversation ownership. No NAS ACL mirroring.
- Publish verified index generations through atomic PostgreSQL pointers.
- Keep LangSmith/cloud exporters disabled. Self-hosted LangSmith is a separately
  licensed/deployed option, not a free baseline container.
- Treat the [context index](../README.md) as the authority map. Evolve contracts,
  migrations, and tests incrementally, not by regenerating the application.

## Alternatives and consequences

SMB filesystem events alone are unreliable across remote mounts, so polling and
durable inventory are required. This adds bounded detection delay and host-probe
operations. Stability without producer cooperation cannot guarantee completion.

PostgreSQL jobs avoid a separate broker at pilot scale but require lease/fencing
tests. A dedicated queue may later be justified by measured scale, not preference.

pgvector could reduce services, but Qdrant is the selected retrieval baseline.
Changing stores is a migration decision, not a routine package swap.

Local inference preserves the selected privacy boundary but requires model/hardware
benchmarking and may be slow. Single-host Compose is not HA. Enterprise LangSmith
cost/deployment constraints remain explicit.

## Changes to this decision

Add a numbered ADR for significant changes and obtain approval before breaking
APIs, data compatibility, security policy, frameworks, or service boundaries.
Supersede this record rather than retroactively erasing rationale. Ordinary bug
fixes and compatible fields do not need a new ADR.
