# ADR-0005: Postgres full-text search first, OpenSearch as a later option

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering, product

## Context

Search is a core product surface, not a utility. Research queries need lexical relevance,
snippets, filters (language, host, date), and hybrid fusion with vector search.

The realistic options:

1. **Postgres FTS** (`tsvector`, GIN, `websearch_to_tsquery`, `ts_rank_cd`, trigram) in the
   same database as documents and chunks.
2. **OpenSearch/Elasticsearch** as a dedicated search engine.

At Phase 1 scale (5M documents, 20M chunks, 50 QPS), both can meet the latency budget.
Postgres gives transactional consistency and one fewer system. OpenSearch gives BM25,
analyzers, highlighting, facets, and horizontal scale.

The decisive detail: hybrid retrieval. With pgvector and FTS in one database, lexical and
semantic candidates can be produced in one transactionally consistent query and fused with
RRF. With OpenSearch, the vector index lives elsewhere (or duplicates pgvector) and every
document update becomes a dual-write with an outbox.

## Decision

Start with **Postgres FTS**, behind a `SearchBackend` interface:

- `simple` text search config, title weight A, body weight B, GIN index.
- `pg_trgm` for fuzzy title/entity recall.
- Query-time stemming for the top supported languages where it improves recall.
- Hybrid = lexical + pgvector, fused with Reciprocal Rank Fusion (k=60) in SQL.
- Snippets generated in SQL (`ts_headline`) with safe HTML escaping.

Build a labeled query set (200 queries with judged results) **before** launch. It is the
regression gate for relevance, regardless of backend.

## Consequences

Positive:

- One store: no dual-write, no index drift, no separate backup/restore, no second auth model.
- Freshness is immediate: a document is searchable in the same transaction that marks it
  available.
- Hybrid ranking is simple and consistent; filters compose naturally with tenant and state.
- Removes an entire operational dependency for a small team.

Negative:

- No true BM25. `ts_rank_cd` is a cover-density rank, not BM25; relevance tuning is coarser.
- Weak analyzers for CJK and morphologically rich languages; stemming requires workarounds.
- Highlighting and faceting are manual SQL, not built-in.
- The primary database absorbs search load; heavy aggregations can hurt ingestion.
- At larger corpora, GIN index size and vacuum cost grow; partitioning may be required.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| OpenSearch from day one | Dual-write consistency, extra cluster, relevance config overhead; premature at 5M docs |
| Typesense/Meilisearch | Great developer experience, but another store and weaker filtering/aggregation for research |
| SQLite FTS5 | No concurrency or HA story for a shared corpus |
| Vector-only search | Loses exact-term recall for names, codes, and legal text; hybrid is required |

## Revisit trigger (any two consecutive weeks)

- Search p95 > 200 ms or p99 > 500 ms after query and index tuning.
- Corpus > 5M documents or > 20M chunks with growing GIN maintenance cost.
- Relevance work requires BM25, custom analyzers, CJK segmentation, or faceted aggregation.
- Search load threatens ingestion write latency (Postgres CPU > 70% at peak).
- The labeled query set shows hybrid Postgres recall@20 below 0.85.

Migration path: introduce an outbox table for document changes, dual-write to OpenSearch,
backfill, compare relevance on the labeled set, then cut reads behind the `SearchBackend`
interface. Postgres remains the system of record.

## Revisit trigger for the opposite direction

None expected. If we adopt OpenSearch, Postgres FTS stays as a fallback for degraded mode,
because search availability should not depend on a second cluster being healthy.
