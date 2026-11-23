# Event Schema

Status: Phase 0 baseline. JSON Schema files live in `contracts/events/v1/` and are the
executable form of this document. Both Go and Python validate against them at runtime.

## 1. Serialization decision

JSON (UTF-8, zstd compression at the broker) with an integer `schema_version`.

Why not Avro/Protobuf in Phase 1:

- Two languages need to read the same events; JSON Schema gives one authority without a
  codegen pipeline.
- Events are debuggable with `kcat`, which matters more than bytes at MVP volume.
- A schema registry can be added later; Redpanda's registry is available and unused today.

Cost: larger messages and weaker compile-time guarantees. Mitigation: runtime validation on
produce and consume, contract tests in CI, and a 256 KB message cap that forces large payloads
into object storage.

## 2. Envelope

Every message on every `doc.*` topic uses this envelope.

```json
{
  "event_id": "01JB2Q8Z9K4T7V3M6R1N5C0X8P",
  "event_type": "document.enriched",
  "schema_version": 1,
  "occurred_at": "2026-11-22T10:04:12.481Z",
  "producer": "enricher@0.7.2+3ad91f",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "tenant_id": "public",
  "attempt": 1,
  "document": {
    "document_id": "01JB2Q1H7S5D9F4K2M8N6P3R0T",
    "revision": 2,
    "state": "enriched"
  },
  "artifacts": [
    {
      "kind": "enriched",
      "uri": "s3://wre-enriched/public/01JB2Q1H.../2/enrich-2026-11.json.zst",
      "sha256": "3f2a...",
      "bytes": 182344,
      "codec": "zstd"
    }
  ],
  "payload": { }
}
```

### Field rules

| Field | Type | Required | Rule |
|---|---|---|---|
| `event_id` | ULID | yes | Unique per produced message; consumers dedupe on it for side effects outside the DB |
| `event_type` | string | yes | `document.{transition}` from the state machine |
| `schema_version` | integer | yes | Increment only for breaking changes |
| `occurred_at` | RFC 3339 UTC | yes | Producer clock; DB commit time wins for state |
| `producer` | string | yes | `service@semver+gitsha` |
| `trace_id` | hex string | yes | W3C trace id; also in `traceparent` header |
| `tenant_id` | string | yes | Never empty |
| `attempt` | integer | yes | Per-stage attempt, ≥ 1 |
| `document.document_id` | string | yes | ULID |
| `document.revision` | integer | yes | ≥ 1 |
| `document.state` | string | yes | State after the transition; `document.fetched` carries `raw_archived` |
| `artifacts` | array | no | References only; never inline bytes |
| `payload` | object | yes | Event-specific, may be `{}` |

### Headers

| Header | Value |
|---|---|
| `traceparent` | W3C trace context |
| `content-type` | `application/json` |
| `schema-version` | Same as envelope |
| `x-producer` | `service@version` |
| `x-attempt` | Same as envelope |
| `x-replay` | `true` when replayed from DLQ |

## 3. Topics

| Topic | Partitions | Key | Retention | Consumer group |
|---|---|---|---|---|
| `doc.fetch.interactive` | 12 | host | 24 h | `fetcher-interactive` |
| `doc.fetch.bulk` | 24 | host | 7 d | `fetcher-bulk` |
| `doc.fetched` | 24 | document_id | 7 d | `parser` |
| `doc.parsed` | 24 | document_id | 7 d | `enricher`, `enricher-backfill` |
| `doc.enriched` | 24 | document_id | 7 d | `indexer`, `graph-builder` |
| `doc.indexed` | 12 | document_id | 24 h | `reconciler` |
| `doc.graph_updated` | 12 | document_id | 24 h | `reconciler` |
| `doc.available` | 12 | document_id | 24 h | `reconciler` (freshness) |
| `doc.dead_letter` | 6 | document_id | 30 d | `reconciler`, operators |
| `doc.reprocess` | 6 | document_id | 7 d | stage-specific |

Rules:

- Topic names describe the **payload**, not the terminal state. `doc.parsed` is emitted after
  dedupe and carries both the parse result and the dedupe outcome; `doc.fetched` is emitted
  after the raw artifact is archived.
- Keying `doc.fetch.*` by host preserves per-domain ordering and keeps politeness local to a
  partition. Downstream keys by `document_id` because per-document ordering is what matters.
