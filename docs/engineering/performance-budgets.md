# Performance Budgets

Status: Phase 0 baseline. Numbers are Phase 1 planning assumptions; changes require a note in
this file, not a silent config change.

## 1. Load assumptions

| Assumption | Value | Basis |
|---|---|---|
| Crawl rate, average | 2 pages/s (≈ 173k/day) | One crawler fleet, politeness-limited |
| Peak crawl rate | 10 pages/s | Bursts, new source onboarding |
| Backfill burst | 30 pages/s | Re-parse / new domain import, bulk lane only |
| New documents admitted | 50k/day during build-out, ~5k/day steady state | After dedupe, quality gate, and policy rejects; most steady-state fetches are recrawls |
| Corpus at 12 months | 5M documents, 20M chunks, 40M claims | ~100 days of build-out, then refresh-dominated growth with churn |
| Search | 50 QPS sustained, 150 QPS peak | 100:1 read:write for a research product |
| Concurrent investigations | 50 sustained, 100 peak | Product target |
| Investigations per day | 1,000 | 20 per concurrent slot |
| Interactive fetch requests | 5% of investigations × 40 docs | Priority lane |

Headroom rule: provision for **2× peak**, not average. Autoscaling reacts to lag within 2
minutes; a 10× spike is shed at the edge, not absorbed.

## 2. Budgets

### End-to-end

| Metric | Budget | Measured by |
|---|---|---|
| Interactive: fetched → available | p95 ≤ 5 min, p99 ≤ 10 min | event timestamps |
| Bulk: fetched → available | p95 ≤ 30 min | event timestamps |
| Corpus freshness after source change | ≤ 7 days (recrawl interval) | scheduler metrics |
| Investigation, ≤ 40 docs | p95 ≤ 5 min, hard cap 20 min | workflow metrics |
| Investigation time to first streamed step | ≤ 3 s | SSE event timestamps |

### Per stage (single document, warm caches)

| Stage | p50 | p95 | Hard timeout | Notes |
|---|---|---|---|---|
| Scheduler admission | 5 ms | 25 ms | 2 s | Includes robots cache hit |
| Fetch, static | 300 ms | 3 s | 30 s | Excludes renderer |
| Fetch, rendered | 2 s | 8 s | 45 s | Separate pool, ≤ 20% of fetches |
| Parse | 80 ms | 800 ms | 30 s | 2 MB text cap |
| Normalize + fingerprint | 20 ms | 150 ms | 10 s | |
| Enrich, local (summary/chunks) | 100 ms | 500 ms | 30 s | CPU |
| Enrich, LLM extraction | 2 s | 8 s | 120 s | Provider-dependent |
| Embedding, 4 chunks | 100 ms | 600 ms | 30 s | Local GPU |
| Index upsert | 30 ms | 300 ms | 60 s | 4 chunks |
| Graph upsert | 50 ms | 500 ms | 60 s | 8 claims, 14 mentions |
| Claim span verification | 5 ms | 20 ms | 1 s | Must remain synchronous |

Sum of p95s is not the end-to-end p95; stages queue. The end-to-end budget is enforced
separately by measuring transitions.

### API

| Route | p95 | p99 | Limit |
|---|---|---|---|
| `POST /v1/search` (lexical) | 120 ms | 300 ms | 100 results |
| `POST /v1/search` (hybrid) | 200 ms | 500 ms | 100 results |
| `GET /v1/documents/{id}` | 40 ms | 120 ms | |
| `GET /v1/entities/{id}/neighbors` | 150 ms | 400 ms | depth ≤ 3, 500 nodes |
| `POST /v1/investigations` | 80 ms | 250 ms | |
| `GET /v1/investigations/{id}` | 50 ms | 150 ms | |
| SSE event delivery | 200 ms | 1 s | from workflow emit |

Statement timeouts: search 2 s, graph traversal 3 s, writes 2 s, migrations 30 s (online
procedures exempt with approval).

### Search quality/latency guards

- No query may force a sequential scan on `document_text`; `EXPLAIN` is asserted in CI for
  the five canonical queries.
- Hybrid search fetches 100 lexical + 100 semantic candidates before fusion; not more.
- Zero-result rate is tracked; a jump > 5 points is a relevance regression, not a latency
  issue, but it blocks the same release.

## 3. Capacity math (verify before provisioning)

### Storage

