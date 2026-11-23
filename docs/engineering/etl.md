# ETL and canonical documents

`parser` consumes raw artifacts produced by `fetcher` and turns them into canonical,
normalized, versioned documents. Postgres is the queue; S3 keeps both the raw bytes and
every derived representation. Nothing in this phase mutates a raw artifact.

## Pipeline

```
core.documents (raw_archived)
        │ dispatcher
        ▼
processing.jobs ── claim ──► extract ──► normalize ──► quality ──► deduplicate
        │                                               │
        │                                               ├─ parsed JSON (S3)
        │                                               └─ canonical JSON (S3)
        ▼
core.document_revisions / core.document_text / processing.document_outputs
```

Supported formats and extractors:

| Format | Extractor |
|---|---|
| HTML | trafilatura (fallback: lxml text) |
| PDF | PyMuPDF |
| DOCX | python-docx |
| XML | defusedxml + ElementTree |
| JSON | native parser + deterministic flattening |
| RSS / Atom | feedparser |
| Plain text | native decoder + charset-normalizer |

HTML extraction also records title, authors, canonical URL, description, tags, images,
outgoing links, visible-text length, and heading count for quality scoring. Boilerplate
is removed from the canonical text but the parsed representation keeps the extractor's raw
text, warnings, and metadata.

## States

`raw_archived → parsed → deduplicated | duplicate | quarantined | failed`

- `quarantined` — empty text, too little text, or quality below `ETL_MIN_QUALITY`.
  The parsed and canonical artifacts are still written for inspection, and the document's
  `failed_stage` is `etl.empty_content`, `etl.short_content`, or `etl.low_quality`.
- `failed` — retries exhausted for an infrastructure failure (S3, DB).
- `duplicate` — another document in the same tenant has the same normalized content
  hash; `duplicate_of` points at the first document.
- `deduplicated` — processed and unique (near-duplicate detection is deferred).

`available_revision` is deliberately **not** set in this phase; it moves when the index
and graph gates complete.

## Versioning and replay

Every processing run writes:

- `processing.versions` — registered processor, config hash, active flag.
- `processing.document_outputs` — one row per `(document_id, revision, processing_version)`
  with title, body, hashes, quality, warnings, and artifact URIs.
- `core.document_revisions` — current `parsed_artifact_uri`, `parser_version`,
  `normalizer_version`, title, language, author, and `content_hash_norm`.
- `core.document_text` — the current lexical view (truncated at 2 MB, flagged
  `TEXT_TRUNCATED`).
- `core.document_events` — `parser.extract`, `parser.normalize`, `parser.rollback` audit rows.

Kafka document events are not produced in this phase; the durable queue, version registry,
and append-only audit log cover replay and provenance. Event contracts remain in
`contracts/events/v1` for the later stream integration.

The same version and raw revision are deterministic: reprocessing upserts the same output
row. A new version keeps the previous output reachable, which makes before/after quality
comparisons and rollback possible.

Raw bytes remain in `wre-raw`. Parsed and canonical artifacts live in `wre-parsed`:

```
s3://wre-parsed/parsed/{tenant}/{document_id}/{revision}/{version}/parsed.json
s3://wre-parsed/canonical/{tenant}/{document_id}/{revision}/{version}/canonical.json
```

## Quality scoring

Six dimensions are stored separately and combined with fixed weights:

| Dimension | Weight | Signal |
|---|---|---|
| text completeness | 0.30 | words vs. a 300-word reference |
| extraction quality | 0.20 | extractor path and warnings |
| metadata completeness | 0.15 | title, date, author, description, language |
| language confidence | 0.15 | langid margin; `und` below 0.6 |
| boilerplate ratio | 0.10 | extracted visible text vs. page text |
| structural quality | 0.10 | lines/paragraphs and headings |

A failed extraction never becomes a successful document: empty, short, or low-scoring
outputs are quarantined and stay out of `core.document_text`.

## API (parser, port 8085)

```bash
GET  /internal/v1/etl/stats
GET  /internal/v1/etl/jobs?status=&document_id=&limit=
GET  /internal/v1/etl/documents/{document_id}
GET  /internal/v1/etl/versions
GET  /internal/v1/etl/quality?document_id=
POST /internal/v1/etl/reprocess
POST /internal/v1/etl/rollback
POST /internal/v1/etl/dispatch
```

Reprocess/backfill body:

```json
{
  "mode": "reparse",
  "source_id": "uuid (optional)",
  "document_ids": ["ulid (optional)"],
  "from": "2026-01-01T00:00:00Z",
  "to": "2026-02-01T00:00:00Z",
  "target_version": "parser-1.0.0",
  "reason": "parser upgrade",
  "requested_by": "operator",
  "limit": 1000
}
```

`mode` is `reparse` or `backfill`; both use the same durable queue. `from`/`to` filter the
revision retrieval timestamp. `target_version` must be the active version because
reprocessing executes the running code. Use `rollback` to activate a previously stored
output:

```json
{"document_id": "...", "revision": 1, "processing_version": "parser-1.0.0"}
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ETL_CONCURRENCY` | 2 | documents processed in parallel per replica |
| `ETL_MIN_WORDS` | 25 | below this the document is quarantined |
| `ETL_MIN_QUALITY` | 0.35 | quality gate |
| `ETL_DISPATCH_INTERVAL_SECONDS` | 5 | dispatcher scan interval |
| `ETL_POLL_INTERVAL_SECONDS` | 1 | worker claim interval |
| `ETL_LEASE_SECONDS` | 300 | job lease before recovery |
| `ETL_MAX_ATTEMPTS` | 3 | infrastructure retry budget |
| `ETL_MAX_TEXT_BYTES` | 2097152 | canonical body cap |

## Scaling and when to add Spark/Flink

The queue is `FOR UPDATE SKIP LOCKED`, so adding parser replicas is safe. This is enough
for per-document extraction and normalization. Introduce Spark only for corpus-wide batch
recomputation or large-scale deduplication, and Flink only for stateful streaming
aggregations that cannot be expressed as replayable jobs. Neither is needed for this
phase.

## Smoke

`make smoke` (or `make alt-smoke`) crawls a large document, waits for `deduplicated`,
checks the canonical artifact and quality score, verifies a short document is quarantined,
and exercises the reprocess queue and quality endpoint.
