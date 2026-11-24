-- Temporal, source-backed graph projection. Authoritative data stays in
-- core/graph/processing; this schema is derived and rebuildable.

create table graph.projection_versions (
  version      text primary key,
  status       text not null default 'building'
                 check (status in ('building', 'active', 'inactive', 'failed')),
  description  text not null default '',
  created_at   timestamptz not null default now(),
  activated_at timestamptz
);
create index projection_versions_active
  on graph.projection_versions (created_at desc) where status = 'active';

create table graph.projection_runs (
  run_id             text primary key,
  projection_version text not null references graph.projection_versions(version) on delete cascade,
  status             text not null default 'running'
                       check (status in ('running', 'done', 'failed')),
  relationship_count integer not null default 0,
  node_count         integer not null default 0,
  duration_ms        integer not null default 0,
  error              text,
  created_at         timestamptz not null default now(),
  finished_at        timestamptz
);

create table graph.relationships (
  relationship_id   text primary key,
  projection_version text not null references graph.projection_versions(version) on delete cascade,
  tenant_id         text not null default 'public',
  relationship_type text not null check (relationship_type in (
                      'WORKS_FOR', 'OWNS', 'INVESTED_IN', 'LOCATED_IN', 'ANNOUNCED',
                      'APPROVED', 'PARTNERED_WITH', 'COMPETES_WITH', 'REGULATES',
                      'PRODUCES', 'USES_TECHNOLOGY', 'MENTIONED_IN', 'SUPPORTS',
                      'CONTRADICTS', 'DERIVED_FROM', 'RELATED_TO', 'SUPPLIES',
                      'DELAYED', 'CANCELLED')),
  src_kind          text not null check (src_kind in ('entity', 'document', 'claim', 'event', 'source')),
  src_id            text not null,
  dst_kind          text not null check (dst_kind in ('entity', 'document', 'claim', 'event', 'source')),
  dst_id            text not null,
  valid_from        timestamptz not null default now(),
  valid_to          timestamptz,
  observed_at       timestamptz not null default now(),
  recorded_at       timestamptz not null default now(),
  source_document_id text,
  source_claim_id   text,
  source_event_id   text,
  confidence        real not null default 0.5 check (confidence between 0 and 1),
  extraction_version text not null,
  evidence          jsonb not null default '{}'::jsonb,
  constraint relationship_validity check (valid_to is null or valid_to >= valid_from),
  constraint relationship_no_self check (
    not (src_kind = dst_kind and src_id = dst_id)
  )
);
create unique index relationships_identity on graph.relationships (
  projection_version, relationship_type, src_kind, src_id, dst_kind, dst_id,
  coalesce(source_document_id, ''), valid_from
);
create index relationships_src on graph.relationships (src_kind, src_id, relationship_type);
create index relationships_dst on graph.relationships (dst_kind, dst_id, relationship_type);
create index relationships_type on graph.relationships (relationship_type, valid_from);
create index relationships_source on graph.relationships (source_document_id);

create view graph.nodes as
select 'entity'::text as kind, entity_id as id, tenant_id,
       canonical_name as label, entity_type as subtype, updated_at as changed_at
from graph.entities where merged_into is null
union all
select 'claim', claim_id, tenant_id, left(claim_text, 200), claim_type, created_at
from graph.claims
union all
select 'event', event_id, tenant_id, left(description, 200), event_type, created_at
from graph.events
union all
select 'document', document_id, tenant_id, canonical_url, state::text, updated_at
from core.documents
union all
select 'source', id::text, 'public', name, source_type, updated_at
from ingest.sources;

create view graph.active_projection as
select * from graph.projection_versions
where status = 'active'
order by created_at desc limit 1;

create view graph.active_relationships as
select r.*
from graph.relationships r
join graph.active_projection p on p.version = r.projection_version;

create table graph.integrity_issues (
  issue_id          text primary key,
  projection_version text,
  kind              text not null,
  severity          text not null check (severity in ('info', 'warning', 'error')),
  details           jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  resolved_at       timestamptz
);
create index integrity_issues_open
  on graph.integrity_issues (severity, created_at desc) where resolved_at is null;
