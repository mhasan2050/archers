# Project instructions

This repository currently contains design contracts, not a working application.
Do not claim commands, services, tests, or migrations exist before verifying them.

## Read before changing

Start with the [context index](../docs/README.md). Read the relevant authoritative
contract before changing its implementation:

- [Architecture and framework boundaries](../docs/architecture.md)
- [Data model and migration rules](../docs/data-model.md)
- [Authentication, RBAC, and privacy](../docs/security.md)
- [OpenAPI contract](../docs/api-contracts.yaml)
- [NAS ingestion and publication](../docs/ingestion.md)
- [Configurable connectors, formats, citations](../docs/connectors-formats.md)
- [Model export, spending, performance](../docs/models-cost-performance.md)
- [Compose and Kubernetes packaging](../docs/deployment-packaging.md)
- [Testing and acceptance](../docs/testing.md)
- [Current architecture decision](../docs/decisions/0002-configurable-production-product.md)

## Change discipline

- Implement the requested behavior incrementally within existing modules.
  Search for and reuse existing logic before adding a helper or dependency.
- Do not rewrite the application, replace frameworks, move service boundaries,
  broaden document visibility, or break public contracts without explicit approval.
  Propose a focused decision record when an architectural change is necessary.
- Use versioned migrations for schema changes. Never drop/recreate populated
  databases, delete persistent volumes, or reset user data to make a change pass.
- Preserve compatible APIs, persisted data, source identities, and ingestion
  idempotency. Use expand/migrate/contract for necessary breaking changes.
- Keep tenant, authentication, role, ownership, and source-lifecycle checks in
  shared server-side domain logic. The UI and LLM are not security boundaries.
- Default to local OSS models. Enable external generation/embeddings only under
  explicit deployment/source/requester approval, endpoint controls and budgets.
  Airgap denies external calls; connected sources do not grant model export.
  Keep reranking local, no automatic paid fallback, runtime downloads or hosted traces.
- Surface dependency/validation failures explicitly. An outage is not an
  evidence-based abstention; a NAS outage is not a source deletion.
- Keep company PDFs, questions, answers, datasets, credentials, and model weights
  out of source control. Use synthetic fixtures; never log sensitive bodies.
- Update the one authoritative contract for an intentional behavior change,
  then refresh affected overview documents/diagrams. Do not copy rules into
  another competing specification.
- Add regression tests and run the smallest existing checks covering the change.
  State what ran and what could not be verified. No success-shaped fallback.

## Implementation conventions

Use typed Python/Pydantic boundaries and TypeScript UI contracts; follow the
established formatter, package manager, and patterns once bootstrapped. Pin
dependencies/images and model revisions at implementation time. Do not treat
provisional model names, thresholds, or hardware estimates as approved constants.
There are no verified build/test commands yet; add them to the testing contract
when the actual toolchain exists.
