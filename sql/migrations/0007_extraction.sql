-- Phase 6: extraction jobs, runs, mentions, events, claims, and graph edges.
-- Derived rows are versioned by run; raw/normalized evidence stays immutable.

create table graph.extraction_versions (
  version      text primary key,
  processor    text not null default 'extractor',
  description  text not null default '',
  config_hash  text not null,
  active       boolean not null default false,
  created_at   timestamptz not null default now()
);

create table graph.extraction_jobs (
  job_id           text primary key,
  document_id      text not null,
  revision         integer not null,
  status           text not null default 'pending'
                     check (status in ('pending', 'running', 'done', 'failed', 'dead_letter')),
  attempts         integer not null default 0,
  max_attempts     integer not null default 3 check (max_attempts between 1 and 10),
  lease_owner      text,
  lease_expires_at timestamptz,
  next_attempt_at  timestamptz not null default now(),
  last_error       text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision) on delete cascade
);
create index extraction_jobs_pending
  on graph.extraction_jobs (status, next_attempt_at, created_at);
create unique index extraction_jobs_active
  on graph.extraction_jobs (document_id)
  where status in ('pending', 'running');

create table graph.extraction_runs (
  run_id             text primary key,
  document_id        text not null,
  revision           integer not null,
  tenant_id          text not null default 'public',
  extraction_version text not null references graph.extraction_versions(version),
  status             text not null default 'running'
                       check (status in ('running', 'done', 'partial', 'failed', 'quarantined')),
  entity_count       integer not null default 0,
  event_count        integer not null default 0,
  claim_count        integer not null default 0,
  quarantined_count  integer not null default 0,
  llm_tokens_in      integer not null default 0,
  llm_tokens_out     integer not null default 0,
  llm_cost_usd       numeric(12,6) not null default 0,
  duration_ms        integer not null default 0,
  error              text,
  created_at         timestamptz not null default now(),
  finished_at        timestamptz,
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision) on delete cascade
);
create index extraction_runs_document
  on graph.extraction_runs (document_id, revision, created_at desc);

create table graph.claims (
  claim_id                    text primary key,
  run_id                      text not null references graph.extraction_runs(run_id) on delete cascade,
  tenant_id                   text not null default 'public',
  document_id                 text not null,
  revision                    integer not null,
  content_hash                bytea not null,
  subject_entity_id           text references graph.entities(entity_id),
  subject_text                text not null,
  predicate                   text not null,
  object_entity_id            text references graph.entities(entity_id),
  object_literal              text,
  object_datatype             text,
  polarity                    text not null default 'asserts'
                                check (polarity in ('asserts', 'denies')),
  claim_type                  text not null,
  claim_text                  text not null,
  quote                       text not null,
  char_start                  integer not null,
  char_end                    integer not null,
  span_verified               boolean not null default false,
  paragraph_index             integer,
  sentence_index              integer,
  published_at                timestamptz,
  modality                    text not null check (modality in (
                                'confirmed', 'announced', 'planned', 'proposed',
                                'expected', 'alleged', 'estimated')),
  commitment_status           text not null check (commitment_status in (
                                'announced', 'planned', 'proposed', 'approved', 'funded',
                                'under_construction', 'operational', 'completed',
                                'expected', 'alleged', 'estimated')),
  temporal_qualifier          text,
  amount                      numeric(24,4),
  currency                    char(3),
  amount_original             text,
  location_text               text,
  claim_key                   text not null,
  normalized_subject          text not null,
  normalized_predicate        text not null,
  normalized_object           text not null,
  extraction_confidence       real not null check (extraction_confidence between 0 and 1),
  entity_resolution_confidence real check (entity_resolution_confidence between 0 and 1),
  claim_support_confidence    real check (claim_support_confidence between 0 and 1),
  verification_confidence     real check (verification_confidence between 0 and 1),
  extractor_version           text not null,
  model_id                    text,
  status                      text not null default 'active'
                                check (status in ('active', 'superseded', 'retracted', 'unverified')),
  created_at                  timestamptz not null default now(),
  constraint claim_object_exactly_one check (
    (object_entity_id is not null)::int + (object_literal is not null)::int = 1
  ),
  constraint claim_span_order check (char_start >= 0 and char_end > char_start),
  constraint claim_quote_present check (length(quote) > 0)
);
create index claims_subject on graph.claims (subject_entity_id, predicate, status);
create index claims_object on graph.claims (object_entity_id, status);
create index claims_document on graph.claims (document_id, revision, status);
create index claims_key on graph.claims (claim_key);
create index claims_quote_trgm on graph.claims using gin (claim_text gin_trgm_ops);
create unique index claims_run_identity
  on graph.claims (run_id, document_id, revision, claim_type, predicate, char_start, char_end);

