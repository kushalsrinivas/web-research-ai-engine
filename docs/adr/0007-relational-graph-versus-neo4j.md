# ADR-0007: Relational graph representation before a graph database

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering, product

## Context

The knowledge graph links documents, entities, and claims. Product queries are:

- "What do we know about X?" — entity + claims + top neighbors.
- "How is X connected to Y within 2–3 hops?"
- "Which documents support this claim?"
- "Where do sources disagree?"

All results must carry provenance (document, revision, span). The graph is written
transactionally with claims, and must be rebuildable from enriched artifacts.

Expected Phase 1 size: 5M documents, ~2M entities, 40M claims, < 100M edges.

## Decision

Represent the graph **relationally in Postgres**:

- Nodes are typed references (`document`, `entity`, `claim`), not duplicated rows.
- Edges are rows in `graph.edges` with `src_kind/src_id`, `dst_kind/dst_id`, type, weight,
  and provenance (`document_id`, `revision`, `claim_id`).
- Traversal uses recursive CTEs with depth cap 3, node cap 500, cycle guard, and a 3 s
  statement timeout.
- Degree statistics are maintained in `graph.entity_stats` to avoid hub explosions.

Schema details: `docs/data/graph-schema.md`.

## Consequences

Positive:

- Entity merges, claim writes, and edge updates commit atomically. A merge cannot leave a
  dangling edge visible to readers.
- Provenance is a foreign key, not a property bag that can drift.
- One backup, one restore drill, one access-control model.
- Query plans are inspectable and indexable; no new query language to learn.
- The edge table doubles as the export format if we ever migrate.

Negative:

- Deep or variable-length path queries become expensive and awkward to express.
- No built-in graph algorithms (centrality, community detection, weighted shortest path).
- Recursive CTEs are easy to write badly; must be wrapped in a repository with hard caps.
- Supernodes (e.g., "United States") require degree caps and pre-aggregation.
- At > 100M edges, index maintenance and vacuum become a real cost.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| Neo4j | Best-in-class traversal and algorithms, but a second store, dual-write, licensing (GPLv3 community / commercial), and provenance joins leave the database anyway |
| Apache AGE | Graph queries in Postgres, but a younger extension with operational risk and a Cypher dialect to maintain |
| Amazon Neptune / JanusGraph | Managed/scale-oriented, heavy for Phase 1, vendor lock |
| RDF triple store | Correct for ontologies, poor fit for provenance-heavy text evidence and our query patterns |
| Materialized adjacency cache (Redis) | Fast reads, but another consistency surface and no durability story |

## Revisit trigger (two consecutive weeks)

- p95 traversal latency > 300 ms after index and query tuning.
- > 30% of research queries need depth ≥ 4 or graph algorithms.
- Active edges > 100M, or `graph.edges` exceeds 100 GB.
- Entity resolution requires fuzzy match at write time that Postgres cannot serve within
  budget (would push toward a dedicated entity store, not necessarily Neo4j).

Migration path: `graph.edges` is the canonical edge list plus typed node references. Export to
Neo4j, dual-write during a validation window, compare traversal results on a fixed query set,
then cut reads. Claims remain in Postgres as the evidence system of record.

## Guardrail

The public API never exposes arbitrary Cypher/SQL. Traversal is a repository method with
fixed depth, node, and edge-type limits. This keeps a future migration from leaking query
semantics into client code.
