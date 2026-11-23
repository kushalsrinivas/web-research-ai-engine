# Testing Strategy

Status: Phase 0 baseline.

## 1. What must be true

The platform's failure modes are data loss, silent corruption, and untraceable claims. Tests
target those first, not line coverage.

Non-negotiable guarantees:

1. No document is lost: every admitted URL reaches a terminal state or is visible in the DLQ.
2. Replaying any stage converges to the same state (idempotency).
3. Every claim's quote matches its source span.
4. Every search result and citation resolves to a stored revision.
5. A policy failure never becomes a retried job.

## 2. Test layers

| Layer | Scope | Runs | Budget |
|---|---|---|---|
| Unit | Pure functions: URL canonicalization, normalization, chunking, state guards, parsers | Every commit, no I/O | < 60 s |
| Contract | Event/API schemas in Go and Python against shared fixtures | Every commit | < 30 s |
| Component | One service with real Postgres + Redpanda + MinIO (testcontainers) | Every PR | < 8 min |
| Integration | Two or three stages wired end-to-end on a synthetic corpus | Every PR | < 12 min |
| End-to-end | Full pipeline from URL to citation on a local fixture site | Every merge to main | < 20 min |
| Performance | Ingestion and search budgets on a fixed dataset | Nightly + release candidate | < 45 min |
| Chaos | Fault injection: kill workers, drop publishes, broker/DB blips | Nightly | < 30 min |
| LLM regression | Fixed prompt/extraction suite, recorded or stubbed providers | Every PR for prompt changes | < 10 min |

No test hits the public internet. The fetcher tests use a local fixture server; SSRF tests
use a controlled DNS resolver.

## 3. Fixtures and corpora

```
/testdata
  /sites/             static fixture sites served by an in-process HTTP server
  /artifacts/         raw HTML + expected parsed/normalized output (golden files)
  /corpus/            1,000 documents with known duplicates, 3 languages, edge cases
  /transcripts/       captured event sequences for replay tests
  /claims/            annotated extraction examples with spans and expected claims
  /llm/               recorded request/response cassettes keyed by prompt hash
```

Rules:

- Golden files are reviewed like code. Changing one requires a stated reason in the PR.
- Corpora contain no personal data; synthetic names are used.
- A fixture added for a bug is named after the issue.

## 4. Required test cases per component

### `scheduler`
- URL canonicalization property tests: idempotent, fragment-insensitive, tracking params
  removed, no two distinct inputs collapse to one URL unless intentionally equivalent.
- Robots deny/allow/crawl-delay; cached robots respects TTL.
- Per-host concurrency and rate limits never exceed config under 10k concurrent admissions.
- A URL already known at the same revision is not re-enqueued.

### `fetcher`
- SSRF matrix: `127.0.0.1`, `::1`, `169.254.169.254`, RFC1918, decimal/octal IP encodings,
  DNS rebinding (resolved IP checked after connection), `file://`, `gopher://`, redirects to
  private ranges.
- Size cap aborts mid-stream; timeout; too many redirects.
- Conditional GET: 304 produces no new revision.
- Artifact hash mismatch aborts before state advancement.
- Rendered fallback only when static extraction fails the threshold.

### `parser`
- Golden extraction for 20 fixture sites including boilerplate-heavy pages, AMP, paywalls,
  and non-HTML content types.
- Determinism: same artifact + version → byte-identical normalized output across 100 runs.
- Language detection: `und` below confidence.
- Exact and near-duplicate detection, including canonical selection tiebreak.
- Empty extraction → `EMPTY_EXTRACTION`, permanent, no retry storm.

### `enricher`
- Chunk boundaries stable across versions; no empty chunks; overlap preserved.
- Budget exhaustion → `degraded=true`, document still indexed, no claims written.
- Provider refusal → classified `content_refused`, not retried.
- The same document enriched twice writes one active enrichment and one event.

### `indexer`
- Replay of `doc.enriched` twice leaves identical row counts.
- Chunk upsert replaces only rows for the same revision/version.
- Hybrid search returns RRF ordering on a hand-computed fixture.
- `degraded` semantics when embeddings are missing.

### `graph-builder`
- Claim with a quote that does not match its span → `unverified`, excluded from retrieval.
- Entity resolution matrix: exact, alias, embedding, ambiguous → LLM, new.
- Merge resolves chains and rewrites mentions atomically; a merge failure rolls back.
- Replaying an enriched event does not duplicate claims, mentions, or edges.

