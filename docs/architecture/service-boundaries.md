# Service Boundaries

Status: Phase 0 baseline.

## 1. Rules

1. **One owner per service.** The owning group is the only one that changes that service's
   schema, topics, and config.
2. **No cross-service database access.** A service reads only schemas it owns. Two explicit
   exceptions: `core` (shared contract, §2) and `reconciler` (read-mostly convergence checks,
   §3.10). Neither exception permits ad-hoc writes to another service's tables.
3. **No synchronous call chain longer than one hop.** `api → service` is allowed for reads
   and command submission; `service → service → service` is not. Work between planes moves
   through Redpanda or Temporal.
4. **Contracts, not code, are shared.** Go types and Python models are generated or written
   from `docs/data/*.md`. CI runs contract tests on both sides.
5. **Every service is stateless** except for its documented caches and Temporal/Redpanda
   client state. All durable state is in Postgres, object storage, or Temporal.
6. **Every service emits OpenTelemetry traces and RED metrics**, and stamps
   `processor_version` on everything it writes.

### 1.1 Ownership groups

| Group | Services | Accountable for |
|---|---|---|
| Ingestion | `scheduler`, `fetcher`, `parser`, `indexer` | Crawl policy, fetch reliability, extraction correctness, index freshness |
| Intelligence | `enricher`, `graph-builder`, `llm-gateway` | Enrichment quality, entity/claim integrity, LLM cost and provider posture |
| Research | `api`, `research-orchestrator` | Public contract, investigation correctness, citation integrity, latency |
| Platform | `reconciler`, infrastructure, CI | Convergence, storage, broker/DB operations, deploys, observability |

A service has exactly one group. Shared contracts (`core`) are co-owned by Ingestion and
Intelligence, with Platform as tie-breaker; changes require both reviewers.

## 2. The `core` schema exception

`core.documents`, `core.document_events`, and `core.document_text` are a **shared contract
schema**, not a service-owned schema. Every ingestion-stage service writes them; no service
may write another service's tables.

Guardrails:

- All writes go through the `pipeline` library (Go: `internal/pipeline`, Python:
  `wre_pipeline`). Ad-hoc SQL against `core` fails code review and is blocked by the DB role
  grants: no `DELETE`, no `TRUNCATE`, `UPDATE` limited to the state-machine columns.
- `document_events` is append-only. Grants: `INSERT` only. No `UPDATE`/`DELETE` for any app
  role.
- Migrations to `core` require review from every ingestion owner and are applied
  expand-then-contract (see `docs/engineering/coding-standards.md`).
- The Python library is generated from the same state-machine table used by the Go library.
  The canonical definition lives in `docs/data/canonical-document-schema.md`.

Rationale: the lifecycle log is the product's provenance record. Routing every transition
through a registry service would put a network hop and a single point of failure in the hot
path of ingestion, and would make "write state + event" non-atomic. See ADR-0010.

## 3. Service contracts

### 3.1 `api` (Go)

**Owns:** `api` schema (idempotency keys only). Read-only on `core`, `search`, and `research`.
Public surface.

**Responsibilities**

- Authenticate and authorize requests; enforce per-key rate limits and body/URL limits.
- Validate inputs; enqueue investigations; return 202 with a status URL.
- Stream investigation progress over SSE (resumable with `Last-Event-ID`).
- Serve search (lexical, semantic, hybrid) and document/entity/claim reads.
- Enqueue single-URL ingestion (`POST /v1/urls`) at `interactive` priority.

**Does not:** crawl, parse, run LLM calls, write document state, or execute investigations.

**Scaling unit:** stateless pods behind a load balancer. Reads may go to a Postgres replica
when lag < 2 s; investigation reads are primary-only.

**Depends on:** Postgres (read-only), Temporal (workflow client), `scheduler` (internal
command). It does not talk to Redpanda directly.

> `api` submits fetch jobs by calling `scheduler` synchronously (`POST /internal/v1/fetch`).
> This is the one permitted `api → service` command. Timeout 2 s, no retries; a failed submit
> returns 503 and writes nothing.

### 3.2 `scheduler` (Go)

**Owns:** `ingest` schema (`frontier`, `robots_cache`, `host_state`, `fetch_jobs`).

**Responsibilities**

- Maintain the crawl frontier: URL admission, dedupe against `core` by `url_hash`,
  recrawl policy, per-host concurrency and rate limits.
- Fetch and cache `robots.txt`; enforce `Crawl-delay` and disallow rules.
- Assign priority lane (`interactive` | `bulk`) and publish `doc.fetch` messages keyed by
  host so per-domain ordering is preserved.
- Enforce global and per-tenant admission (max URLs per investigation, max in-flight).

**Does not:** perform HTTP fetches, parse, or write document content.

**Scaling unit:** 2+ replicas; leader election for frontier leasing via Postgres advisory
locks. Not horizontally sharded in Phase 1.

**Depends on:** Postgres (`ingest`, `core` read), Redpanda.

