# Architecture Decision Records

Format: [Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).
Each ADR states context, decision, consequences, alternatives, and a **revisit trigger**.
An accepted ADR is not reopened by opinion; it is reopened by its trigger or a new ADR that
supersedes it.

## Index

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-go-for-crawling-and-api.md) | Go for crawling and API infrastructure | Accepted |
| [0002](0002-python-for-etl-and-agents.md) | Python for ETL and agent orchestration | Accepted |
| [0003](0003-redpanda-versus-kafka.md) | Redpanda as the Kafka-compatible event log | Accepted |
| [0004](0004-object-storage-versus-postgres.md) | Object storage for artifacts, Postgres for metadata | Accepted |
| [0005](0005-postgres-fts-versus-opensearch.md) | Postgres FTS first, OpenSearch as a revisit | Accepted |
| [0006](0006-pgvector-versus-qdrant.md) | pgvector for embeddings, Qdrant as escape hatch | Accepted |
| [0007](0007-relational-graph-versus-neo4j.md) | Relational graph before a graph database | Accepted |
| [0008](0008-temporal-versus-app-managed-workflows.md) | Temporal for research workflows only | Accepted |
| [0009](0009-llm-provider-abstraction.md) | LLM abstraction inside the gateway | Accepted |
| [0010](0010-provenance-and-versioning.md) | Immutable artifacts, revisions, append-only events | Accepted |

## Consequences that need active monitoring

These decisions are cheap to reverse only if their trigger is watched. Service ownership is
defined in `docs/architecture/service-boundaries.md` §1.1.

| ADR | Trigger metric | Watch in |
|---|---|---|
| 0003 | Ops hours, broker incidents | Ops review |
| 0005 | Search p95, corpus size, relevance set | Search dashboard |
| 0006 | Chunk count, filtered recall, p95 | Search dashboard |
| 0007 | Traversal p95, edge count | Graph dashboard |
| 0009 | LLM cost per doc/investigation, gateway maintenance | Cost dashboard |

## Adding an ADR

1. Copy the structure: Status, Date, Deciders, Context, Decision, Consequences, Alternatives,
   Revisit trigger.
2. Number sequentially, never reuse a number.
3. If it supersedes an ADR, set the old one to `Superseded by ADR-XXXX` and keep it.
4. Link the ADR from the document it changes (`system-overview.md`, a schema doc, or a
   service section).
