-- ETL registry and queue. Raw artifacts stay immutable; every derived output is
-- keyed by processing version so reprocessing never destroys the previous result.

create schema if not exists processing;

create table processing.versions (
  version      text primary key,
  processor    text not null,
  description  text not null default '',
  config_hash  text not null,
  active       boolean not null default false,
  created_at   timestamptz not null default now()
);

create table processing.document_outputs (
  document_id        text not null,
  revision           integer not null,
  processing_version text not null references processing.versions(version),
  content_hash_norm  bytea not null,
  simhash            bigint not null,
  title              text not null default '',
  body               text not null,
  language           text not null default 'und',
  word_count         integer not null check (word_count >= 0),
  quality_score      numeric(5,4) not null check (quality_score between 0 and 1),
  quality            jsonb not null default '{}'::jsonb,
  metadata           jsonb not null default '{}'::jsonb,
  warnings           jsonb not null default '[]'::jsonb,
  parsed_uri         text not null,
  canonical_uri      text not null,
  is_duplicate       boolean not null default false,
  duplicate_of       text,
  created_at         timestamptz not null default now(),
  primary key (document_id, revision, processing_version),
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision) on delete cascade
);

create index document_outputs_hash on processing.document_outputs (content_hash_norm);
create index document_outputs_simhash on processing.document_outputs (simhash);
create index document_outputs_quality
  on processing.document_outputs (processing_version, quality_score desc);

create table processing.jobs (
  job_id          text primary key,
  document_id     text not null,
  revision        integer not null,
  mode            text not null default 'extract'
                    check (mode in ('extract', 'reparse', 'backfill')),
  target_version  text not null,
  status          text not null default 'pending'
                    check (status in ('pending', 'running', 'done', 'failed', 'dead_letter')),
  attempts        integer not null default 0,
  max_attempts    integer not null default 3 check (max_attempts between 1 and 10),
  priority        integer not null default 5 check (priority between 1 and 10),
  reason          text not null default '',
  requested_by    text not null default 'dispatcher',
  lease_owner     text,
  lease_expires_at timestamptz,
  next_attempt_at timestamptz not null default now(),
  last_error      text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  foreign key (document_id, revision)
    references core.document_revisions(document_id, revision) on delete cascade
);

create index jobs_pending
  on processing.jobs (status, next_attempt_at, priority desc, created_at);
create unique index jobs_active
  on processing.jobs (document_id, revision, target_version)
  where status in ('pending', 'running');

create trigger processing_jobs_touch_updated_at
  before update on processing.jobs
  for each row execute function ingest.touch_updated_at();

create view processing.live_outputs as
select distinct on (document_id, revision) *
from processing.document_outputs
order by document_id, revision, created_at desc;