### 3.3 `fetcher` (Go)

**Owns:** `fetch` schema (`http_cache`: conditional-request metadata only), object storage
bucket `raw/`.

**Responsibilities**

- Consume `doc.fetch.interactive` or `doc.fetch.bulk`, one deployment per lane.
- Enforce policy: scheme allowlist, private-IP/SSRF block, response size cap, content-type
  allowlist, redirect cap, timeout.
- Perform conditional GETs (`ETag`, `If-Modified-Since`) when a prior revision exists.
- On changed content, insert revision N+1, reset gates, and enter at `fetched`. On identical
  content (304, or 200 with an unchanged normalized hash), update `last_seen_at` only.
- Render JavaScript only when the static HTML yields insufficient content, via a pooled
  headless browser; rendered output is a separate artifact with `renderer_version`.
- Write the raw artifact and its SHA-256 to object storage; transition to `raw_archived`.
- Classify failures (`transient`, `permanent`, `blocked_by_policy`, `robots_denied`).

**Does not:** extract text, follow links into the frontier, or call LLMs.

**Scaling unit:** deployment per lane. Bulk fetchers are capped by `maxReplicas` so they can
never starve interactive fetches of bandwidth or browser slots. Browser pool is a separate
sidecar/container with its own memory limit.

**Depends on:** Redpanda, object storage, Postgres (`core`), `robots_cache` (read-only).

### 3.4 `parser` (Python)

**Owns:** `parse` schema (`fingerprints`), object storage prefix `parsed/`.

**Responsibilities**

- Consume `doc.fetched`; extract main content (readability + site-specific rules),
  normalize whitespace/encoding, detect language, resolve canonical URL, extract
  publication metadata.
- Produce a deterministic normalized artifact keyed by its content hash.
- Compute exact hash and a 64-bit SimHash; check against `parse.fingerprints`; decide
  `deduplicated` or `duplicate` (pointing at a canonical document).
- Write `core.document_text` and advance state to `normalized` / `deduplicated`.

**Does not:** summarize, extract entities/claims, embed, or index.

**Scaling unit:** CPU-bound pods; autoscale on consumer lag. Parsing a document must be
deterministic given `(raw artifact, parser_version)`; the same inputs must produce byte-
identical normalized output, asserted by golden tests.

**Depends on:** Redpanda, object storage, Postgres (`core`, `ingest`).

### 3.5 `indexer` (Go)

**Owns:** `search` schema.

**Responsibilities**

- Consume `doc.enriched`; write chunk rows and embeddings idempotently keyed by
  `(document_id, revision, chunk_no)`.
- Advance state to `indexed`; report index freshness. The lexical `tsvector` and its GIN index
  live on `core.document_text`, written by `parser` and maintained by Postgres.
- Support rebuilds: `reindex` mode consumes from artifacts without refetching.

**Does not:** call LLMs (embeddings arrive in `doc.enriched`), decide relevance ranking
policy (that lives in `api`/query layer), or write document text.

**Scaling unit:** low CPU; DB-write bound. Batch upserts of 500 chunks.

**Depends on:** Redpanda, Postgres (`search`, `core` read), object storage (reindex only).

### 3.6 `enricher` (Python)

**Owns:** object storage prefix `enriched/`; `core.document_enrichment`.

**Responsibilities**

- Consume `doc.parsed`; produce summary, key passages, topic tags, entity candidates,
  claim candidates, and embeddings.
- Chunk deterministically (token-aware, overlapping); never re-chunk differently for the
  same `chunker_version`.
- Call `llm-gateway` only. Enforce per-document token/cost caps; degrade to
  non-LLM extraction rather than fail the document when the budget is exhausted.
- Emit one `doc.enriched` event containing all derived payload references and versions.

**Does not:** persist entities, claims, or graph edges (that is `graph-builder`), or write
to `search`.

**Scaling unit:** LLM-bound; autoscale on lag but hard-capped by the `batch` token budget.
Long backfills are a separate deployment with lower concurrency.

**Depends on:** Redpanda, object storage, Postgres (`core`), `llm-gateway`.

### 3.7 `graph-builder` (Python)

**Owns:** `graph` schema.

**Responsibilities**

- Consume `doc.enriched` in its own consumer group (independent of `indexer`).
- Resolve entities (exact normalized name → alias → embedding similarity → LLM adjudicate
  only above ambiguity threshold), record mentions with spans.
- Persist claims with mandatory document span + quote provenance; never update a claim in
  place.
- Upsert edges; update degree counters.
- Advance `kg_status` for the revision.

**Does not:** extract from raw text (reconsumes the enriched artifact), or call LLMs except
through `llm-gateway` for entity adjudication.

**Scaling unit:** DB-bound with occasional LLM bursts; can be replayed from `enriched/`
artifacts without LLM cost.

**Depends on:** Redpanda, object storage, Postgres (`graph`, `core`), `llm-gateway`.

### 3.8 `llm-gateway` (Python)

