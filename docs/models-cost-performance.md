# Models, data export, cost, and performance

Status: product contract and benchmark plan; no optimization has been measured.

## Local-first capability routing

Default generation/verification uses a locally served, appropriately licensed
OSS model. Default dense embeddings, sparse encoding, and reranking are local.
Select a genuinely redistributable model with license review, pinned weights,
quantization, tokenizer, dimensions, context limits, and hardware benchmark.
Ollama is a serving runtime, not proof that every available model is open source.
No particular model/price/version is frozen before compatibility evaluation.

Generation and dense embeddings may each be explicitly configured as external.
Sparse encoding, reranking, and default claim checks stay local. First-release
provider adapter scope: OpenAI, Azure OpenAI, Anthropic, Google Gemini, and approved
OpenAI-compatible endpoints. Capabilities are provider/model-specific:

- Anthropic is generation-only unless a separately approved actual embedding
  integration is added; do not invent an Anthropic embeddings API.
- OpenAI/Azure/Gemini embedding support requires a model/endpoint that actually
  supports it. Compatible endpoints must pass capability tests, not rely on name.
- Structured output, usage reporting, tokenizers, batching, contexts, rate limits,
  regional endpoints, retention, and prompt caching vary. Record supported
  capabilities and refuse unsupported configurations before use.

The application/provider adapter remains responsible for auth, export permission,
cost reservation, deadlines, schema validation, and cancellation. LangChain
integrations do not bypass these gates. Select one profile per capability, not a
new agent/tool for each vendor.
In external-model mode the backend-image internal model gateway is the only
model egress route. Authenticate service callers, bind calls to stored run/job
IDs and current evidence/source references, and reread approvals/budgets there.
Do not trust a forwarded `approved=true` flag. Private local adapters have no
paid-model egress; unapproved calls fail even if callers know a profile ID.

## Deployment policy and profile lifecycle

Use deployment-versioned profiles, not client-selected URLs/API keys/models.
Each profile records ID/revision, capability, provider/model, approved endpoint/
region, secret reference, request/output/batch limits, and price-catalog revision.
The runtime account cannot enable network/export permission. Operators provision
secrets and endpoint allowlists separately; admin source export choices cannot
override a deployment deny.

External generation and external embeddings default independently to disabled.
Enabling one requires deployment approval and relevant source approvals. Each
source/uploaded document has separate `allow_external_generation` and
`allow_external_embeddings` flags, both false on creation. An administrator can
approve export, audited with revision checks; curators cannot grant export rights.

Before an external call:

1. Authenticate and check capability policy/profile health and current revision.
2. Verify export permission for every text-bearing source/document. Query text and
   conversation history need requester consent too. Reject unapproved content.
3. Reserve worst-case cost and concurrency, enforce endpoint/secret/network policy.
4. Send only necessary bounded content; never attach whole files automatically.
5. Record usage/latency/policy revisions without logging prompt or document text.
6. Recheck content/authorization/export revisions before final answer delivery.

Query requests default to `external_processing_consent: false`. When an external
profile is configured, lack of consent returns `EXTERNAL_PROCESSING_NOT_APPROVED`,
not a surprise cloud call. Consent and deployment policy cover query rewrite and
any remote verifier as well as generation. Keep rewrite/checks local by default.
For private chats, prior consent cannot be inferred from another user or install.

Mixed approved/unapproved evidence is not silently filtered to make a paid call
work: deny external generation and expose an actionable policy error, or use a
separately requested approved local profile. Never automatically fall back from
local to paid, paid to another vendor, or external embeddings to a different
encoder. Retries stay within the same approved capability/profile and deadline.

