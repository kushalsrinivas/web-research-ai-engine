# API Contracts

Status: Phase 0 baseline. The executable contract is `contracts/openapi.v1.yaml`; this
document records the rules that are easy to lose in generated specs.

## 1. Conventions

| Concern | Rule |
|---|---|
| Base path | `/v1`; breaking changes require `/v2` |
| Content type | `application/json` except SSE (`text/event-stream`) |
| Auth | `Authorization: Bearer <api_key>`; scopes `read`, `research`, `ingest`, `admin` |
| Request id | `X-Request-Id` accepted; generated if absent; echoed |
| Trace | `traceparent` propagated; `trace_id` returned in every error |
| Time | RFC 3339 UTC with milliseconds |
| IDs | ULIDs, 26 chars, opaque to clients |
| Idempotency | `Idempotency-Key` required on `POST /investigations` and `POST /urls` |
| Pagination | Opaque `cursor`; `limit` 1–100, default 20 |
| Errors | RFC 9457 `application/problem+json` |
| Compression | gzip/br accepted; SSE never compressed |
| Rate limits | Per key: 60 reads/min, 10 research starts/min, 60 ingests/min; `429` with `Retry-After` |

### Error envelope

```json
{
  "type": "https://errors.wre.dev/validation-failed",
  "title": "Request validation failed",
  "status": 422,
  "detail": "scope.max_documents must be between 1 and 200",
  "instance": "/v1/investigations",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errors": [{"field": "scope.max_documents", "code": "OUT_OF_RANGE"}]
}
```

Clients must branch on `status` and `type`, never on `detail` text.

## 2. Investigations

### `POST /v1/investigations`

```json
{
  "question": "How did EU AI Act enforcement change between 2025 and 2026?",
  "scope": {
    "domains": ["europa.eu", "europarl.europa.eu"],
    "languages": ["en"],
    "published_after": "2025-01-01",
    "max_documents": 40,
    "allow_web_search": true
  },
  "budget": {"max_usd": 2.00, "max_minutes": 20},
  "priority": "interactive",
  "output": {"format": "markdown", "citations": "inline"}
}
```

Response `202`:

```json
{
  "investigation_id": "01JB7...",
  "status": "queued",
  "created_at": "2026-11-22T10:00:00Z",
  "links": {
    "self": "/v1/investigations/01JB7...",
    "events": "/v1/investigations/01JB7.../events",
    "cancel": "/v1/investigations/01JB7.../cancel"
  }
}
```

Validation:

- `question` 8–2,000 chars.
- `max_documents` 1–200; declines beyond 100 require the `research` scope and are capped by
  tenant quota.
- `max_usd` 0.05–10.00; server may clamp to tenant cap and reports the applied value.
- `max_minutes` 1–20.
- Idempotency: same key + same body returns the original `202`; same key + different body
  returns `422 IDEMPOTENCY_MISMATCH`.

### `GET /v1/investigations/{id}`

```json
{
  "investigation_id": "01JB7...",
  "status": "running",
  "question": "...",
  "plan": [
    {"step": 1, "kind": "search", "status": "done", "summary": "12 sources found"},
    {"step": 2, "kind": "read", "status": "running", "summary": "reading 7/12"}
  ],
  "budget": {"max_usd": 2.0, "spent_usd": 0.62, "max_minutes": 20, "elapsed_s": 184,
             "documents_read": 7, "calls": 11},
  "result": null,
  "citations": [],
  "created_at": "...", "started_at": "...", "completed_at": null,
  "error": null
}
```

`status` ∈ `queued | running | paused | completed | failed | cancelled | budget_exhausted`.
Terminal states are immutable.

When completed, `result` contains `{format, content_markdown, token_cost, as_of}` and
`citations` contains one entry per `[n]` marker:

```json
{
  "index": 1,
  "document_id": "01JB2...",
  "revision": 3,
  "url": "https://example.org/a",
  "title": "…",
  "span": {"start": 4210, "end": 4387},
  "quote": "verbatim text",
  "claim_ids": ["01JB8..."],
  "retrieved_at": "2026-11-22T10:02:11Z"
}
```

