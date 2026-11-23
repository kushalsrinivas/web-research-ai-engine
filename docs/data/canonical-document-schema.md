# Canonical Document Schema

Status: Phase 0 baseline. Postgres DDL is normative; language models and API types are derived
from it.

## 1. Identity model

| Concept | Identifier | Mutability |
|---|---|---|
| Document (a canonical URL for a tenant) | `document_id` (ULID) | Never changes |
| Content version | `(document_id, revision)` | New revision when normalized content changes |
| Raw bytes | `content_hash_raw = SHA-256(bytes)` | Immutable |
| Normalized content | `content_hash_norm = SHA-256(canonical JSON)` | Immutable per revision |
| Artifact | content-addressed object key | Immutable |
| Processing run | `(document_id, revision, stage, processor_version, attempt)` | Append-only |

A document is never deleted. Legal takedown sets `documents.tombstoned_at`, removes content
and artifacts, retains identity, events, and a tombstone reason.

## 2. Enumerations

```sql
create schema core;
create extension if not exists vector;     -- pgvector >= 0.7 (halfvec)
create extension if not exists pg_trgm;

create type core.doc_state as enum (
  'discovered',     -- identity recorded, not yet admitted to the frontier
  'scheduled',      -- admitted; robots and politeness passed
  'fetched',        -- HTTP response received and classified
  'raw_archived',   -- immutable raw artifact stored and hash verified
  'parsed',         -- main content extracted
  'normalized',     -- deterministic normalized text produced
  'deduplicated',   -- no canonical duplicate found
  'duplicate',      -- terminal: this URL duplicates another document
  'enriched',       -- summary, chunks, entities, claims, embeddings produced
  'indexed',        -- search index gate complete, graph gate pending
  'kg_updated',     -- graph gate complete, index gate pending
  'available',      -- both gates complete; usable by research
  'failed',         -- terminal for this revision; retryable only by reprocess
  'dead_lettered',  -- attempts exhausted; payload preserved for replay
  'quarantined'     -- policy/safety block; never processed
);

create type core.gate_status as enum ('pending', 'done', 'failed', 'skipped');
create type core.failure_class as enum (
  'transient', 'rate_limited', 'permanent', 'policy', 'dependency', 'budget', 'bug'
);
```

## 3. `core.documents` — identity and current lifecycle

One row per document. Always reflects the **current** revision.

```sql
create table core.documents (
  document_id        text primary key,                      -- ULID, sortable
  tenant_id          text not null default 'public',
  canonical_url      text not null,
  url_hash           bytea not null,                        -- sha256(canonical_url)
  host               text not null,
  current_revision   integer not null default 1 check (current_revision > 0),
  available_revision integer
                     check (available_revision is null or available_revision <= current_revision),
  state              core.doc_state not null default 'discovered',
  duplicate_of       text references core.documents(document_id),
  -- gate statuses refer to current_revision and reset when a new revision starts
  index_status       core.gate_status not null default 'pending',
  kg_status          core.gate_status not null default 'pending',
  attempt            smallint not null default 0,
  failed_stage       text,
  last_error         jsonb,
  trace_id           text,
  first_seen_at      timestamptz not null default now(),
  last_seen_at       timestamptz not null default now(),
  last_fetched_at    timestamptz,
  next_crawl_at      timestamptz,
  crawl_interval_s   integer not null default 604800,
  tombstoned_at      timestamptz,
  tombstone_reason   text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  constraint duplicate_consistency check ((state = 'duplicate') = (duplicate_of is not null)),
  constraint tombstone_consistency check ((tombstoned_at is null) = (tombstone_reason is null)),
  constraint no_self_duplicate check (duplicate_of is null or duplicate_of <> document_id)
);

create unique index documents_tenant_url_hash
  on core.documents (tenant_id, url_hash);
create index documents_state_idx
  on core.documents (state, updated_at) where tombstoned_at is null;
create index documents_due_crawl
  on core.documents (next_crawl_at)
  where available_revision is not null and tombstoned_at is null;
create index documents_host_idx on core.documents (host);
```