| Item | Assumption | At 5M documents |
|---|---|---|
| Raw HTML (zstd ~4×) | 150 KB × 5M × ~1.3 revisions | ≈ 250 GB |
| Parsed artifacts | 8 KB × 5M | ≈ 40 GB |
| Enriched artifacts | 20 KB × 5M | ≈ 100 GB |
| Postgres text (TOAST-compressed) | ~8 KB effective × 5M | ≈ 40 GB + 30 GB GIN |
| Postgres metadata/events | 5M docs, 50M events | ≈ 40 GB |
| Chunks + HNSW | 20M × 3 KB + index overhead | ≈ 120 GB |
| Claims/mentions/edges | 40M claims, 120M mentions/edges | ≈ 80 GB |
| **Total** | | **≈ 700 GB**, provision 2 TB |

### Compute

| Component | Driver | Sizing |
|---|---|---|
| Fetcher | 2 pages/s × 0.3 s avg | ~1 CPU sustained, 4 for peak |
| Parser | 2 docs/s × 0.1 s CPU | < 1 CPU sustained, 4 peak |
| Enricher local | 2 docs/s × 0.15 s | < 1 CPU |
| Embedding GPU | 200k chunks/day ≈ 2.3/s | 1 × L4/A10 with ample headroom |
| Indexer/graph | 7 writes/s each | 1 replica each at MVP |
| LLM gateway | 50k docs × 1.2 calls + research | I/O bound, 2–6 pods |
| Postgres | 250 writes/s peak, 50 QPS search | 8 vCPU / 32 GB, 3,000 IOPS |

### LLM cost (build-out phase, 50k docs/day)

| Item | Unit cost | Daily | Monthly |
|---|---|---|---|
| Enrichment (50k docs × 7k tokens) | $0.0015/doc | $75 | $2.3k |
| Investigations (1,000 × 12 calls) | $0.40 avg | $400 | $12k |
| Entity adjudication | ~$0.0002/doc | $10 | $0.3k |
| **Total** | | **≈ $485** | **≈ $14.6k** |

Steady state (5k docs/day) drops enrichment to ~$7.50/day; investigations dominate at ~$12k/
month. A corpus-wide re-enrichment is a separate, explicitly budgeted project (≈ $7.5k for
5M documents) and runs in the bulk lane.

Budget guardrails: hard cap per document $0.01, per investigation $2 (default) / $10 (max),
per tenant/day configurable, global daily cap with `llm-gateway` refusing at 100%. Alerts at
50/80/100%.

Cost per completed investigation and cost per `available` document are published daily. A
20% week-over-week rise requires an explanation in the ops review.

## 4. Database budgets

| Constraint | Value |
|---|---|
| Connections per service | API 20, parser 10, enricher 10, indexer 10, graph 10, research 10 |
| Total connections | ≤ 150, leaving headroom on a 200-connection primary |
| Write amplification | ≤ 2.5× per document transition |
| Longest acceptable lock | 1 s (online migrations only) |
| Autovacuum lag | `document_events` and `chunks` monitored; partition if dead tuples > 20% |
| Replication lag alert | > 5 s for 2 min |
| Table size trigger for partitioning | `document_events` > 100 GB, `claims` > 100 GB |

Partitioning plan (when triggered): `document_events` by month, `chunks` by document hash
bucket, `claims` by subject hash.

## 5. Broker budgets

| Constraint | Value |
|---|---|
| Publish latency p99 | ≤ 50 ms |
| End-to-end event age (produce → consume) | p95 ≤ 30 s |
| Consumer lag | ≤ 5 min at target load |
| Disk per broker | 500 GB, ≤ 70% used |
| Message size | ≤ 256 KB (hard) |
| Producer retries | bounded, 30 s total |

## 6. How budgets are enforced

1. **CI performance tests** (see `testing-strategy.md` §8) run on a fixed dataset and fail on
   a >20% regression against the last accepted baseline.
2. **Production SLO alerts** (see `observability.md` §7) burn error budget on breach.
3. **Query plan assertions** for the canonical search/graph queries.
4. **Budget ledgers** in `llm-gateway` enforce cost at call time; a breach returns `402`
   rather than degrading silently.
5. **Capacity review** monthly: crawl rate, corpus growth, storage, LLM spend, and the
   projection to the next quarter. A breached capacity line is a planning input, not an
   incident.

## 7. Scaling triggers (documented, not automatic)

| Signal | Action |
|---|---|
| Search p95 > 200 ms sustained after tuning | Add read replica; then revisit OpenSearch (ADR-0005) |
| Chunk count > 10M and HNSW recall < 0.95 | Tune `ef_search`/`m`; then consider Qdrant (ADR-0006) |
| Graph traversal p95 > 300 ms | Add depth cap; then consider Neo4j (ADR-0007) |
| `document_events` > 100 GB | Partition by month |
| Parse throughput > 20 docs/s | Split parsing into a dedicated fleet per content type |
| LLM cost > $25k/month | Revisit extraction model/prompt, add a relevance gate before full extraction |
| Investigation p95 > 10 min | Profile workflow; cap tool calls per step; parallelize reads |
