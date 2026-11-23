# Source Registry

Status: Phase 2 implementation. The registry is owned by `scheduler` and lives in the
`ingest` schema.

A source is a discovery target, not an authority. Reliability scores influence scheduling
and presentation; they are never evidence that an individual article or claim is correct.

## 1. Data model

| Table | Purpose |
|---|---|
| `ingest.sources` | Registry entries: URL, type, language, priority, robots policy, health-backed schedule, quality composite |
| `ingest.source_health` | Current state (`unknown`, `healthy`, `degraded`, `failing`, `blocked`) and failure/success counters |
| `ingest.source_crawl_history` | Append-only crawl outcomes reported by workers |
| `ingest.source_domain_policies` | Per-domain concurrency, request rate, crawl delay, robots override, allow/deny |
| `ingest.source_auth_refs` | References to secrets; secret values never enter the registry |
| `ingest.source_discovery_history` | Discovery inputs, candidates, and registration counts |
| `ingest.source_config_versions` | Immutable source configuration snapshots |
| `ingest.source_scores` | Dimension scores, weights, model version, and evaluation timestamp |

`source_auth_refs` is table-only in Phase 2; authentication flows are added with the source
connectors that need them. It stores a secret-manager reference, never a secret value.

`base_url` is unique. Domains are registrable domains (public-suffix aware) and are derived
from `base_url`; a supplied domain is recomputed. Candidates are stored with
`status='candidate'` and `crawl_enabled=false` until promoted.

## 2. API

Internal, scheduler-only routes:

| Method | Path | Purpose |
|---|---|---|
| POST | `/internal/v1/sources` | Register a source |
| GET | `/internal/v1/sources` | List/filter by status, type, or domain |
| GET | `/internal/v1/sources/{id}` | Read one source |
| PATCH | `/internal/v1/sources/{id}` | Update or promote/disable |
| DELETE | `/internal/v1/sources/{id}` | Remove a source and cascaded state |
| GET | `/internal/v1/sources/due` | Due active sources, bounded by domain policy |
| POST | `/internal/v1/sources/{id}/outcomes` | Report success/failure/skip and reschedule |
| POST | `/internal/v1/sources/{id}/score` | Evaluate and persist quality dimensions |
| GET | `/internal/v1/sources/{id}/score` | Latest persisted score |
| GET | `/internal/v1/sources/{id}/health` | Current health projection |
| GET | `/internal/v1/sources/{id}/history` | Recent crawl attempts |
| GET | `/internal/v1/sources/health` | Health for all sources |
| GET | `/internal/v1/sources/{id}/versions` | Configuration history |
| GET | `/internal/v1/sources/discoveries` | Discovery history |
| POST | `/internal/v1/sources/discover/seed` | Feed/sitemap/link discovery from a URL |
| POST | `/internal/v1/sources/discover/topic` | Provider-backed topic discovery |
| POST | `/internal/v1/sources/discover/graph` | External-domain discovery from an existing source |
| GET/PUT/DELETE | `/internal/v1/domain-policies[/{domain}]` | Domain policy management |

Discovery accepts `register=true` to insert candidates. Promotion is a normal PATCH with
`{"status":"active","crawl_enabled":true}`.

## 3. Discovery

- **Seed**: fetches a URL, observes `robots.txt`, parses HTML feed autodiscovery, sitemap
  declarations, and same-origin links, and proposes feed/website/sitemap candidates.
- **Topic**: sends configurable queries to a replaceable `TopicProvider`. The default
  provider is GDELT (`DISCOVERY_PROVIDER=gdelt`); `none` disables topic discovery.
- **Graph**: fetches an existing source, extracts outbound links, groups them by registrable
  domain, and proposes external domains with a link-count evidence trail.
- **External APIs**: providers implement `sources.TopicProvider` and are selected at
  startup. Adding another provider does not change the handler.

Discovery HTTP uses a guarded client: http/https only, no credentials, public addresses
only, redirects revalidated, five-hop limit, 20-second timeout, and a 2 MiB body cap.
`robots.txt` disallow rules are honored for the seed path.

## 4. Quality model

The model has eight dimensions, each in `[0,1]`:

authority, originality, historical accuracy, topical relevance, publication frequency,
freshness, transparency, corrections/retractions.

`source-quality-v1` weights: authority 0.20, originality 0.15, historical accuracy 0.15,
topical relevance 0.15, publication frequency 0.10, freshness 0.10, transparency 0.10,
corrections 0.05. The API merges evaluator overrides with explainable heuristics and stores
the model version, weights, evaluation time, and details. The composite is copied to
`sources.reliability_score` for scheduling and dashboards.

## 5. Scheduling

Base intervals (configurable, not mandates):

| Source type | Interval |
|---|---|
| News | 10 min |
| RSS/Atom | 15 min |
| Press releases | 45 min |
| Financial | 1 h |
| Government | 3 h |
| Research / dataset | 12 h |
| Website / PDF | 24 h |
| Forum | 1 h |

`crawl_frequency_seconds` overrides the type interval. Priority (10 highest, 1 lowest) and
reliability adjust the adaptive interval. Domain policy sets a floor from `crawl_delay_ms`
and `requests_per_minute`. Consecutive failures apply exponential backoff (capped at 7 days);
`Retry-After` is honored, and a domain or source robots denial removes the source from the
due set.

Workers report outcomes, then call `/sources/due` again. The registry stores every outcome
in crawl history and updates health and `next_crawl_at` atomically. A blocked source is
excluded from `/due`; PATCH it back to active with `crawl_enabled=true` after fixing the
policy to reset the blocked health state.

## 6. Dashboard

Grafana provisions the `Source Registry` dashboard from
`deploy/compose/grafana/provisioning/dashboards/source-registry.json`. It shows registered
sources by type, due count, health states, crawl outcomes, discovery runs, persisted score
evaluations, and average reliability by source type.

Prometheus metrics are intentionally aggregate-only:

- `wre_sources_total{source_type,status}`
- `wre_sources_due`
- `wre_source_health_total{state}`
- `wre_source_reliability_avg{source_type}`
- `wre_source_discoveries_total{mechanism,outcome}`
- `wre_source_crawl_outcomes_total{status}`
- `wre_source_scores_total{model_version}`

## 7. Configuration

| Variable | Purpose | Default |
|---|---|---|
| `DISCOVERY_PROVIDER` | `gdelt` or `none` | `gdelt` |
| `SOURCE_USER_AGENT` | Identification and contact for discovery requests | `wre-source-registry/0.1 (...)` |
| `DATABASE_URL` | Registry persistence | required outside development |
