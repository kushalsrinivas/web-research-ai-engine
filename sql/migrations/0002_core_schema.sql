-- rollback: forward-fix only. core is append-only provenance state; dropping it
-- is a data-loss event and needs a separate approved plan.

create type core.doc_state as enum (
  'discovered',
  'scheduled',
  'fetched',
  'raw_archived',
  'parsed',
  'normalized',
  'deduplicated',
  'duplicate',
  'enriched',
  'indexed',
  'kg_updated',
  'available',
  'failed',
  'dead_lettered',
  'quarantined'
);

create type core.gate_status as enum ('pending', 'done', 'failed', 'skipped');

create type core.failure_class as enum (
  'transient', 'rate_limited', 'permanent', 'policy', 'dependency', 'budget', 'bug'
);

create table core.documents (
  document_id        text primary key,
  tenant_id          text not null default 'public',
  canonical_url      text not null,
  url_hash           bytea not null,
  host               text not null,
  current_revision   integer not null default 1 check (current_revision > 0),
  available_revision integer
                     check (available_revision is null or available_revision <= current_revision),
  state              core.doc_state not null default 'discovered',
  duplicate_of       text references core.documents(document_id),
  index_status       core.gate_status not null default 'pending',
  kg_status          core.gate_status not null default 'pending',
  attempt            smallint not null default 0,
  failed_stage       text,
  last_error         jsonb,
  trace_id           text,
  first_seen_at      timestamptz not null default now(),
  last_seen_at       timestamptz not null default now(),
  last_fetched_at    timestamptz,
  next_crawl_at      timestamptz,
  crawl_interval_s   integer not null default 604800,
  tombstoned_at      timestamptz,
  tombstone_reason   text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  constraint duplicate_consistency check ((state = 'duplicate') = (duplicate_of is not null)),
  constraint tombstone_consistency check ((tombstoned_at is null) = (tombstone_reason is null)),
  constraint no_self_duplicate check (duplicate_of is null or duplicate_of <> document_id)
);

create unique index documents_tenant_url_hash
  on core.documents (tenant_id, url_hash);
create index documents_state_idx
  on core.documents (state, updated_at) where tombstoned_at is null;
create index documents_due_crawl
  on core.documents (next_crawl_at)
  where available_revision is not null and tombstoned_at is null;
create index documents_host_idx on core.documents (host);

create table core.document_revisions (
  document_id        text not null references core.documents(document_id),
  revision           integer not null check (revision > 0),
  fetched_url        text not null,
  http_status        smallint,
  content_type       text,
  content_hash_raw   bytea,
  content_hash_norm  bytea,
  raw_artifact_uri   text,
  parsed_artifact_uri text,
  enriched_artifact_uri text,
  artifact_bytes     bigint,
  title              text,
  language           text,
  published_at       timestamptz,
  author             text,
  word_count         integer,
  renderer_version   text,
  parser_version     text,
  normalizer_version text,
  enrichment_version text,
  superseded_by      integer,
  created_at         timestamptz not null default now(),

  primary key (document_id, revision),
  foreign key (document_id, superseded_by)
    references core.document_revisions(document_id, revision),
  constraint artifact_presence check (
    (content_hash_raw is null) = (raw_artifact_uri is null)
  )
);

create index revisions_hash_norm on core.document_revisions (content_hash_norm);

create table core.document_text (
  document_id  text not null,
  revision     integer not null,
  title        text not null default '',
  body         text not null,
  word_count   integer not null,
  language     text not null default 'und',
  tsv          tsvector generated always as (
                 setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                 setweight(to_tsvector('simple', coalesce(body, '')), 'B')
               ) stored,
  created_at   timestamptz not null default now(),
  primary key (document_id, revision),
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision)
);

create index document_text_tsv on core.document_text using gin (tsv);
create index document_text_body_trgm on core.document_text using gin (body gin_trgm_ops);

create table core.document_enrichment (
  document_id     text not null,
  revision        integer not null,
  enrichment_version text not null,
  summary         text,
  key_passages    jsonb not null default '[]',
  topics          text[] not null default '{}',
  entity_count    integer not null default 0,
  claim_count     integer not null default 0,
  chunk_count     integer not null default 0,
  token_cost      numeric(12,4) not null default 0,
  degraded        boolean not null default false,
  created_at      timestamptz not null default now(),
  primary key (document_id, revision, enrichment_version),
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision)
);

create index enrichment_active
  on core.document_enrichment (document_id, revision, created_at desc);

create table core.document_events (
  event_seq        bigserial primary key,
  document_id      text not null,
  revision         integer not null,
  stage            text not null,
  from_state       core.doc_state,
  to_state         core.doc_state not null,
  occurred_at      timestamptz not null default now(),
  attempt          smallint not null,
  processor        text not null,
  processor_version text not null,
  trace_id         text not null,
  artifact_uri     text,
  raw_artifact_uri text,
  duration_ms      integer,
  error_class      core.failure_class,
  error            jsonb,
  constraint event_has_trace check (length(trace_id) > 0)
);

create index events_document on core.document_events (document_id, occurred_at);
create index events_state_time on core.document_events (to_state, occurred_at);
create index events_stage_time on core.document_events (stage, occurred_at);
create index events_trace on core.document_events (trace_id);

create view core.live_documents as
select d.*, r.content_hash_raw, r.content_hash_norm, r.raw_artifact_uri,
       r.parsed_artifact_uri, r.enriched_artifact_uri, r.http_status,
       r.title, r.language, r.published_at, r.word_count
from core.documents d
left join core.document_revisions r
  on r.document_id = d.document_id and r.revision = d.available_revision;
