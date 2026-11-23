# Graph Schema

Status: Phase 0 baseline. Relational representation in Postgres, owned by `graph-builder`.
Rationale and migration trigger: ADR-0007.

## 1. Representation

Nodes are not duplicated into a single table. A node is a typed reference to an existing
record:

| Node kind | Table | Identity |
|---|---|---|
| `document` | `core.documents` | `document_id` |
| `entity` | `graph.entities` | `entity_id` |
| `claim` | `graph.claims` | `claim_id` |
| `span` | `core.document_text` | `(document_id, revision, char_start, char_end)` — not an edge endpoint in v1 |

Edges are explicit rows. This keeps one source of truth per node and makes provenance a
property of the edge.

## 2. DDL

```sql
create schema graph;

create type graph.node_kind as enum ('document', 'entity', 'claim');
create type graph.edge_type as enum (
  'mentions',     -- document  -> entity   (surface mention exists)
  'asserts',      -- document  -> claim    (claim extracted from document)
  'about',        -- claim     -> entity   (resolved argument)
  'same_as',      -- entity    -> entity   (merged identity)
  'cites',        -- document  -> document (outbound citation found)
  'authored_by',  -- document  -> entity   (author)
  'co_occurs',    -- entity    -> entity   (derived, weighted)
  'related_to'    -- entity    -> entity   (extracted weak relation)
);

create table graph.edges (
  edge_id           bigserial primary key,
  tenant_id         text not null default 'public',
  edge_type         graph.edge_type not null,
  src_kind          graph.node_kind not null,
  src_id            text not null,
  dst_kind          graph.node_kind not null,
  dst_id            text not null,
  document_id       text,          -- provenance document, required for document-derived edges
  revision          integer,
  claim_id          text references graph.claims(claim_id),
  weight            real not null default 1.0,
  extractor_version text not null,
  created_at        timestamptz not null default now(),

  constraint no_self_edge check (not (src_kind = dst_kind and src_id = dst_id))
);

create index edges_out on graph.edges (src_kind, src_id, edge_type);
create index edges_in on graph.edges (dst_kind, dst_id, edge_type);
create index edges_document on graph.edges (document_id, revision);
create index edges_claim on graph.edges (claim_id) where claim_id is not null;
```

`unique` cannot include nullable `document_id`, so identity is a unique expression index:

```sql
create unique index edges_identity on graph.edges (
  edge_type, src_kind, src_id, dst_kind, dst_id,
  coalesce(document_id, ''), coalesce(revision, 0)
);
```

Allowed node-kind shapes per edge type (e.g. `mentions` must be `document → entity`) are
enforced in `graph-builder`, not by a database check. A malformed shape is a bug, and the
nightly integrity job reports it.

## 3. Derivation rules

| Edge | Created from | Rebuildable |
|---|---|---|
| `mentions` | `entity_mentions` rows | yes, from enriched artifact |
| `asserts` | `claims` rows | yes |
| `about` | claim subject/object | yes |
| `same_as` | `entities.merged_into` | yes |
| `cites` | parser-extracted outbound links that resolve to a known document | yes |
| `authored_by` | document metadata with confidence ≥ 0.8 | yes |
| `co_occurs` | entities in the same sentence/paragraph; weight = count | yes, expensive |
| `related_to` | extractor output with confidence ≥ 0.6 | yes |

Edges are **derived** and may be deleted/rewritten for a document revision. Claims and
entities are **evidence** and are never deleted except by tombstone/retraction.

Rebuild procedure for one revision:

```sql
begin;
delete from graph.edges where document_id = $1 and revision = $2;
-- reinsert from enriched artifact
commit;
```

## 4. Traversal patterns

Neighborhood (depth ≤ 3, capped):

```sql
with recursive n(node_kind, node_id, depth, path) as (
  select $1::graph.node_kind, $2::text, 0, array[$2::text]
  union all
  select e.dst_kind, e.dst_id, n.depth + 1, n.path || e.dst_id
  from graph.edges e
  join n on e.src_kind = n.node_kind and e.src_id = n.node_id
  where n.depth < $3
    and not (e.dst_id = any(n.path))          -- cycle guard
    and e.edge_type = any($4::graph.edge_type[])
)
select * from n where depth > 0;
```

Limits enforced by the repository layer, not by callers:

| Limit | Value |
|---|---|
| Max depth | 3 (4 only for operator tools) |
| Max nodes returned | 500 |
| Statement timeout | 3 s |
| Edge types allowed from the public API | `mentions`, `cites`, `same_as`, `about`, `related_to` |

`co_occurs` edges are excluded from public traversal unless explicitly requested; they are
high-degree and low signal.

## 5. Degree and statistics

Traversal needs degree caps to avoid hubs (e.g., "United States" has 100k edges).

```sql
create table graph.entity_stats (
  entity_id    text primary key references graph.entities(entity_id),
  doc_count    integer not null default 0,
  claim_count  integer not null default 0,
  in_degree    integer not null default 0,
  out_degree   integer not null default 0,
  updated_at   timestamptz not null default now()
);
```

Updated incrementally by `graph-builder` in the same transaction as edge writes, using
`insert ... on conflict do update`. The public API returns at most 50 neighbors per entity
ordered by edge weight, and states when results were truncated.

## 6. Query examples the schema must serve

| Product question | Query shape |
|---|---|
| What do we know about entity X? | entity + active claims by subject + top neighbors |
| Who is connected to X within 2 hops? | recursive CTE over `same_as`/`related_to` |
| Which documents support claim C? | `asserts` + `supports` claim relations |
| Which sources disagree about X? | contradiction view in `claim-schema.md` §5 |
| What does document D cite? | `cites` edges by `document_id` |
| How did this citation chain arise? | walk `document → claim → entity → document` edges |

## 7. Consistency and repair

- Graph writes are idempotent upserts on `edges_identity`; replay is safe.
- A nightly job samples 1% of `available` documents and checks that `mentions` and `asserts`
  edges match `entity_mentions` and `claims`. Mismatch → rebuild that revision.
- Deleting a document (tombstone) deletes its derived edges and marks claims `retracted`; it
  does not delete entities, which may be referenced by other documents.
- Entity merges create/remove edges; the merge transaction resolves chains first so no edge
  points at a merged-away entity in public reads.

## 8. Why not a graph database yet

Phase 1 queries are bounded-depth, single-tenant, and provenance-heavy. Postgres gives
transactional consistency with `claims`/`entities`, one backup story, and one failure mode.
Recursive CTEs at depth ≤ 3 over < 50M edges are within the performance budget.

Migrate to Neo4j (or Apache AGE) when any of these hold for two consecutive weeks:

- p95 traversal latency > 300 ms after index and query tuning.
- More than 30% of research queries need depth ≥ 4 or path algorithms (centrality, community,
  shortest path weighted by claim confidence).
- Active edges > 100M.
- A requirement for graph algorithms that would otherwise be exported nightly.

Migration path: `graph.edges` is already the canonical edge list; export it plus nodes to
Neo4j, dual-write during a validation window, then cut reads. Claims remain in Postgres as the
system of record for evidence.
