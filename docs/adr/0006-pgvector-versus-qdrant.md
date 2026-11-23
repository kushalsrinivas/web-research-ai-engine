# ADR-0006: pgvector for embeddings, Qdrant as the escape hatch

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering

## Context

Research retrieval needs semantic search over chunks (≈ 20M at 5M documents), filtered by
tenant, language, date, and host, and fused with lexical results.

Requirements:

- Recall@20 ≥ 0.90 on the labeled query set.
- p95 ≤ 200 ms for hybrid queries including filters.
- Consistency with document state: a document must not be searchable before it is available,
  and a deleted document must not surface.
- Rebuildable embeddings when the model changes.
- One team able to operate it.

Candidates: pgvector in the existing Postgres, or a dedicated vector database (Qdrant).

## Decision

Use **pgvector** (`vector`/`halfvec`, HNSW, cosine) in the same Postgres instance.

- Embeddings are `halfvec(1024)`; model `bge-m3` initially, versioned per row.
- Chunks are stored in `search.chunks` with `embedding_model` and `extractor_version`.
- HNSW index with `m=16`, `ef_construction=64`, query `ef_search` tuned per workload
  (default 100; 200 for research).
- Filtered queries use partial indexes for high-selectivity filters (`tenant_id`, `language`)
  and iterative scans (pgvector ≥ 0.8) to preserve recall under filters.
- Inference runs locally on one GPU for corpus embeddings; the gateway handles provider
  embeddings behind the same interface when enabled.

## Consequences

Positive:

- One transactional store: chunk rows, document state, and lexical index agree.
- Deletes and tombstones are ordinary transactions; no vector-index drift.
- Hybrid RRF is one SQL query; no cross-store latency or consistency window.
- Backup, auth, monitoring, and failover reuse the Postgres investment.

Negative:

- HNSW index build is memory-hungry and blocks writes during creation; built with
  `maintenance_work_mem` tuned and `CREATE INDEX CONCURRENTLY` where possible.
- Filtered ANN recall is the weak spot; mitigated by partial indexes and iterative scans, but
  it is a real tuning burden.
- Vertical scaling limits: one primary writes vectors; read replicas help queries but not
  write throughput.
- Model changes require a full re-embed; with 20M chunks this is a multi-day GPU job that must
  run behind a version column and dual-read.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| Qdrant | Better filtered search, quantization, multi-tenancy, and horizontal scale — but a second store, dual-write, and a consistency window |
| Weaviate/Milvus | Same dual-write cost, heavier operations |
| FAISS in-process | No durability or filtered search; rebuild on every deploy |
| Elasticsearch kNN | Search engine plus vectors, but still a second store and weaker transactional semantics |
| Pinecone/managed | Data egress of corpus embeddings, recurring cost, vendor lock at the retrieval core |

## Revisit trigger

- > 25M chunks, or HNSW build time exceeds a maintenance window.
- Filtered recall < 0.90 at the target `ef_search`, or filtered p95 > 200 ms after tuning.
- Vector writes compete with ingestion writes (Postgres CPU > 70% at peak).
- Multi-tenant isolation requires per-collection separation or per-tenant quotas that
  pgvector cannot express cleanly.
- Quantization (binary/int8) becomes necessary to fit the index in memory.

Migration path: embeddings are already external to the text; dual-write vectors to Qdrant,
backfill from `search.chunks`, compare recall on the labeled set, then cut reads behind the
retrieval interface. Postgres keeps the canonical chunk text and provenance.

## Note on model changes

Changing the embedding model is a **data migration**, not a config change:

1. Add `embedding_model_version` to the new rows.
2. Backfill in bulk lane, oldest-active first.
3. Dual-read: serve old vectors until coverage > 95%, then switch.
4. Keep the old index for one release for rollback.
