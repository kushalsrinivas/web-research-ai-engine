# Entity Schema

Status: Phase 0 baseline. Owned by `graph-builder`; read by `api` and `research-orchestrator`.

## 1. Model

An entity is a resolved real-world thing (person, organization, product, place, event, work,
concept). Text mentions are not entities; they are spans that point at an entity after
resolution.

- Identity is `entity_id` (ULID), never a name.
- Names are labels, and many labels can point at one entity.
- Merges are append-only and reversible by reading `merged_into` chains. Nothing is deleted.
- Entities are tenant-scoped; the same name in two tenants is two entities unless a shared
  public graph is explicitly enabled (off in Phase 1).

## 2. DDL

```sql
create schema graph;

create type graph.entity_type as enum (
  'person', 'organization', 'product', 'place', 'event', 'work', 'concept', 'other'
);

create table graph.entities (
  entity_id      text primary key,                     -- ULID
  tenant_id      text not null default 'public',
  entity_type    graph.entity_type not null,
  canonical_name text not null,                        -- display form, original casing
  normalized_name text not null,                       -- see normalization rules
  aliases        text[] not null default '{}',
  description    text,                                 -- one sentence, may be LLM-written
  external_ids   jsonb not null default '{}'::jsonb,   -- {"wikidata":"Q42","domain":"example.org"}
  embedding      halfvec(1024),                        -- name + description + top aliases
  embedding_model text,
  confidence     real not null default 0.5
                 check (confidence >= 0 and confidence <= 1),
  merged_into    text references graph.entities(entity_id),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  constraint no_self_merge check (merged_into is null or merged_into <> entity_id)
);

create unique index entities_canonical
  on graph.entities (tenant_id, entity_type, normalized_name)
  where merged_into is null;

create index entities_aliases on graph.entities using gin (aliases);
create index entities_external_ids on graph.entities using gin (external_ids jsonb_path_ops);
create index entities_embedding on graph.entities
  using hnsw (embedding halfvec_cosine_ops);
```

```sql
create table graph.entity_mentions (
  mention_id     bigserial primary key,
  entity_id      text not null references graph.entities(entity_id),
  document_id    text not null,
  revision       integer not null,
  surface_form   text not null,                        -- exactly as it appears
  char_start     integer not null,
  char_end       integer not null,
  confidence     real not null,
  extractor_version text not null,
  created_at     timestamptz not null default now(),
  constraint mention_span check (char_start >= 0 and char_end > char_start),
  unique (document_id, revision, char_start, char_end, entity_id)
);

create index mentions_entity on graph.entity_mentions (entity_id, document_id);
create index mentions_document on graph.entity_mentions (document_id, revision);
```

```sql
create table graph.entity_merges (
  merge_id       bigserial primary key,
  from_entity_id text not null references graph.entities(entity_id),
  into_entity_id text not null references graph.entities(entity_id),
  method         text not null,        -- 'exact' | 'alias' | 'embedding' | 'llm' | 'operator'
  confidence     real not null,
  evidence       jsonb not null default '{}'::jsonb,
  performed_by   text not null,        -- service@version or operator id
  created_at     timestamptz not null default now(),
  constraint merge_distinct check (from_entity_id <> into_entity_id)
);
```

## 3. Normalization rules (deterministic, versioned)

Applied to `canonical_name` and every alias to produce `normalized_name` / lookup keys:

1. Unicode NFKC.
2. Case fold (not just lowercase).
3. Collapse whitespace, trim.
4. Strip leading/trailing punctuation and quotation marks.
5. Remove a trailing legal suffix only for `organization` (`inc`, `ltd`, `gmbh`, `llc`).
6. Keep internal punctuation (`AT&T`, `Yahoo!`) but also index a punctuation-stripped variant
   as an alias.
7. Transliterate diacritics for the lookup key; keep the original for display.

`extractor_version` is stored on mentions; a normalizer change triggers `regraph`, not a
rewrite of existing rows.

## 4. Resolution pipeline

Ordered, cheapest first. Each step may return `candidate` or `no_match`.

| Step | Condition | Result |
|---|---|---|
| 1. External ID | Same `external_ids` value (Wikidata QID, official domain) | Merge |
| 2. Exact | Same type + `normalized_name` | Merge |
| 3. Alias | Same type + alias equal to normalized name | Merge |
| 4. Embedding | Same type + cosine ≥ 0.92 | Merge |
| 5. Ambiguous | Same type + cosine 0.86–0.92 | LLM adjudication with top 3 candidates and the mention context |
| 6. New | None of the above | Create entity with `confidence` from extractor |

LLM adjudication rules:

- Prompt contains only candidate names/descriptions and the sentence around the mention.
- Response must be one of the candidate ids or `NEW`. Anything else is treated as `NEW`.
- Every merge is recorded in `entity_merges` with the model id and confidence.
- Merges below 0.95 confidence are queued for operator review, not auto-applied, when the
  entity has degree > 50.

Merging a duplicate: set `merged_into` on the loser, rewrite `entity_mentions.entity_id` and
edges in the same transaction, keep the loser row. Reads resolve the chain to the root.

## 5. Reads

Canonical read always resolves merges:

```sql
with recursive root as (
  select entity_id, merged_into from graph.entities where entity_id = $1
  union all
  select e.entity_id, e.merged_into
  from graph.entities e join root r on e.entity_id = r.merged_into
)
select entity_id from root where merged_into is null;
```

API responses include `aliases`, `entity_type`, `description`, `external_ids`, and a
`merged_from` list when relevant. They never expose raw embeddings.

## 6. Constraints

| Constraint | Reason |
|---|---|
| One canonical entity per `(tenant, type, normalized_name)` among unmerged rows | Prevent resolution forks |
| Aliases unique within an entity | Clean display |
| No cross-type merges | "Apple" the company is not "apple" the fruit |
| Mentions must reference an existing revision | Provenance |
| Merge rows are immutable | Audit trail |

## 7. Revisit triggers

- Entity count > 5M or resolution throughput < 200 mentions/s → dedicated entity store.
- Cross-tenant shared graph becomes a product requirement → add `graph_scope` and partition
  by scope, not by tenant.
- Merge error rate > 1% sampled → move adjudication to a reviewed queue.