`attempt` is the current stage's attempt counter; it resets when the document advances a stage
or starts a new revision. Per-stage history is in `document_events`.

### Read view for hot paths

Search, research, and public reads use the **live revision**, not the revision currently being
processed. This is what keeps a document searchable while a recrawl runs.

```sql
create view core.live_documents as
select d.*, r.content_hash_raw, r.content_hash_norm, r.raw_artifact_uri,
       r.parsed_artifact_uri, r.enriched_artifact_uri, r.http_status,
       r.title, r.language, r.published_at, r.word_count
from core.documents d
left join core.document_revisions r
  on r.document_id = d.document_id and r.revision = d.available_revision;
```

`available_revision` is null until the first revision completes both gates. It remains at N
while revision N+1 is being processed, and moves to N+1 in the promotion transaction. Search
must filter on `available_revision is not null`, never on `state = 'available'`.

## 4. `core.document_revisions` — immutable content facts

```sql
create table core.document_revisions (
  document_id        text not null references core.documents(document_id),
  revision           integer not null check (revision > 0),
  fetched_url        text not null,                         -- URL actually requested
  http_status        smallint,
  content_type       text,
  content_hash_raw   bytea,                                 -- sha256(raw bytes)
  content_hash_norm  bytea,                                 -- sha256(normalized artifact)
  raw_artifact_uri   text,
  parsed_artifact_uri text,
  enriched_artifact_uri text,
  artifact_bytes     bigint,
  title              text,
  language           text,                                  -- BCP-47, best effort
  published_at       timestamptz,                           -- from metadata, untrusted
  author             text,
  word_count         integer,
  renderer_version   text,                                  -- null when static HTML sufficed
  parser_version     text,
  normalizer_version text,
  enrichment_version text,
  superseded_by      integer,
  created_at         timestamptz not null default now(),

  primary key (document_id, revision),
  foreign key (document_id, superseded_by)
    references core.document_revisions(document_id, revision),
  constraint artifact_presence check (
    (content_hash_raw is null) = (raw_artifact_uri is null)
  )
);

create index revisions_hash_norm on core.document_revisions (content_hash_norm);
```

Rules:

- `documents.state` describes `current_revision` only. Searchability is determined by
  `available_revision`, never by `state`.
- A refresh queues work without touching `state`. When content changes, the fetcher inserts
  revision N+1, sets `current_revision = N+1`, resets both gates, and enters at `fetched`.
  `available_revision` stays at N until N+1 completes both gates.
- Rows are insert-only. The only permitted updates are setting `superseded_by` (when the next
  revision becomes available) and the artifact pointers on re-enrichment, each audited.
- A revision is created when normalized content changes; a recrawl with identical content
  updates `documents.last_seen_at` and `next_crawl_at` only.
- `enriched_artifact_uri` is set once; re-enrichment writes a new artifact keyed by
  `processor_version` and updates the pointer with an audit row.

## 5. `core.document_text` — normalized text and lexical index

```sql
create table core.document_text (
  document_id  text not null,
  revision     integer not null,
  title        text not null default '',
  body         text not null,
  word_count   integer not null,
  language     text not null default 'und',
  tsv          tsvector generated always as (
                 setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                 setweight(to_tsvector('simple', coalesce(body, '')), 'B')
               ) stored,
  created_at   timestamptz not null default now(),
  primary key (document_id, revision),
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision)
);

create index document_text_tsv on core.document_text using gin (tsv);
create index document_text_body_trgm on core.document_text using gin (body gin_trgm_ops);
```

Tradeoff, deliberate: `to_tsvector('simple', ...)` is language-agnostic and immutable (required
for a generated column). It loses stemming for non-English corpora. Mitigation: language-
specific stemming is applied at query time for the top supported languages, and a per-language
partial index can be added later without changing the contract. Revisit when non-English
queries exceed 20% of traffic.

Body text is capped at 2 MB per document; documents above the cap are stored truncated and
flagged `documents.last_error = {"code":"TEXT_TRUNCATED"}`. The raw artifact retains the full
content.