**Owns:** `llm` schema (usage ledger, response cache, prompt registry metadata).

**Responsibilities**

- Single egress for all model calls. No other service holds provider credentials.
- Route by capability and cost class (`interactive` | `batch`), enforce token budgets and
  concurrency, retry with jitter, fail over across providers/models.
- Enforce structured output via JSON Schema; validate and repair once before failing.
- Cache deterministically (hash of model + prompt + params + schema) with a stated TTL.
- Record usage per tenant, investigation, and document; enforce hard cost caps.
- Redact secrets/PII per policy before egress; log prompt hashes, not raw prompts by default.

**Does not:** decide research strategy, write documents, or persist model outputs beyond the
cache/ledger.

**Scaling unit:** I/O-bound; autoscale on queue depth. Budget accounting is in Postgres with
row-level locks; the ledger is authoritative.

**Depends on:** Postgres (`llm`), provider APIs.

### 3.9 `research-orchestrator` (Python, Temporal worker)

**Owns:** `research` schema (investigations, steps, messages, citations).

**Responsibilities**

- Run the investigation workflow: plan → search → read → follow citations → synthesize.
- Enforce `budget` (USD, wall clock, tool calls, documents read) as workflow state, not as a
  best-effort counter.
- Call `api`'s internal search/read endpoints (not the DB) so tenancy and rate limits apply.
- Request priority fetches through `scheduler` via a Temporal activity with its own timeout.
- Persist progress events to `research.investigation_events`; `api` streams them over SSE by
  tailing the table. No extra broker topic.
- Persist every citation with `document_id`, `revision`, span, and claim ids.
- Support cancel, pause/resume, and human-in-the-loop approval gates.

**Does not:** hold provider credentials, crawl directly, or write `core`.

**Scaling unit:** Temporal task queue `research`; worker concurrency capped separately from
`enricher`. Workflow code must be deterministic; all I/O in activities.

**Depends on:** Temporal, `llm-gateway`, `api` (internal), `scheduler` (internal),
Postgres (`research`).

### 3.10 `reconciler` (Go)

**Owns:** `ops` schema (`leases`, `reconcile_runs`).

**Responsibilities**

- Find documents stuck in a non-terminal state beyond the stage's lease timeout.
- Verify whether the stage actually completed (idempotent check) and republish if not.
- Release orphaned leases in `ops`; record reconcile runs and their outcomes.
- Never mutate document content: it only republishes work and clears leases. Writes are
  limited to `ops` and the `core` state-machine fields via the pipeline library.

**Scaling unit:** single active replica (leader election), low throughput.

**Depends on:** Postgres (all schemas, read-mostly), Redpanda.

## 4. Synchronous dependency matrix

| Caller | Callee | Purpose | Timeout | Retries |
|---|---|---|---|---|
| `api` | `scheduler` | enqueue interactive fetch | 2 s | 0 |
| `api` | Temporal | start/signal/query investigation | 2 s | 1 (idempotent key) |
| `enricher` | `llm-gateway` | generate/extract/embed | 120 s | 2 (gateway retries internally) |
| `graph-builder` | `llm-gateway` | entity adjudication | 60 s | 2 |
| `research-orchestrator` | `llm-gateway` | research calls | 180 s | 1 |
| `research-orchestrator` | `api` internal | search/read | 10 s | 2 |
| `research-orchestrator` | `scheduler` internal | priority fetch | 5 s | 1 |

All other communication is asynchronous. No service calls a service's database.

## 5. Topic ownership

| Topic | Producer | Consumers | Key |
|---|---|---|---|
| `doc.fetch.interactive` | `scheduler` | `fetcher-interactive` | host |
| `doc.fetch.bulk` | `scheduler` | `fetcher-bulk` | host |
| `doc.fetched` | `fetcher` | `parser` | document_id |
| `doc.parsed` | `parser` | `enricher` (batch group) | document_id |
| `doc.enriched` | `enricher` | `indexer`, `graph-builder` | document_id |
| `doc.indexed` | `indexer` | `reconciler` | document_id |
| `doc.graph_updated` | `graph-builder` | `reconciler` | document_id |
| `doc.available` | `indexer` / `graph-builder` (second gate) | `reconciler` (freshness accounting) | document_id |
| `doc.dead_letter` | all stages | `reconciler`, operators | document_id |
| `doc.reprocess` | operators, `reconciler` | `parser` / `enricher` / `indexer` / `graph-builder` (filter by mode) | document_id |

Topic names, envelope, and evolution rules: `docs/data/event-schema.md`.

## 6. Change protocol

A change crosses a boundary when it does any of: adds/removes a field in a shared contract,
changes a state transition, adds a topic or consumer group, changes a service's owned schema,
or changes an SLO. Such a change needs:

1. An ADR if it is architectural (new store, new boundary, new trust boundary).
2. A schema version bump and dual-write/dual-read plan for shared contracts.
3. Contract tests merged before the producer ships.
4. A rollback note in the PR: which version can still read the new data.
