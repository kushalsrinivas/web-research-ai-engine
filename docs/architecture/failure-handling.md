# Failure Handling

Status: Phase 0 baseline.

## 1. Principles

1. **Failures are classified before they are retried.** Blind retries amplify outages.
2. **Every stage is idempotent.** Redelivery, duplicate workers, and replay are normal
   operating conditions, not incidents.
3. **A single document can never block a partition.** No unbounded retries in the hot loop.
4. **Policy failures are terminal.** Robots denials, SSRF blocks, and disallowed content
   never enter a retry loop.
5. **Partial availability beats total unavailability.** An indexed document without claims is
   searchable and flagged; it is not withheld.

## 2. Failure taxonomy

| Class | Examples | Retry | Destination |
|---|---|---|---|
| `transient` | connection reset, 5xx, 429, broker timeout, DB serialization failure | Yes, bounded | Same stage |
| `rate_limited` | 429, provider quota, `Retry-After` | Yes, honor header | Same stage, delayed |
| `permanent` | 404, 410, malformed HTML, unsupported content type, empty extraction | No (or 1 verification retry) | `failed` |
| `policy` | robots denied, SSRF target, disallowed scheme, malware signature | No | `quarantined` |
| `dependency` | object store unavailable, Postgres failover, LLM provider outage | Yes, with circuit breaker | Same stage |
| `budget` | per-document or per-investigation cost cap hit | No | Degraded mode where defined |
| `bug` | schema violation, panic, invariant breach | No after 2 attempts | `failed` + alert |

## 3. Retry policy

| Stage | Max attempts | Base | Backoff | Cap | Notes |
|---|---|---|---|---|---|
| fetch | 5 | 5 s | ×2, full jitter | 10 min | honor `Retry-After`; 4xx permanent except 408/425/429 |
| parse | 3 | 2 s | ×2, full jitter | 30 s | a parse failure on identical bytes is deterministic: do not retry a third time |
| enrich | 3 | 10 s | ×2, full jitter | 5 min | provider errors are retried inside `llm-gateway`; stage retries cover crashes |
| index | 5 | 1 s | ×2, full jitter | 60 s | DB-centric; serialization failures are safe to retry |
| graph | 3 | 2 s | ×2, full jitter | 60 s | entity adjudication failures default to "no merge", never block the document |
| research activity | per activity | 2 s | ×2 | 2 min | Temporal retry policy; workflow-level budget is authoritative |

Retries are scheduled with a **delay topic or Temporal timer**, never by sleeping in a
consumer. A sleeping consumer is a stalled partition.

### Deterministic-failure short circuit

If the same `(document_id, revision, stage, processor_version)` fails twice with the same
`error.code`, the third attempt is skipped and the document goes to `dead_lettered`. This
prevents a broken extractor from burning the queue.

## 4. Circuit breakers

Per host (fetcher) and per provider/model (llm-gateway):

- Window: 20 consecutive outcomes.
- Open at: ≥ 8 failures (40%) or 3 consecutive 429/503.
- Open duration: 5 min, then half-open with 1 probe; 3 successes close it.
- While open: fetches for that host are rescheduled by the scheduler (not failed), so the
  document retains its queue position. LLM calls fail over to the next provider or return a
  classified `dependency` error.

## 5. Dead letter queue

`doc.dead_letter` carries the original envelope, plus:

```json
{
  "original_topic": "doc.fetched",
  "original_partition": 7,
  "original_offset": 183422,
  "stage": "parser",
  "attempts": 3,
  "error": {"class": "permanent", "code": "EMPTY_EXTRACTION", "message": "..."},
  "processor_version": "parser@0.4.0+9f2c",
  "first_failed_at": "2026-11-22T10:00:00Z",
  "dead_lettered_at": "2026-11-22T10:04:12Z"
}
```

Rules:

- `failed` (permanent) does not enter the DLQ. It is recorded in events and counted; replaying
  it would reproduce the same failure.