create table graph.events (
  event_id                    text primary key,
  run_id                      text not null references graph.extraction_runs(run_id) on delete cascade,
  tenant_id                   text not null default 'public',
  document_id                 text not null,
  revision                    integer not null,
  event_type                  text not null,
  subject_entity_id           text references graph.entities(entity_id),
  object_entity_id            text references graph.entities(entity_id),
  location_text               text,
  event_time                  timestamptz,
  event_time_precision        text not null default 'unknown'
                                check (event_time_precision in ('year', 'month', 'day', 'unknown')),
  discovered_at               timestamptz not null default now(),
  amount                      numeric(24,4),
  currency                    char(3),
  amount_original             text,
  commitment_status           text not null,
  modality                    text not null,
  description                 text not null,
  quote                       text not null,
  char_start                  integer not null,
  char_end                    integer not null,
  paragraph_index             integer,
  sentence_index              integer,
  confidence                  real not null check (confidence between 0 and 1),
  extraction_confidence       real not null check (extraction_confidence between 0 and 1),
  event_classification_confidence real not null check (event_classification_confidence between 0 and 1),
  entity_resolution_confidence real,
  verification_confidence     real,
  evidence_document_ids       text[] not null default '{}',
  extractor_version           text not null,
  model_id                    text,
  created_at                  timestamptz not null default now(),
  constraint event_span_order check (char_start >= 0 and char_end > char_start),
  constraint event_quote_present check (length(quote) > 0)
);
create index events_type on graph.events (event_type, discovered_at desc);
create index events_subject on graph.events (subject_entity_id, discovered_at desc);
create index events_document on graph.events (document_id, revision);
create unique index events_run_identity
  on graph.events (run_id, event_type, char_start, char_end);

create table graph.extraction_quarantine (
  quarantine_id     text primary key,
  run_id            text references graph.extraction_runs(run_id) on delete cascade,
  document_id       text not null,
  revision          integer not null,
  stage             text not null,
  object_type       text not null,
  reason            text not null,
  payload           jsonb not null default '{}'::jsonb,
  extraction_version text not null,
  created_at        timestamptz not null default now()
);
create index extraction_quarantine_pending
  on graph.extraction_quarantine (created_at desc);

create table graph.edges (
  edge_id           bigserial primary key,
  tenant_id         text not null default 'public',
  edge_type         text not null check (edge_type in (
                      'mentions', 'asserts', 'about', 'same_as', 'cites',
                      'authored_by', 'co_occurs', 'related_to')),
  src_kind          text not null check (src_kind in ('document', 'entity', 'claim')),
  src_id            text not null,
  dst_kind          text not null check (dst_kind in ('document', 'entity', 'claim')),
  dst_id            text not null,
  document_id       text,
  revision          integer,
  claim_id          text,
  weight            real not null default 1.0,
  extractor_version text not null,
  created_at        timestamptz not null default now(),
  constraint edge_no_self check (not (src_kind = dst_kind and src_id = dst_id))
);
create index edges_out on graph.edges (src_kind, src_id, edge_type);
create index edges_in on graph.edges (dst_kind, dst_id, edge_type);
create index edges_document on graph.edges (document_id, revision);
create unique index edges_identity on graph.edges (
  edge_type, src_kind, src_id, dst_kind, dst_id,
  coalesce(document_id, ''), coalesce(revision, 0)
);

create view graph.live_claims as
select c.*
from graph.claims c
where c.run_id = (
  select r.run_id from graph.extraction_runs r
  where r.document_id = c.document_id and r.revision = c.revision
    and r.status in ('done', 'partial')
  order by r.created_at desc limit 1
);

create view graph.live_events as
select e.*
from graph.events e
where e.run_id = (
  select r.run_id from graph.extraction_runs r
  where r.document_id = e.document_id and r.revision = e.revision
    and r.status in ('done', 'partial')
  order by r.created_at desc limit 1
);

create trigger extraction_jobs_touch_updated_at
  before update on graph.extraction_jobs
  for each row execute function ingest.touch_updated_at();
