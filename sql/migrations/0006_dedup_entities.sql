-- Deduplication, story clusters, lineage, and canonical entities.
-- Documents are never merged; clusters and lineage are derived and replaceable.

create schema if not exists graph;
create schema if not exists dedup;

create table graph.entities (
  entity_id       text primary key,
  tenant_id       text not null default 'public',
  entity_type     text not null check (entity_type in (
                    'person', 'organization', 'government', 'location', 'product',
                    'industry', 'technology', 'financial_instrument', 'project',
                    'policy', 'event', 'work', 'concept', 'other')),
  canonical_name  text not null,
  normalized_name text not null,
  aliases         text[] not null default '{}',
  alias_keys      text[] not null default '{}',
  description     text,
  external_ids    jsonb not null default '{}'::jsonb,
  country         text,
  confidence      real not null default 0.5 check (confidence between 0 and 1),
  merged_into     text references graph.entities(entity_id),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint no_self_merge check (merged_into is null or merged_into <> entity_id)
);

create unique index entities_canonical
  on graph.entities (tenant_id, entity_type, normalized_name)
  where merged_into is null;
create index entities_aliases on graph.entities using gin (aliases);
create index entities_alias_keys on graph.entities using gin (alias_keys);
create index entities_external_ids on graph.entities using gin (external_ids jsonb_path_ops);
create index entities_name_trgm on graph.entities using gin (normalized_name gin_trgm_ops);

create table graph.entity_mentions (
  mention_id        bigserial primary key,
  entity_id         text not null references graph.entities(entity_id),
  document_id       text not null,
  revision          integer not null,
  surface_form      text not null,
  char_start        integer not null,
  char_end          integer not null,
  confidence        real not null check (confidence between 0 and 1),
  extractor_version text not null,
  created_at        timestamptz not null default now(),
  constraint mention_span check (char_start >= 0 and char_end > char_start),
  unique (document_id, revision, char_start, char_end, entity_id)
);
create index mentions_entity on graph.entity_mentions (entity_id, document_id);
create index mentions_document on graph.entity_mentions (document_id, revision);

create table graph.entity_merges (
  merge_id       bigserial primary key,
  from_entity_id text not null references graph.entities(entity_id),
  into_entity_id text not null references graph.entities(entity_id),
  method         text not null,
  confidence     real not null check (confidence between 0 and 1),
  evidence       jsonb not null default '{}'::jsonb,
  performed_by   text not null,
  created_at     timestamptz not null default now(),
  constraint merge_distinct check (from_entity_id <> into_entity_id)
);

create table graph.entity_match_candidates (
  candidate_id       text primary key,
  tenant_id          text not null default 'public',
  input_name         text not null,
  normalized_name    text not null,
  entity_type        text,
  domain             text,
  country            text,
  candidate_entity_id text references graph.entities(entity_id),
  match_score        real not null check (match_score between 0 and 1),
  match_features     jsonb not null default '{}'::jsonb,
  decision_status    text not null check (decision_status in ('auto_match', 'review', 'keep_separate')),
  model_version      text not null,
  review_status      text not null default 'not_required'
                       check (review_status in ('pending', 'accepted', 'rejected', 'not_required')),
  resolved_entity_id text references graph.entities(entity_id),
  created_at         timestamptz not null default now(),
  reviewed_at        timestamptz
);
create index entity_candidates_pending
  on graph.entity_match_candidates (created_at desc) where review_status = 'pending';
create index entity_candidates_name_trgm
  on graph.entity_match_candidates using gin (normalized_name gin_trgm_ops);

create table dedup.story_clusters (
  cluster_id                text primary key,
  tenant_id                 text not null default 'public',
  title                     text not null default '',
  representative_document_id text references core.documents(document_id) on delete set null,
  member_count              integer not null default 0,
  independent_source_count  integer not null default 0,
  syndicated_count          integer not null default 0,
  official_count            integer not null default 0,
  confidence                real not null default 0 check (confidence between 0 and 1),
  model_version             text not null,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);
create index story_clusters_updated on dedup.story_clusters (updated_at desc);

create table dedup.story_members (
  cluster_id   text not null references dedup.story_clusters(cluster_id) on delete cascade,
  document_id  text not null references core.documents(document_id) on delete cascade,
  revision     integer not null,
  similarity   real not null default 0 check (similarity between 0 and 1),
  relation     text not null default 'original' check (relation in (
                 'original', 'syndicated_copy', 'follows_up', 'independent',
                 'official_statement', 'repeated_claim')),
  origin_kind  text not null default 'media' check (origin_kind in ('official', 'media', 'unknown')),
  added_at     timestamptz not null default now(),
  primary key (cluster_id, document_id),
  unique (document_id)
);

create table dedup.lineage_edges (
  edge_id             bigserial primary key,
  tenant_id           text not null default 'public',
  document_id         text not null references core.documents(document_id) on delete cascade,
  related_document_id text not null references core.documents(document_id) on delete cascade,
  relation            text not null check (relation in (
                        'original', 'syndicated_copy', 'cites', 'follows_up',
                        'independent', 'official_statement', 'repeated_claim')),
  confidence          real not null default 0 check (confidence between 0 and 1),
  evidence            jsonb not null default '{}'::jsonb,
  model_version       text not null,
  created_at          timestamptz not null default now(),
  unique (document_id, related_document_id, relation),
  constraint lineage_not_self check (document_id <> related_document_id)
);
create index lineage_document on dedup.lineage_edges (document_id);
create index lineage_related on dedup.lineage_edges (related_document_id);

create table dedup.semantic_candidates (
  candidate_id          text primary key,
  tenant_id             text not null default 'public',
  document_id           text not null references core.documents(document_id) on delete cascade,
  candidate_document_id text not null references core.documents(document_id) on delete cascade,
  similarity            real not null check (similarity between 0 and 1),
  model_version         text not null,
  features              jsonb not null default '{}'::jsonb,
  decision_status       text not null default 'review' check (decision_status = 'review'),
  review_status         text not null default 'pending'
                          check (review_status in ('pending', 'accepted', 'rejected')),
  created_at            timestamptz not null default now(),
  reviewed_at           timestamptz,
  unique (document_id, candidate_document_id, model_version),
  constraint semantic_not_self check (document_id <> candidate_document_id)
);
create index semantic_candidates_pending
  on dedup.semantic_candidates (created_at desc) where review_status = 'pending';

create table dedup.jobs (
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

create index dedup_jobs_pending
  on dedup.jobs (status, next_attempt_at, created_at);
create unique index dedup_jobs_active
  on dedup.jobs (document_id)
  where status in ('pending', 'running');

create trigger entities_touch_updated_at
  before update on graph.entities
  for each row execute function ingest.touch_updated_at();
create trigger story_clusters_touch_updated_at
  before update on dedup.story_clusters
  for each row execute function ingest.touch_updated_at();
create trigger dedup_jobs_touch_updated_at
  before update on dedup.jobs
  for each row execute function ingest.touch_updated_at();
