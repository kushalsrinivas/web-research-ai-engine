# Knowledge graph and temporal intelligence

Phase 7 projects the authoritative Postgres records into a temporal, source-backed graph.
Documents, claims, events, mentions, and source records remain the source of truth; the
projection is derived and can be rebuilt.

The stack deliberately does not run Neo4j (ADR-0007). `graph.relationships` is the graph
projection; it can be exported to Neo4j later without changing authoritative data.

## Nodes

`graph.nodes` is a view over:

- `entity` — canonical entities (person, organization, government, location, technology,
  industry, project, policy, …)
- `claim` — extracted claims
- `event` — extracted events
- `document` — canonical documents
- `source` — registered sources

Stable IDs come from the authoritative tables; the projection never creates node identity.

## Relationships

Controlled types: `WORKS_FOR`, `OWNS`, `INVESTED_IN`, `LOCATED_IN`, `ANNOUNCED`,
`APPROVED`, `PARTNERED_WITH`, `COMPETES_WITH`, `REGULATES`, `PRODUCES`,
`USES_TECHNOLOGY`, `MENTIONED_IN`, `SUPPORTS`, `CONTRADICTS`, `DERIVED_FROM`,
`RELATED_TO`, `SUPPLIES`, `DELAYED`, `CANCELLED`.

Projection rules:

| Source | Edges |
|---|---|
| mentions | entity `MENTIONED_IN` document |
| claims | claim `DERIVED_FROM` document; document `SUPPORTS` claim; predicate-mapped entity edges (`LOCATED_IN`, `APPROVED`, `OWNS`, `PRODUCES`, `PARTNERED_WITH`, `ANNOUNCED`, `REGULATES`, `RELATED_TO`) |
| events | event `DERIVED_FROM` document; document `SUPPORTS` event; subject/object/location/industry edges; `INVESTED_IN` for investment events; `APPROVED` for policy events |
| dedup lineage | document `DERIVED_FROM` or `RELATED_TO` another document |
| fetcher source | document `DERIVED_FROM` source |
| text heuristics | `SUPPLIES` when a sentence names two organizations and a supplier relationship; `DELAYED`/`CANCELLED` for project status sentences |

No event subtype becomes a node type; events use a controlled taxonomy and share the
`event` node kind.

## Temporal fields

Every relationship stores:

| Field | Meaning |
|---|---|
| `valid_from` / `valid_to` | when the relationship was true in the real world (`valid_to` null = open) |
| `observed_at` | when the platform first observed the supporting evidence |
| `recorded_at` | transaction time of this projection version |
| `source_document_id` | evidence document |
| `source_claim_id` / `source_event_id` | optional originating extraction |
| `confidence` | projection/extraction confidence |
| `extraction_version` | extractor version |
| `evidence` | quote/amount/lineage details |

A cancellation detected in text closes prior `INVESTED_IN`/`ANNOUNCED` relationships at the
cancellation date in the new projection. Older projection versions retain the open edge,
which preserves both valid-time and transaction-time history.

## Projection versions and replay

Each rebuild creates a `graph.projection_versions` row and a `graph.projection_runs` audit
row. Relationships carry `projection_version`.

- Rebuild is a full, deterministic regeneration from authoritative tables.
- A successful rebuild activates the new version and marks the previous one `inactive`.
- Old versions remain queryable; `graph.active_relationships` follows the active version.
- Repeated rebuilds over unchanged data produce the same relationship set.
- The projection loop checks whether claims/events/mentions/entities/lineage changed and
  rebuilds when needed (default 60 seconds), or on `POST /kg/rebuild`.

This is a derived intelligence projection, not the system of record.

## Graph queries

Named queries exposed through `POST /internal/v1/kg/query`:

| Name | Question |
|---|---|
| `investments_by_location_industry` | Which companies are investing in a sector/location? |
| `policies_for_investments` | Which approved policies are associated with investments? |
| `shared_suppliers` | Which companies share suppliers? |
| `delayed_projects` | Which projects were delayed or cancelled? |
| `developments_after_policy` | Which investments occurred after a policy? |
| `claims_multiple_sources` | Which claims have multiple independent hosts? |
| `connected_to_event` | Which nodes are within N hops of an event? |
| `entity_neighborhood` | What is connected to an entity? |
| `event_entities` | Which entities participate in an event? |

Additional endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/kg/stats` | active version, node/relationship counts |
| GET | `/kg/versions` | projection history |
| GET | `/kg/relationships?type=&entity_id=&as_of=` | relationship search with valid-time filter |
| GET | `/kg/entities/{id}/neighborhood?depth=` | recursive neighborhood |
| GET | `/kg/events/{id}/entities` | event participants |
| GET | `/kg/integrity` | duplicate/orphan/dangling checks |
| POST | `/kg/rebuild` | create a new projection version |

## Integrity

`GET /kg/integrity` checks:

- duplicate canonical entities (same tenant/type/normalized name)
- orphan entities with no mentions or relationships
- relationships referencing missing nodes
- unsupported src/dst shapes
- active claims without verified spans
- missing active projection

Errors are surfaced; duplicate/orphan findings are warnings/info. The authoritative tables
are never “fixed” by the integrity check.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `KG_ENABLED` | true | enable the projection loop |
| `KG_REBUILD_INTERVAL_SECONDS` | 60 | dirty-check/rebuild interval |

## Limits

- The projection is a full rebuild baseline; large corpora will need incremental
  relationship deltas keyed by extraction run.
- Supplier and delay/cancellation edges are low-confidence text heuristics.
- Neo4j export is deferred by ADR-0007; the relational projection answers the current
  traversal queries with recursive SQL.
- `claims_multiple_sources` counts distinct hosts after excluding synthetic/near-duplicate
  copies via dedup membership; it is evidence independence, not factual verification.
