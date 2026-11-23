-- rollback: forward-fix only. Source history and scores are reference data used to
-- explain crawl decisions; removing them needs a separate retention plan.

create table ingest.sources (
  id                       uuid primary key default gen_random_uuid(),
  name                     text not null check (length(trim(name)) > 0),
  base_url                 text not null,
  source_type              text not null check (source_type in (
                             'rss', 'website', 'news', 'government', 'research',
                             'financial', 'press_release', 'pdf', 'dataset', 'forum'
                           )),
  domain                   text not null,
  language                 text,
  crawl_frequency_seconds  integer check (crawl_frequency_seconds is null or crawl_frequency_seconds >= 60),
  priority                 integer not null default 5 check (priority between 1 and 10),
  robots_policy            text not null default 'respect'
                             check (robots_policy in ('respect', 'allow', 'deny')),
  crawl_enabled            boolean not null default true,
  status                   text not null default 'active'
                             check (status in ('active', 'candidate', 'disabled', 'rejected')),
  last_crawled_at          timestamptz,
  next_crawl_at            timestamptz,
  reliability_score        numeric(5,4) check (reliability_score is null or reliability_score between 0 and 1),
  metadata                 jsonb not null default '{}'::jsonb,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  constraint sources_base_url_key unique (base_url),
  constraint sources_candidate_disabled check (status <> 'candidate' or not crawl_enabled)
);

create index sources_domain_idx on ingest.sources (domain);
create index sources_status_type_idx on ingest.sources (status, source_type);
create index sources_due_idx
  on ingest.sources (next_crawl_at)
  where status = 'active' and crawl_enabled;

create table ingest.source_health (
  source_id                 uuid primary key references ingest.sources(id) on delete cascade,
  state                     text not null default 'unknown'
                              check (state in ('unknown', 'healthy', 'degraded', 'failing', 'blocked')),
  consecutive_failures      integer not null default 0 check (consecutive_failures >= 0),
  total_successes           bigint not null default 0,
  total_failures            bigint not null default 0,
  last_success_at           timestamptz,
  last_failure_at           timestamptz,
  last_error                text,
  last_retry_after_seconds  integer check (last_retry_after_seconds is null or last_retry_after_seconds >= 0),
  updated_at                timestamptz not null default now()
);

create table ingest.source_crawl_history (
  id             bigserial primary key,
  source_id      uuid not null references ingest.sources(id) on delete cascade,
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  status         text not null check (status in ('success', 'failure', 'skipped')),
  http_status    integer check (http_status is null or http_status between 100 and 599),
  items_found    integer not null default 0 check (items_found >= 0),
  error          text,
  trigger        text not null default 'scheduled',
  created_at     timestamptz not null default now()
);

create index crawl_history_source_time
  on ingest.source_crawl_history (source_id, started_at desc);

create table ingest.source_domain_policies (
  domain               text primary key,
  max_concurrency      integer not null default 1 check (max_concurrency between 1 and 100),
  requests_per_minute  integer not null default 30 check (requests_per_minute between 1 and 10000),
  crawl_delay_ms       integer not null default 1000 check (crawl_delay_ms between 0 and 3600000),
  robots_policy        text not null default 'respect'
                         check (robots_policy in ('respect', 'allow', 'deny')),
  allowed              boolean not null default true,
  notes                text,
  metadata             jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create table ingest.source_auth_refs (
  id           uuid primary key default gen_random_uuid(),
  source_id    uuid not null references ingest.sources(id) on delete cascade,
  auth_type    text not null,
  secret_ref   text not null,
  header_name  text,
  expires_at   timestamptz,
  metadata     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  constraint auth_refs_secret_is_reference check (secret_ref not like '% %')
);

create index auth_refs_source on ingest.source_auth_refs (source_id);

create table ingest.source_discovery_history (
  id                uuid primary key default gen_random_uuid(),
  mechanism         text not null check (mechanism in ('seed', 'topic', 'graph', 'external')),
  input             jsonb not null default '{}'::jsonb,
  candidates        jsonb not null default '[]'::jsonb,
  candidate_count   integer not null default 0 check (candidate_count >= 0),
  registered_count  integer not null default 0 check (registered_count >= 0),
  status            text not null default 'completed'
                      check (status in ('completed', 'failed')),
  error             text,
  created_at        timestamptz not null default now()
);

create index discovery_history_time
  on ingest.source_discovery_history (created_at desc);

create table ingest.source_config_versions (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid not null references ingest.sources(id) on delete cascade,
  version     integer not null check (version > 0),
  config      jsonb not null,
  created_by  text,
  created_at  timestamptz not null default now(),
  constraint source_config_version_key unique (source_id, version)
);

create table ingest.source_scores (
  id                    uuid primary key default gen_random_uuid(),
  source_id             uuid not null references ingest.sources(id) on delete cascade,
  model_version         text not null,
  evaluated_at          timestamptz not null default now(),
  authority             numeric(5,4) not null check (authority between 0 and 1),
  originality           numeric(5,4) not null check (originality between 0 and 1),
  historical_accuracy   numeric(5,4) not null check (historical_accuracy between 0 and 1),
  topical_relevance     numeric(5,4) not null check (topical_relevance between 0 and 1),
  publication_frequency numeric(5,4) not null check (publication_frequency between 0 and 1),
  freshness             numeric(5,4) not null check (freshness between 0 and 1),
  transparency          numeric(5,4) not null check (transparency between 0 and 1),
  corrections           numeric(5,4) not null check (corrections between 0 and 1),
  composite             numeric(5,4) not null check (composite between 0 and 1),
  weights               jsonb not null default '{}'::jsonb,
  details               jsonb not null default '{}'::jsonb,
  constraint source_score_key unique (source_id, model_version, evaluated_at)
);

create index source_scores_source_time
  on ingest.source_scores (source_id, evaluated_at desc);

create or replace function ingest.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger sources_touch_updated_at
  before update on ingest.sources
  for each row execute function ingest.touch_updated_at();

create trigger source_health_touch_updated_at
  before update on ingest.source_health
  for each row execute function ingest.touch_updated_at();

create trigger source_domain_policies_touch_updated_at
  before update on ingest.source_domain_policies
  for each row execute function ingest.touch_updated_at();
