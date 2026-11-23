# ADR-0004: Object storage for artifacts, Postgres for metadata

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering

## Context

Documents arrive as raw HTML (average 150 KB, frequently 1–5 MB, PDFs up to 50 MB). For each
revision we keep:

- the raw bytes exactly as fetched (legal and provenance value),
- a parsed artifact,
- an enriched artifact (summary, chunks, entities, claims, embeddings).

Postgres could store these in `bytea` or large objects, but:

- TOAST compresses poorly on mixed HTML and bloats backups and WAL.
- A 5M-document corpus at 150 KB raw is ~750 GB before derived artifacts; this dwarfs the
  queryable data.
- Re-parsing and re-enrichment should not require refetching the web, so artifacts must be
  durable and directly addressable.
- Immutability and content addressing are easier to enforce in a bucket than in a mutable
  table.

## Decision

Store **raw and derived artifacts in S3-compatible object storage**, and store **metadata,
lifecycle state, text needed for search, and references** in Postgres.

Layout and guarantees:

- Keys are content-addressed (`raw/{tenant}/{sha256}.html`) and immutable; overwrite is denied
  by bucket policy. Re-uploading identical bytes is a no-op.
- Every artifact row records `sha256`, `bytes`, and `codec`; the fetcher verifies the hash
  before advancing state.
- Postgres stores `raw_artifact_uri`, `parsed_artifact_uri`, `enriched_artifact_uri` on
  `core.document_revisions`.
- Normalized body text **is** stored in Postgres (`core.document_text`) because it powers FTS
  and claim span verification. This is deliberate duplication of the parsed artifact; the
  artifact is the immutable source, the table is the queryable projection.

## Consequences

Positive:

- Postgres stays small (≈ 400 GB for 5M docs) and can be backed up and restored quickly.
- Raw artifacts survive extractor changes and enable `reparse`/`reenrich` without network
  access.
- Lifecycle policies (infrequent access after 90 days, delete after 12 months) cut storage
  cost without touching queryable data.
- Content addressing gives natural dedupe and tamper evidence.

Negative:

- One more system to operate, secure, and pay for.
- Two-phase write: artifact first, then DB. Orphan artifacts are possible and must be
  reconciled (we list, never auto-delete within 7 days).
- Signed URLs and access control are needed for any artifact read; artifacts are never public.
- Read-after-write consistency must be assumed strong (S3 and MinIO provide it); if a provider
  does not, the fetcher verifies with a HEAD before advancing state.

## Alternatives considered

| Alternative | Why not |
|---|---|
| `bytea` in Postgres | WAL/backup bloat, poor compression, large rows hurt vacuum and replication |
| Postgres large objects | Same storage problems, awkward streaming, tooling friction |
| Local filesystem / NFS | No multi-node durability, backup and scaling burden, no lifecycle policies |
| A document database (MongoDB) | Reintroduces a second query engine without solving blob storage |
| Store only text, discard raw HTML | Loses provenance and the ability to re-parse; unacceptable for the product promise |

## Revisit trigger

- Artifact serving becomes a latency bottleneck for the API (would add a CDN in front, not
  change the store).
- Storage cost exceeds the budget after lifecycle policies → reduce raw retention or move to
  colder tiers.
- A regulatory requirement forbids retaining raw copies → keep hashes and text only, mark
  `provenance_degraded`, and document the tradeoff.
