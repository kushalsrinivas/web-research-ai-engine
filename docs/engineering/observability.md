# Observability

Status: Phase 0 baseline.

## 1. Principles

1. **The trace id on a document event is the join key** across HTTP, broker, DB, and LLM.
   If a transition cannot be traced end-to-end, it is not observable.
2. **Every stage reports the same four things:** rate, errors, duration, and lag.
3. **Business integrity is monitored like uptime** (citation validity, claim span match,
   duplicate rate), not left to nightly jobs.
4. **Cardinality is budgeted.** No user IDs, URLs, prompts, or document ids in metric labels.
5. **Logs are for debugging a specific unit of work; metrics are for aggregate truth.**

## 2. Tracing

- OpenTelemetry SDK in every service; OTLP to the collector; 10% head sampling, 100% for
  errors and for `interactive` lane work.
- Kafka producers inject `traceparent`; consumers extract it and start a child span linked to
  the producer span. A document's lifecycle is a trace graph, not a single trace.
- Span names: `stage.operation`, e.g. `fetch.get`, `parse.extract`, `enrich.claims`,
  `index.upsert_chunks`, `research.step.search`.
- Required span attributes:

| Attribute | Example |
|---|---|
| `document.id` | ULID |
| `document.revision` | `2` |
| `document.stage` | `parser` |
| `document.attempt` | `1` |
| `processor.version` | `parser@0.4.0+9f2c` |
| `tenant.id` | `public` (hashed if high cardinality) |
| `http.status_code` / `db.system` / `messaging.system` | per instrumentation |
| `llm.model`, `llm.tokens.input`, `llm.cost_usd` | gateway only |

Never attach document text, prompts, API keys, or full query strings.

## 3. Metrics

Prefix `wre_`. Labels in `{}` are the only allowed labels.

### Pipeline

| Metric | Type | Labels |
|---|---|---|
| `wre_document_transitions_total` | counter | `stage`, `from`, `to`, `outcome` |
| `wre_stage_duration_seconds` | histogram | `stage`, `outcome` |
| `wre_consumer_lag_messages` | gauge | `topic`, `group` |
| `wre_consumer_lag_seconds` | gauge | `topic`, `group` (age of newest ready message) |
| `wre_dead_letter_total` | counter | `stage`, `failure_class`, `code` |
| `wre_stale_events_total` | counter | `stage` |
| `wre_lease_expired_total` | counter | `stage` |
| `wre_reconcile_republished_total` | counter | `stage` |

### Fetch

| Metric | Type | Labels |
|---|---|---|
| `wre_fetch_requests_total` | counter | `lane`, `status_class`, `outcome` |
| `wre_fetch_duration_seconds` | histogram | `lane`, `renderer` |
| `wre_fetch_bytes` | histogram | `content_type` |
| `wre_fetch_policy_denied_total` | counter | `reason` |
| `wre_robots_denied_total` | counter | — |
| `wre_host_circuit_state` | gauge | `host_hash_bucket` (0 closed, 1 open) |

### Parse / index / graph

| Metric | Type | Labels |
|---|---|---|
| `wre_parse_failures_total` | counter | `code` |
| `wre_documents_parsed_total` | counter | `language` |
| `wre_duplicate_detected_total` | counter | `method` |
| `wre_index_lag_seconds` | gauge | — |
| `wre_index_chunks_total` | counter | `result` (`inserted`,`updated`,`skipped`) |
| `wre_claim_unverified_total` | counter | `extractor_version` |
| `wre_entity_merge_total` | counter | `method` |
| `wre_graph_edges_total` | counter | `edge_type`, `result` |

### LLM

| Metric | Type | Labels |
|---|---|---|
| `wre_llm_requests_total` | counter | `class`, `task`, `outcome` |
| `wre_llm_duration_seconds` | histogram | `class`, `model` |
| `wre_llm_tokens_total` | counter | `class`, `direction` |
| `wre_llm_cost_usd_total` | counter | `class`, `task` |
| `wre_llm_budget_denied_total` | counter | `scope` (`document`,`investigation`,`tenant`) |
| `wre_llm_cache_hits_total` | counter | `task` |

### API / research

| Metric | Type | Labels |
|---|---|---|
| `wre_http_requests_total` | counter | `route`, `method`, `status_class` |
| `wre_http_request_duration_seconds` | histogram | `route`, `method` |
| `wre_search_duration_seconds` | histogram | `mode`, `degraded` |
| `wre_search_results_total` | histogram | `mode` |
| `wre_investigations_active` | gauge | `status` |
| `wre_investigation_duration_seconds` | histogram | `outcome` |
| `wre_investigation_cost_usd` | histogram | — |
| `wre_citation_validation_failures_total` | counter | `reason` |

### Sources