Every citation is validated before completion: document exists, revision exists, quote
matches span. A failed validation removes the sentence using that citation from the report and
records `report.repaired = true`. It never ships an unverifiable citation.

### `GET /v1/investigations/{id}/events` (SSE)

```
event: step
id: 000017
data: {"step":2,"kind":"read","status":"running","summary":"reading 7/12"}

event: citation
id: 000018
data: {"index":4,"document_id":"...","revision":2,"quote":"..."}

event: done
id: 000042
data: {"status":"completed","spent_usd":0.81}
```

- Event ids are monotonically increasing integers; reconnect with `Last-Event-ID` replays from
  the next id. Buffer 1,000 events per investigation; a client that falls further behind gets
  a `resync` event and must call `GET /v1/investigations/{id}`.
- Heartbeat comment every 15 s.
- One stream per investigation per key; a second concurrent stream is allowed but does not
  duplicate work.

### `POST /v1/investigations/{id}/cancel`

`202`; cancellation is cooperative (workflow signal). Returns `409` if already terminal.
Effects: no new LLM calls, in-flight activities allowed 30 s to abort, partial result stored
with `status=cancelled`.

### `POST /v1/investigations/{id}/resume`

Only for `paused` (human approval) or `budget_exhausted` with an increased budget.
`409` otherwise.

## 3. Search

### `POST /v1/search`

```json
{
  "query": "AI Act high-risk obligations",
  "mode": "hybrid",
  "filters": {
    "languages": ["en"],
    "hosts": ["europa.eu"],
    "published_after": "2025-01-01",
    "entity_ids": []
  },
  "limit": 20,
  "cursor": null,
  "snippet": true
}
```

| Mode | Behavior |
|---|---|
| `lexical` | Postgres full-text, `ts_rank_cd` with title weight A and body weight B |
| `semantic` | pgvector cosine over chunks, max-pool per document |
| `hybrid` | Reciprocal Rank Fusion of both (k=60), then dedupe by document |

Response:

```json
{
  "results": [{
    "document_id": "01JB2...",
    "revision": 3,
    "url": "https://...",
    "title": "…",
    "snippet": "…<em>high-risk</em>…",
    "score": 0.0312,
    "mode": "hybrid",
    "matched_chunks": [{"chunk_no": 3, "score": 0.81}],
    "published_at": "2025-11-04T00:00:00Z",
    "language": "en"
  }],
  "next_cursor": "eyJzIjowLjAzMSwiaWQiOiIwMUpCMiJ9",
  "took_ms": 41,
  "degraded": false
}
```

`filters` apply to the live revision. `results[].revision` is always `available_revision`, never
a revision that is still processing.

`degraded=true` when semantic search is unavailable and lexical-only results are returned.
Clients must display that state in research UIs.

Limits: `query` 1–500 chars; `limit` ≤ 100; scans capped. Public search only returns **live**
revisions (`available_revision is not null`), i.e. the last revision that completed both gates;
a document being recrawled does not disappear. Operators with the `admin` scope may pass
`include_unavailable=true`; other callers receive `422`.

## 4. Documents and evidence

| Method | Path | Returns |
|---|---|---|
| `GET` | `/v1/documents/{id}` | metadata, current revision, live revision (`available_revision`), state, gate statuses, artifact links (signed, 10 min TTL) |
| `GET` | `/v1/documents/{id}/revisions` | revision list with hashes and fetch times |
| `GET` | `/v1/documents/{id}/claims?status=active` | claims with spans and quotes |
| `GET` | `/v1/documents/{id}/text?revision=` | plain text, `admin` or `research` scope only |
| `GET` | `/v1/documents/{id}/events?limit=100` | lifecycle log projection (admin only) |

`GET /v1/documents/{id}` never leaks another tenant's document; cross-tenant access returns
`404`, not `403`, to avoid existence disclosure.

## 5. Entities and claims