- `quarantined` enters the DLQ for audit only and is never auto-replayed.
- DLQ retention 30 days; alert if depth > 0.5% of stage throughput for 15 min.
- Replay is an operator action with a dry-run: select by error code, stage, and time range;
  republish with `attempt = 1` and a `replay=true` header.
- Replay of a DLQ message that has already succeeded is a no-op because writes are
  idempotent. This is tested.

## 6. Stuck work and leases

Each stage takes a lease before processing:

```
ops.leases(stage, document_id, revision, attempt, worker_id, expires_at)
```

- Lease TTL = 3 × p99 stage duration (fetch 90 s, parse 60 s, enrich 6 min, index 60 s).
- The worker renews while alive. On crash the lease expires.
- `reconciler` runs every 60 s: finds non-terminal documents whose lease expired, verifies
  completion idempotently (does the derived row exist?), and either advances the state or
  republishes from the correct stage.
- A document stuck for > 6 h raises an alert with its event history attached.

## 7. Cascading failure controls

| Risk | Control |
|---|---|
| Crawl backlog starves enrichment | Separate planes, separate quotas, separate DB pools |
| Bulk backfill starves interactive fetch | Separate deployments; bulk `maxReplicas` hard cap; bulk pauses when interactive p95 breaches SLO |
| LLM cost runaway | Per-document cap, per-investigation cap, global daily budget; `llm-gateway` rejects over budget with `budget` class |
| Provider outage | Multi-provider routing, then degraded extraction (see below) |
| Postgres saturation | Per-plane pool caps, statement timeouts, read replicas for `api` search, queue backpressure via consumer pause |
| Hot partition (large domain) | `doc.fetch` keyed by host; scheduler limits per-host in-flight to 2; host sharding for the top 0.1% of domains |
| Object store outage | Fetcher pauses consumption (no raw artifact = no progress); broker buffers; no data loss |
| Broker outage | Producers fail after commit; `reconciler` republishes. Ingestion stops; reads continue |

### Degraded enrichment

If the LLM budget is exhausted or all providers are down, `enricher` produces:

- extractive summary (first N sentences / TextRank),
- chunks and lexical metadata,
- **no embeddings, no claims**,
- `degraded = true` on the enrichment row.

The document is indexed lexically and marked for re-enrichment when budget returns. This
keeps the corpus current without pretending the enrichment is complete.

## 8. Partial join failure

| Index | Graph | Result |
|---|---|---|
| done | done | `available` |
| done | failed | `indexed`, `kg_status=failed`; searchable; claims absent; retried by reconciler up to graph budget |
| failed | done | `kg_updated`; not searchable; graph intact; reindex retried |
| failed | failed | `enriched`, both gates failed; dead-lettered after budget |

A failed gate never rewinds a successful one — re-running the failed gate must not delete the
other gate's output.

## 9. Data integrity checks

Run continuously by `reconciler` (sampled) and fully in nightly jobs:

1. Every live revision (`available_revision`) has a `document_text` row and ≥ 1 chunk.
2. Every artifact URI in `documents` exists and matches its recorded SHA-256 (sampled 1%).
3. Every claim span resolves inside its document revision and its quote matches.
4. Every claim's `extractor_version` has a matching enrichment artifact for its revision.
5. No `document_events` row references a nonexistent document.
6. Orphan artifacts (no DB reference) are listed, not deleted, for 7 days.

Violations create an incident ticket and a metric; they never auto-delete data.

## 10. Runbooks to write in Phase 1

- Broker unavailable / consumer lag > 30 min.
- Postgres failover during ingestion.
- LLM provider hard outage.
- Poison message flood (single extractor bug).
- Duplicate-canonical oscillation.
- Object store credential expiry.

Each runbook states: detection signal, blast radius, immediate mitigation, data repair, and
the post-incident check that proves convergence.