- `cleanup.policy=delete` everywhere. No compaction: state is in Postgres.
- `min.insync.replicas=2`, `acks=all` for producers. Throughput is sufficient at MVP volume.
- Consumer groups are stable and named in code, never generated at runtime (otherwise
  offsets are lost on deploy).

## 4. Event payloads

Only fields needed by the next stage. Everything else is fetched from the canonical record.

### `document.scheduled`

```json
{
  "lane": "interactive",
  "priority": 10,
  "not_before": "2026-11-22T10:00:00Z",
  "robots_hash": "sha256:...",
  "crawl_delay_ms": 1500,
  "is_recrawl": false
}
```

### `document.fetched`

```json
{
  "http_status": 200,
  "content_type": "text/html; charset=utf-8",
  "content_length": 84210,
  "etag": "\"abc123\"",
  "last_modified": "2026-11-01T08:00:00Z",
  "redirects": 1,
  "renderer_used": false,
  "elapsed_ms": 412
}
```

### `document.parsed`

```json
{
  "extractor": "readability+trafilatura",
  "parser_version": "0.4.0",
  "word_count": 1840,
  "language": "en",
  "canonical_url_changed": false,
  "duplicate": false,
  "simhash": "0x91ab34c02d7e8811"
}
```

### `document.enriched`

```json
{
  "enrichment_version": "enrich-2026-11",
  "summary_chars": 420,
  "chunk_count": 9,
  "entity_count": 14,
  "claim_count": 6,
  "embedding_model": "bge-m3",
  "embedding_dim": 1024,
  "token_cost": 0.0121,
  "degraded": false
}
```

### `document.indexed` / `document.kg_updated`

```json
{ "gate": "index", "gate_status": "done", "chunks": 9, "index_version": "pgvector-1" }
```

If the other gate is already done, the stage emits **`document.available`** instead of its
intermediate event, with the same payload plus `promoted=true` and `live_revision`. This keeps
one lifecycle event per transition: the first gate records `indexed` or `kg_updated`, the
second records `available`.

### `document.failed`

```json
{
  "stage": "parser",
  "failure_class": "permanent",
  "code": "EMPTY_EXTRACTION",
  "message": "no main content above threshold",
  "retryable": false,
  "attempts": 3
}
```

### `document.duplicate`

```json
{ "duplicate_of": "01JB2P...", "method": "simhash", "distance": 2 }
```

### `document.reprocess`

```json
{ "mode": "reparse", "target_version": "parser@0.5.0", "reason": "extraction rule fix", "requested_by": "operator" }
```

## 5. Evolution rules

1. **Additive within a major version.** New optional fields → no version bump. Consumers must
   ignore unknown fields (enforced by `additionalProperties: true` in the consumer schema).
2. **Breaking** = removing/renaming a field, changing a type, changing semantics of an
   existing value. Requires `schema_version + 1` and a dual-publish window of at least 7
   days, or a full-topic replay plan.
3. Producers publish the **new** schema only after all consumers can read it. Deploy order:
   consumers → producers.
4. Every event type has a JSON Schema fixture and a round-trip test in both languages.
5. A field may be deprecated by writing `null` and documenting it for one minor release
   before removal.

## 6. Delivery and idempotency

- Delivery is at-least-once. Producers retry on broker errors with bounded backoff.
- Consumers must be idempotent on the **DB state guard** (`UPDATE ... WHERE state = expected`),
  not on `event_id`. `event_id` dedupe is used only for non-idempotent side effects (email,
  billing).
- Out-of-order messages are possible only across partitions. Because the key is
  `document_id` downstream, per-document order is preserved within a topic.
- A message older than the document's current state is dropped with a metric
  `pipeline_stale_events_total{stage}`. It is never an error.

## 7. Size and content limits

| Limit | Value | Behavior |
|---|---|---|
| Message size | 256 KB | Producer rejects; payload must move to object storage |
| Artifact reference | 1 per kind | Multiple versions of the same kind are not allowed in one event |
| Text in payload | 0 bytes | Text never travels in events; only URIs and spans |
| PII | none | Events may contain URLs and extracted names; no emails, phones, or credentials |

## 8. Security

- Events are internal only. `api` never exposes raw events; SSE emits a separate, filtered
  `research.*` progress schema.
- Consumers validate `tenant_id` against the document row; a mismatch is a bug-level failure
  and quarantines the message.
- URLs in events are data, not instructions. No consumer fetches a URL because an event says
  so; only `scheduler` admits URLs, and only through policy.