| Method | Path | Returns |
|---|---|---|
| `GET` | `/v1/entities/{id}` | canonical entity, aliases, `merged_from`, stats |
| `GET` | `/v1/entities/{id}/claims?predicate=&limit=` | active claims, paginated |
| `GET` | `/v1/entities/{id}/neighbors?depth=1&edge_types=` | capped neighborhood, `truncated` flag |
| `GET` | `/v1/claims/{id}` | claim + source document + span + relations |
| `GET` | `/v1/claims/contradictions?subject_entity_id=` | grouped conflicting values |

Neighborhood responses always include `depth`, `node_count`, `truncated`, and `edge_types`
actually used, so a client can render "showing 50 of 1,204 connections".

## 6. Ingestion

### `POST /v1/urls`

```json
{"url": "https://example.org/report", "priority": "interactive", "recrawl": false}
```

`202`:

```json
{"document_id": "01JB3...", "state": "discovered", "created": true,
 "estimated_available_at": "2026-11-22T10:00:45Z"}
```

- Unknown URL: creates identity, enqueues at `interactive`.
- Known and `available`: returns `200` with existing document unless `recrawl=true`, which
  enqueues a refetch (`202`).
- SSRF/disallowed scheme: `422` with `URL_NOT_ALLOWED`. This check happens again in the
  fetcher; the API check is convenience, not security.
- Rate limited per key; batch ingestion is a separate admin endpoint in Phase 2.

## 7. Internal endpoints

Not exposed by the ingress. Authenticated with service identity (mTLS) and a short-lived
service token. No `tenant_id` is trusted from the body; it comes from the token.

### `scheduler` — `POST /internal/v1/fetch`

```json
{"url": "...", "tenant_id": "public", "lane": "interactive",
 "reason": "investigation:01JB7...", "not_before": null}
```

`202` with `{document_id, state}`; `409` if already in flight; `422` if policy rejects.
Idempotent on `(tenant_id, url_hash, lane)`.

### `llm-gateway` — `POST /internal/v1/generate`

```json
{
  "class": "batch",
  "task": "claims",
  "tenant_id": "public",
  "investigation_id": null,
  "document": {"document_id": "01JB2...", "revision": 2},
  "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "json_schema": {"type": "object", "properties": {}},
  "max_tokens": 1200,
  "temperature": 0,
  "budget": {"max_usd": 0.02}
}
```

```json
{
  "request_id": "01JB9...",
  "output": "{\"claims\":[...]}",
  "parsed": {"claims": []},
  "model": "provider/model@version",
  "usage": {"input_tokens": 3120, "output_tokens": 410, "cost_usd": 0.0041},
  "cached": false,
  "latency_ms": 1840
}
```

| Status | Meaning |
|---|---|
| `400` | invalid request or schema |
| `402` | `budget_exhausted` — no provider call made |
| `422` | `content_refused` — provider safety refusal, classified, not retried |
| `502` | `schema_invalid` after one repair attempt |
| `503` | `provider_unavailable` after failover attempts |

`llm-gateway` is the only service allowed to make outbound model calls. Its ledger row is
written before the response is returned; a missing ledger row is a bug.

### `llm-gateway` — `POST /internal/v1/embed`

Batch up to 256 texts per call, fixed model per deployment, returns vectors and a
`model_version`. A vector without its model version is rejected by `indexer`.

### `api` internal — `POST /internal/v1/search`, `GET /internal/v1/documents/{id}`

Used by `research-orchestrator` so that tenancy, rate limits, and redaction are enforced in one
place. Same schemas as public, plus `include_text=true` for the read tool.

## 8. Versioning and deprecation

- `/v1` is frozen at Phase 0. Additive fields are allowed at any time.
- Removing or changing a field: new major path. Old path supported for 90 days with
  `Deprecation` and `Sunset` headers.
- Unknown enum values must be tolerated by clients (forward compatibility).
- The OpenAPI spec is generated from code annotations and diffed in CI; a breaking diff fails
  the build unless the PR includes a major-version bump.
