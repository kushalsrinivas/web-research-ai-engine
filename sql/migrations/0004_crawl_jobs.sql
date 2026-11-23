-- rollback: forward-fix only. Crawl jobs and leases are operational state; draining
-- them requires a separate retention decision.

create table fetcher.jobs (
  job_id            text primary key,
  source_id         uuid references ingest.sources(id) on delete set null,
  document_id       text not null,
  url               text not null,
  url_hash          bytea not null,
  host              text not null,
  tenant_id         text not null default 'public',
  lane              text not null default 'bulk' check (lane in ('interactive', 'bulk')),
  method            text not null default 'GET' check (method in ('GET', 'HEAD')),
  depth             integer not null default 0 check (depth >= 0),
  status            text not null default 'pending' check (status in (
                      'pending', 'dispatched', 'leased', 'retrying',
                      'archived', 'failed', 'dead_lettered', 'skipped'
                    )),
  attempts          integer not null default 0 check (attempts >= 0),
  max_attempts      integer not null default 5 check (max_attempts between 1 and 20),
  next_attempt_at   timestamptz not null default now(),
  lease_owner       text,
  lease_expires_at  timestamptz,
  payload           jsonb not null default '{}'::jsonb,
  last_error        text,
  http_status       integer check (http_status is null or http_status between 100 and 599),
  content_type      text,
  content_hash      bytea,
  artifact_uri      text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create unique index jobs_active_url
  on fetcher.jobs ((coalesce(source_id, '00000000-0000-0000-0000-000000000000'::uuid)), url_hash)
  where status in ('pending', 'dispatched', 'leased', 'retrying');
create index jobs_due on fetcher.jobs (status, next_attempt_at);
create index jobs_host on fetcher.jobs (host, status);
create index jobs_source on fetcher.jobs (source_id, created_at desc);

create trigger fetch_jobs_touch_updated_at
  before update on fetcher.jobs
  for each row execute function ingest.touch_updated_at();

create table fetcher.workers (
  worker_id      text primary key,
  hostname       text not null,
  concurrency    integer not null check (concurrency between 1 and 500),
  status         text not null default 'active' check (status in ('active', 'stopping')),
  started_at     timestamptz not null default now(),
  heartbeat_at   timestamptz not null default now(),
  last_error     text
);

create table fetcher.robots_cache (
  domain      text primary key,
  rules       text not null,
  fetched_at  timestamptz not null default now(),
  expires_at  timestamptz not null
);

create table fetcher.http_cache (
  url_hash        bytea primary key,
  url             text not null,
  etag            text,
  last_modified   timestamptz,
  content_hash    bytea,
  last_status     integer,
  last_fetched_at timestamptz not null default now()
);
