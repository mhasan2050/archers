# ADR 0002: Configurable self-hosted production product

Date: 2026-11-15

Status: accepted design scope; no integrations, packaging, or benchmarks implemented.

## Context and approved scope

The local PDF pilot is being packaged for other companies. The user confirmed:
one company per install; Compose plus Kubernetes; filesystem/SMB/NFS, S3,
SharePoint/OneDrive, Google Drive, manual uploads; PDF/DOCX/PPTX/XLSX/
Markdown/text/HTML/CSV; local OSS default with OpenAI/Azure/Anthropic/Gemini/
compatible endpoints; independent external generation/embedding opt-in;
local reranking; benchmark profiles and configurable spending caps.

## Decision

Keep the domain, job, publication, RBAC, and query workflow; add reviewed typed
connector/parser/provider adapters and versioned deployment configuration.
Do not rebuild the pipeline per customer, source, file format, or model vendor.

Default to local OSS inference; distinguish source connectivity from model
export. Paid capabilities require deployment and per-source/document approval,
requester consent, secrets references, endpoint controls, and atomic budget
reservations. No automatic local-to-paid fallback.

Preserve original bytes and format-specific citation locations, never invent PDF
pages for non-PDF files. Ship Compose and Helm using identical application images
with storage, migration, health, security, license, backup, and upgrade gates.
Performance/cost claims require published measured quality-preserving benchmarks.

## Supersession and compatibility

Supersedes ADR 0001's PDF-only, host-SMB-only, local-inference-only network
assumptions and single-host packaging scope. Keeps local default, frameworks,
RBAC/ownership, leased jobs, staged publication, and cloud tracing disabled.
No running application or customer migration exists yet; API draft changes below
stay `/api/v1` but the design contract version advances to 0.2.0.

Retain old PDF download route for draft compatibility, add generalized original
file/normalized-view routes and typed location citations. Mounted-source API
requests transition to discriminated connector configuration; do not keep two
authoritative config shapes. Before a deployed v1 API, freeze contracts and enforce
compatibility on later changes.

## Consequences and remaining decisions

Cloud connectors and paid inference are deliberate egress exceptions, not an
airgap deployment. Mixed export policies, token/pricing differences, remote
deletion/permission errors, storage topology, upgrades and billing reconciliation
add implementation/testing work. Generic capability interfaces do not imply every
provider supports embeddings or every parser preserves all visual information.

Local-account RBAC and all-authenticated document visibility remain the selected
policy; corporate SSO and remote ACL mirroring are not silently introduced.
Licenses, exact platforms/models, budget values, connector consent, hardware, and
SLA remain customer/operator decisions. See [context index](../README.md).