For external dense embeddings, ingestion sends chunk text and query retrieval
sends questions; both need export consent/policy. If a source refuses external
embeddings, choose a separately managed local index or reject its processing
configuration visibly. Do not mix incompatible dimensions/encoders in a collection.
Initially use one active embedding profile per installation's corpus; switching
requires full staged reindex and atomic corpus routing promotion after verification.
Old queries keep their pinned profile/index or restart, never encode against a
different version mid-run. Revoking export stops queued/new remote calls; previously
sent data cannot be recalled. Local serving of already computed embeddings can
continue under a reviewed policy while reindexing locally.

## Budget accounting

External mode cannot activate without approved pricing metadata and finite caps:
monthly installation allowance, per-query ceiling, and per-ingestion-job ceiling.
Use configured currency (initially USD), integer micro-units, UTC budget windows,
and versioned operator-maintained price tables. Never hard-code "current" prices.
Represent cached tokens, input/output tokens, request fees, and embedding units
as supported by each provider. Unknown price -> visible block, not zero cost.

Atomically reserve conservative worst-case usage in PostgreSQL before each
remote request, including revisions, retries, query rewrites, verifiers, and
embedding batches. Parallel API/worker replicas share reservations, never enforce
budgets with process-local counters alone. Include pending reservations in limits.
Reconcile actual reported usage on completion. Uncertain failures keep a safe
reservation pending reconciliation; do not automatically replay a potentially
billed request. Release only proven unused amounts; budget overflow returns
`BUDGET_EXCEEDED`/429, never swaps model or truncates essential evidence silently.

Dollar estimates are controls, not a billing guarantee: providers may change
prices or report usage late. Require provider-side spend limits where available,
reconcile bills, and expose estimated/confirmed/unknown charges separately.
Do not embed billing credentials in support bundles or response bodies.

## Performance work that preserves correctness

- Incremental connector cursors and conditional snapshots avoid repeated download.
- Hash/deduplicate after observation before parsing; reuse extraction only for
  identical content/parser policy within authorized scope.
- Cache dense document embeddings by content + exact encoder/policy fingerprint,
  without leaking cross-tenant hash existence. Recheck export approval on reuse.
- Cache query encodings with tenant/profile/policy isolation and short TTL. Answer
  caching is initially off; if introduced, key by owner/history/active corpus/
  auth/export/model/prompt revisions and reauthorize before delivery.
- Batch embedding calls under token, payload, model, and memory limits; cap worker
  concurrency. Shared warm models avoid per-worker copies.
- Prioritize online retrieval/model work over ingestion with bounded queues and
  backpressure. Use true DB/distributed concurrency limits for replicated workloads.
- Conditional OCR, tokenizer-aware structural chunks, hybrid retrieval and local
  reranking reduce unnecessary inference and context tokens.
- Quantization, vector on-disk/quantized indexes, candidate caps, and smaller models
  require quality/memory/latency comparisons. Do not trade citation/access checks
  away for speed.
- No extra model-based reranker, classifier, or multi-agent loop without measured
  benefit. Tune total deadline, output budget, and verification passes explicitly.

## Reproducible benchmark profiles

These are provisional workloads, not hardware/SLA claims:

| Profile | Corpus | Active users | Purpose |
|---|---|---|---|
| Small | 1k documents, ~10k normalized blocks | 1 | CPU-only/local baseline; cold/warm latency |
| Medium | 10k documents, ~100k blocks | 5 | Concurrent chat plus incremental ingest |
| Large | 100k documents, ~1m blocks | 20 | K8s resource/backpressure/storage scaling |

Record actual bytes, pages/slides/cells, chunks, languages, OCR share, host/GPU,
RAM/VRAM, model/context/quantization, prices, index settings, replicas, and storage.
Use reproducible question sets and equal quality gates for local/paid comparisons.
Report throughput, p50/p95/end-to-end latency, queue time, peak resource use,
freshness lag, recall, answer/citation support, and cost per 1k ingested chunks/
100 queries, plus amortized local electricity/hardware/operations where known.

Release requires a published measured envelope per certified profile, not a
generic "optimized for production" label. Hardware and spending targets remain
deployment choices until benchmarks and customer requirements exist.
