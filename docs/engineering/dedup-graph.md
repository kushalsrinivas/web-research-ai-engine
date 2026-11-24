# Deduplication, lineage, and entity resolution

Phase 5 is implemented in `graph-builder` (port 8087). Documents are never merged:
clusters, lineage edges, and match decisions are derived records that can be rebuilt.

## Three levels

| Level | Method | Effect |
|---|---|---|
| 1. Exact | SHA-256 of normalized text (ETL) | second document gets `state = duplicate` and `duplicate_of` |
| 2. Near | SimHash prefilter + body shingle Jaccard + title similarity | both documents join one story cluster; relations are recorded |
| 3. Semantic | review-only candidates | never auto-clusters; stored for human/LLM adjudication |

Near-duplicate candidate generation uses `bit_count(simhash # candidate_simhash) <=
DEDUP_SIMHASH_DISTANCE` or pg_trgm title similarity. Candidates are verified in Python with
5-word shingle Jaccard:

- near duplicate: body Jaccard ≥ 0.60, or SimHash distance ≤ 3 and Jaccard ≥ 0.35
- related story: title similarity ≥ 0.50 and body Jaccard ≥ 0.12
- otherwise a new cluster is created

This is deterministic and versioned by `dedup-1.0.0`; thresholds are configuration, not
hidden constants.

## Story clusters

`dedup.story_clusters` holds the cluster; `dedup.story_members` points at each document
(`unique (document_id)`). Members keep their own rows in `core.documents` and
`core.document_text`, so every article stays independently retrievable.

Member relations: `original`, `syndicated_copy`, `repeated_claim`, `independent`,
`follows_up`, `official_statement`. `official_statement` is an origin kind on the member
(government domains and press-release phrasing), while the relation describes the link to
the cluster.

## Lineage and evidence

`dedup.lineage_edges` records:

- `syndicated_copy` / `repeated_claim` for near-identical copies
- `independent` for related but separately written reporting
- `cites` when a document's extracted outbound links resolve to another known document
- `official_statement` for primary-source documents

Evidence is calculated from lineage, not document count:

```
independent_sources = distinct hosts whose relation is not a syndicated/repeated copy
syndicated_copies   = members with syndicated/repeated relation
official_statements = members with official origin
evidence_score      = min(1, 0.30 + 0.25 * independent_sources + 0.10 * official)
inflation_ratio     = document_count / max(1, independent_sources)
```

Ten copies of one press release therefore produce one independent source and an
`inflation_ratio` near 10, not ten confirmations. `original` is the earliest member by
`published_at`, falling back to first-seen time; the field is a lineage convenience, not a
truth claim about the first publisher.

## Entity resolution

`graph.entities` stores canonical entities, display aliases, normalized alias keys,
external IDs (including domain), country, and confidence. `graph.entity_match_candidates`
stores every proposed match with `match_score`, `match_features`, `decision_status`,
`model_version`, and `review_status`.

Normalization: NFKC, casefold, punctuation removal, diacritic folding, and legal suffix
removal for organizations (`Private Limited`, `Pvt Ltd`, `Inc`, `GmbH`, …). Tata
Electronics variants collapse to `tata electronics`.

Scoring and decisions:

| Signal | Score |
|---|---|
| official ID match | 0.99 |
| exact normalized name | 1.00 |
| alias match | 0.98 |
| matching official domain | 0.95 |
| fuzzy name | ≤ 0.94, never auto |

`auto_match` requires ≥ 0.95 **and** a corroborating signal plus a matching entity type.
`review` is 0.78–0.95. Everything else is `keep_separate` (optionally creating a new
entity). Fuzzy similarity alone can never auto-merge, and a name match across types is
queued for review. Accepted reviews attach the surface form as an alias; merges are
recorded in `graph.entity_merges` and remain reversible through `merged_into`.

## API

All endpoints are on `http://127.0.0.1:8087/internal/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/dedup/stats` | clusters, members, lineage, pending reviews |
| POST | `/dedup/scan` | enqueue new documents |
| POST | `/dedup/documents/{id}` | enqueue one document |
| GET | `/dedup/clusters` | list clusters |
| GET | `/dedup/clusters/{id}` | members, lineage, evidence |
| GET | `/dedup/documents/{id}` | cluster membership and lineage |
| GET | `/dedup/evidence?cluster_id=` | evidence independence |
| POST/GET | `/dedup/semantic-candidates` | review-only semantic links |
| GET | `/graph/stats` | entity counters |
| POST/GET | `/graph/entities` | create/list canonical entities |
| GET | `/graph/entities/{id}` | entity, aliases, mentions, merges |
| POST | `/graph/entities/{id}/merge` | operator merge |
| POST | `/graph/resolve` | resolve a name/domain/ID |
| GET | `/graph/candidates` | pending/decided match candidates |
| POST | `/graph/candidates/{id}/review` | accept/reject a candidate |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GRAPH_TENANT_ID` | `public` | tenant scope |
| `DEDUP_CONCURRENCY` | 2 | documents clustered in parallel |
| `DEDUP_SIMHASH_DISTANCE` | 8 | candidate prefilter |
| `DEDUP_TITLE_FLOOR` | 0.35 | title candidate threshold |
| `DEDUP_DISPATCH_INTERVAL_SECONDS` | 5 | dispatcher interval |
| `DEDUP_POLL_INTERVAL_SECONDS` | 1 | worker claim interval |
| `DEDUP_LEASE_SECONDS` | 300 | job lease |
| `DEDUP_MAX_ATTEMPTS` | 3 | infrastructure retry budget |

## Smoke

`make smoke` (or `make alt-smoke`) crawls two URLs with identical content, waits for both
ETL outputs, asserts they share a cluster, checks that evidence counts one independent
source rather than two documents, resolves Tata Electronics aliases, exercises an
ambiguous candidate review, and verifies a semantic candidate stays in `review`.