| Metric | Type | Labels |
|---|---|---|
| `wre_sources_total` | gauge | `source_type`, `status` |
| `wre_sources_due` | gauge | — |
| `wre_source_health_total` | gauge | `state` |
| `wre_source_reliability_avg` | gauge | `source_type` |
| `wre_source_discoveries_total` | counter | `mechanism`, `outcome` |
| `wre_source_crawl_outcomes_total` | counter | `status` |
| `wre_source_scores_total` | counter | `model_version` |

Cardinality guardrails: no label may take more than 200 distinct values; `host` is hashed into
buckets; errors carry `code`, not message. CI checks the metric registry for label growth.

## 4. Logs

One JSON object per line:

```json
{
  "ts": "2026-11-22T10:04:12.481Z",
  "level": "info",
  "service": "parser",
  "version": "0.4.0+9f2c",
  "trace_id": "4bf92f...",
  "span_id": "a3ce92...",
  "document_id": "01JB2...",
  "revision": 2,
  "stage": "parser",
  "attempt": 1,
  "outcome": "ok",
  "duration_ms": 483,
  "msg": "transition committed"
}
```

Rules: no bodies, no prompts, no tokens, no full URLs with query strings. `error` logs include
`error.class`, `error.code`, and a truncated message. Sampling: info 100% for pipeline, debug
off in production unless a trace is sampled for debugging.

## 5. SLOs

| SLO | Target | Measurement |
|---|---|---|
| API availability | 99.5% monthly | non-5xx / total, excluding 4xx |
| Search latency | p95 ≤ 200 ms, p99 ≤ 500 ms | server-side histogram, `route=/v1/search` |
| Investigation completion | 95% of ≤40-doc investigations within 20 min | `wre_investigation_duration_seconds` |
| Interactive freshness | 95% fetched → available ≤ 5 min | event timestamps, lane=interactive |
| Bulk freshness | 95% fetched → available ≤ 30 min | event timestamps, lane=bulk |
| Ingestion lag | p95 ≤ 10 min at target load | `wre_consumer_lag_seconds` |
| Citation integrity | 100% of shipped citations valid | `wre_citation_validation_failures_total == 0` (hard gate) |
| Claim span integrity | ≥ 99.9% verified on write | `wre_claim_unverified_total / claims` |

Error-budget policy: at 50% budget consumed, freeze non-critical releases and prioritize
reliability; at 100%, feature work stops until the budget recovers. Citation/claim integrity
breaches are treated as incidents regardless of budget.

## 6. Dashboards

1. **Pipeline health** — transitions/s by stage, lag seconds, DLQ depth, lease expiries,
   freshness burn.
2. **Fetch** — success rate by lane and status class, latency, policy denials, circuit states,
   bytes.
3. **Enrichment & cost** — docs/s, degraded ratio, tokens and dollars by task, cache hit rate,
   budget denials.
4. **Search** — QPS, p50/p95/p99 by mode, degraded ratio, zero-result ratio, index size.
5. **Research** — active investigations, completion rate, duration histogram, cost histogram,
   tool calls per investigation, citation failures.
6. **Data integrity** — unverified claims, artifact hash mismatches, tombstone queue, graph
   rebuild backlog.
7. **Infrastructure** — Postgres connections/locks/replication lag, broker disk/ISR, object
   store error rate, Temporal task backlog.
8. **Source registry** — sources by type/status, due count, health states, crawl outcomes,
   discovery runs, and reliability by source type.

## 7. Alerts

| Alert | Condition | Severity |
|---|---|---|
| API 5xx | > 1% for 5 min | page |
| Search p95 | > 300 ms for 10 min | page |
| Ingestion stalled | lag > 15 min or no transitions for 5 min at expected volume | page |
| DLQ growth | > 0.5% of stage throughput for 15 min | page |
| Citation integrity | any validation failure in the last hour | page |
| Claim span mismatch | > 0.1% of claims in the last hour | page |
| Reconciler backlog | > 100 stuck documents for 30 min | ticket |
| LLM budget | 80% of daily cap | ticket |
| LLM budget | 100% of daily cap | page |
| Freshness burn | 2% of monthly budget in 1 h | page |
| Artifact hash mismatch | any | page |
| Graph rebuild backlog | > 24 h of documents | ticket |

Each alert links to a runbook (Phase 1 deliverable) and includes the dashboard panel and the
first diagnostic query.

## 8. Cost observability

- `llm-gateway` writes one ledger row per call: tenant, class, task, model, tokens, cost,
  investigation/document, timestamp. This is the source of truth, not metrics.
- Daily rollups by tenant, task, and model; weekly anomaly detection against the trailing
  7-day median.
- Cost per available document and cost per completed investigation are reported daily; a 20%
  week-over-week increase requires an explanation in the ops review.
- Storage cost is estimated from bucket inventory and Postgres size weekly.

## 9. Retention

| Signal | Retention |
|---|---|
| Traces | 7 days (30 days for errors) |
| Metrics | 13 months raw, 3 years downsampled |
| Logs | 30 days |
| LLM ledger | 13 months |
| Audit log | 1 year minimum |