### `llm-gateway`
- Budget ledger is written before response return; concurrent calls cannot overspend.
- Failover across two stub providers; no duplicate spend on retry.
- Schema-invalid output is repaired once, then `502`.
- Cache key includes model, prompt, params, and schema; a prompt change misses.

### `research-orchestrator`
- Budget enforcement: a workflow cannot exceed `max_usd` even under retries.
- Cancellation stops new calls within one activity boundary.
- A citation without a valid span is removed and the report is marked repaired.
- Workflow replay from history produces the same plan (Temporal determinism test).

### `api`
- Auth scopes per endpoint; cross-tenant reads return `404`.
- SSE resume with `Last-Event-ID`, including a gap that triggers `resync`.
- Idempotency key replay and mismatch.
- Pagination cursor stability under concurrent inserts.

## 5. Fault-injection scenarios

Each runs against the component stack with a harness that can inject failure at a named point.

| Scenario | Expected |
|---|---|
| Kill parser pod mid-batch | Lease expires, reconciler republishes, no duplicate rows |
| Broker unavailable for 60 s after DB commit | Publishes fail, reconciler republishes, state converges |
| Postgres failover during indexing | Consumer pauses, retries, no partial chunk sets |
| Object store 503 on raw write | Fetcher does not advance state; retry later; no orphan event |
| Poison message (always throws) | Attempts exhausted → DLQ; partition keeps moving |
| 5% of publishes dropped | Reconciler converges the corpus within 10 min |
| Duplicate delivery of every message ×3 | No duplicate claims, chunks, edges, or events |
| LLM provider total outage | Enrichment degrades, ingestion continues, alert fires |

The "no orphan" test asserts that after injecting failures for 10 minutes, every document in
`core.documents` is either terminal or has an active lease, and every non-terminal document's
last event is < lease TTL old.

## 6. Contract tests

- Every JSON Schema has a valid and invalid fixture.
- Go and Python both load `/contracts/fixtures/*.json`; a mismatch fails both builds.
- OpenAPI is validated against handler tests: response status and body shape must match the
  spec (schemathesis or equivalent on a subset).
- Event producer tests assert the exact envelope fields; consumer tests assert unknown-field
  tolerance.

## 7. LLM testing

| Test | Method |
|---|---|
| Extraction quality | 200 annotated documents; precision/recall per claim field; gate at ≥ 0.80 precision for entity, ≥ 0.75 for claim |
| Span faithfulness | 100% of accepted claims must match the source span; any mismatch blocks release |
| Schema adherence | 500 generated cases; invalid JSON ≤ 0.5% after one repair |
| Prompt injection | 50 adversarial documents; no tool call, no system-prompt disclosure, no URL fetch |
| Cost | tokens per document within 15% of the recorded baseline for the same corpus |
| Latency | p95 generation within provider SLO + 20% |

These run against recorded cassettes in PRs and against live providers nightly (budget-capped).

## 8. Performance and load tests

- Ingestion: 10 pages/s for 15 min against the fixture corpus; assert lag drains, p95 fetch
  within budget, no DLQ growth.
- Search: 50 QPS mixed lexical/hybrid for 10 min; assert p95 and error rate.
- Research: 20 concurrent investigations; assert no starvation of ingestion and budget
  accuracy within 1%.
- Soak: 24 h at 50% load; assert memory stable (no goroutine/task leak), no unbounded table
  growth.

Results are recorded in `docs/engineering/performance-budgets.md` with date, commit, and
hardware class. A regression > 20% blocks release.

## 9. CI pipeline

```
1. lint + format + typecheck          (all)
2. unit + contract                    (all, parallel)
3. build images
4. component + integration            (per affected service, testcontainers)
5. e2e                                (main branch and release candidates)
6. perf + chaos                       (nightly)
7. security scans                     (deps, images, secrets)
```

- Affected-only test selection is allowed for PRs; `main` runs everything.
- Flaky tests: quarantined within 24 h with an owner and an issue; a quarantined test does not
  count as coverage. More than 2% flake rate in a week blocks the release.
- Coverage is reported, not gated, except for `wre_pipeline` state transitions and fetcher
  policy, which require ≥ 90% branch coverage.

## 10. Release gate

A release ships only if: unit/contract/component green, e2e green on main within 24 h,
performance within budget, no open critical security finding, and the migration plan is
rehearsed in staging.