## 6. `core.document_enrichment`

```sql
create table core.document_enrichment (
  document_id     text not null,
  revision        integer not null,
  enrichment_version text not null,          -- model+prompt+chunker bundle, e.g. enrich-2026-11
  summary         text,
  key_passages    jsonb not null default '[]',   -- [{start,end,text}]
  topics          text[] not null default '{}',
  entity_count    integer not null default 0,
  claim_count     integer not null default 0,
  chunk_count     integer not null default 0,
  token_cost      numeric(12,4) not null default 0,
  degraded        boolean not null default false,
  created_at      timestamptz not null default now(),
  primary key (document_id, revision, enrichment_version),
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision)
);

create index enrichment_active
  on core.document_enrichment (document_id, revision, created_at desc);
```

Multiple enrichment versions may coexist for a revision (rollout, A/B, degraded fallback).
Readers select the **newest non-degraded version**, falling back to the newest version when all
are degraded. There is deliberately no single "active" pointer: re-enrichment must not make
the previous version unreachable while a rollout is being evaluated.

## 7. `core.document_events` — append-only lifecycle log

```sql
create table core.document_events (
  event_seq        bigserial primary key,      -- append order, not the Kafka event_id
  document_id      text not null,
  revision         integer not null,
  stage            text not null,              -- scheduler|fetcher|parser|enricher|indexer|graph|reconciler
  from_state       core.doc_state,
  to_state         core.doc_state not null,
  occurred_at      timestamptz not null default now(),
  attempt          smallint not null,
  processor        text not null,              -- service name
  processor_version text not null,
  trace_id         text not null,
  artifact_uri     text,                       -- artifact produced/consumed by this stage
  raw_artifact_uri text,                       -- original bytes; null only before raw_archived
  duration_ms      integer,
  error_class      core.failure_class,
  error            jsonb,
  constraint event_has_trace check (length(trace_id) > 0)
);

create index events_document on core.document_events (document_id, occurred_at);
create index events_state_time on core.document_events (to_state, occurred_at);
create index events_stage_time on core.document_events (stage, occurred_at);
create index events_trace on core.document_events (trace_id);
-- No UPDATE/DELETE grants. Retention: forever (see retention policy).
```

Each event is self-contained for audit: once `raw_archived` has occurred, `raw_artifact_uri` is
copied from the revision so the provenance chain survives even if the revision pointer is later
tombstoned. The event's `artifact_uri` points at what that stage produced (parsed, enriched).

Required on every transition, per Phase 0 requirement 0.2: document id, timestamp, attempt,
processing version, failure reason, trace id, raw artifact reference.

## 8. Field-level rules

| Field | Rule |
|---|---|
| `canonical_url` | No fragment, lowercase host, default port removed, tracking params stripped, remaining params sorted |
| `url_hash` | `sha256(canonical_url)`; unique per tenant |
| `host` | Registrable domain plus public suffix handling; used for politeness and circuit breaking |
| `pipeline_version` | Stamped on every derived row as `processor_version`, never inferred |
| `published_at` | Extracted metadata is untrusted; never used for ranking without a `confidence` |
| `language` | `und` when detection confidence < 0.6 |
| `next_crawl_at` | Computed from observed change rate; never null once a revision is live |
| `last_error` | Truncated to 4 KB; full error only in logs/DLQ |

## 9. Retention

| Data | Retention |
|---|---|
| `documents`, `document_revisions`, `document_events` | Indefinite (tombstoned on legal removal) |
| `document_text` | Indefinite while live; removed on tombstone |
| Raw artifacts | 12 months, then delete; derived artifacts 12 months |
| Enrichment rows | Indefinite; superseded versions kept for provenance |
| DLQ payloads | 30 days |

Deleting an artifact must not delete its event rows. A `reconciler` check reports documents
whose raw artifact is gone but whose revision still claims provenance; those are marked
`provenance_degraded` in `documents.last_error` and excluded from citation.
