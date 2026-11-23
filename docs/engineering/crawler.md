# Crawler (Phase 3)

Worker-based crawler: the scheduler owns the frontier and publishes to Kafka,
`fetcher` workers execute jobs with leases stored in Postgres. Browser rendering
is not part of this phase.

## Modules

| Package | Role |
|---|---|
| `internal/crawler/urlnormalizer` | canonical URL + SHA-256 hash (fragment, default port, tracking parameters removed; meaningful query parameters kept) |
| `internal/crawler/dispatch` | scheduler loop: due sources → jobs, retries, stale dispatch recovery, Kafka producer, `/internal/v1/crawl` API |
| `internal/crawler/worker` | Kafka consumer group `fetcher`; claim → robots → gate → fetch → archive → persist → discover links |
| `internal/crawler/robots` | robots.txt parser and Postgres cache (`fetcher.robots_cache`) |
| `internal/crawler/safehttp` | SSRF guard: public-IP-only dialer, redirect revalidation, scheme/port/userinfo checks |
| `internal/crawler/gate` | per-host concurrency and request spacing |
| `internal/crawler/fetcher` | bounded HTTP GET/HEAD, compression, conditional `ETag`/`Last-Modified` requests, `Retry-After` parsing |
| `internal/crawler/content` | HTML/text/PDF/XML/JSON/RSS/Atom/image detection |
| `internal/crawler/archive` | immutable S3 layout with content-hash key; response headers are filtered of cookies/authorization |
| `internal/crawler/extract` | follow-up links from HTML, Atom, RSS, and sitemaps |
| `internal/crawler/store` | jobs, leases, retries, HTTP cache, workers, `core.documents`/`core.document_revisions` |

## Job states

`pending → dispatched → leased → archived`

- `retrying` — failed but attempts remain; `next_attempt_at` uses exponential
  backoff (30s, 1m, 2m, … capped at 1h) and honors `Retry-After`.
- `failed` — terminal non-retryable response (4xx except 408/429).
- `dead_lettered` — attempts exhausted; payload retained in Postgres.
- `skipped` — robots.txt or domain policy denied the URL.

Kafka delivers at least once. Workers claim ownership in Postgres
(`fetch.jobs.lease_owner`), so re-delivered messages cannot fetch twice.
Expired leases are reset to `retrying` by the dispatcher and republished.

## Data

- `fetcher.jobs`, `fetcher.workers`, `fetcher.robots_cache`, `fetcher.http_cache`.
- Raw bytes: `s3://wre-raw/raw/{tenant}/{source}/{yyyy}/{mm}/{dd}/{document}/{sha256}/original.*`
  plus `response.json` and `metadata.json` in the same prefix. A changed document
  produces a new content-hash prefix; identical bytes reuse the existing version.
- Document identity and revisions: `core.documents` and `core.document_revisions`.
  Identical bytes on a recrawl do not add a revision.

## API (scheduler)

```bash
# enqueue one URL (interactive lane)
curl -s -X POST http://127.0.0.1:8081/internal/v1/crawl/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/news"}'

# job status
curl -s http://127.0.0.1:8081/internal/v1/crawl/jobs/<job_id>

# queue counters and live workers
curl -s http://127.0.0.1:8081/internal/v1/crawl/stats
```

Scheduled crawls use each source's `base_url` as the depth-0 seed. Same-host
links are followed up to `CRAWLER_MAX_DEPTH` (default 2).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `FETCHER_MAX_CONCURRENCY` | `20` | jobs processed concurrently per worker |
| `FETCHER_TIMEOUT_SECONDS` | `30` | per-request HTTP timeout |
| `CRAWLER_MAX_DEPTH` | `2` | link-following depth |
| `CRAWLER_MAX_BODY_MB` | `10` | response size limit |
| `CRAWLER_ALLOW_PRIVATE` | `true` in development, else `false` | allows private/loopback targets; keep `false` in production |
| `CRAWLER_USER_AGENT` | `wre-crawler/0.1` | identify the crawler |

`CRAWLER_ALLOW_PRIVATE` exists so the dev stack can crawl its own services in
smoke tests. Production must leave it false; otherwise the crawler becomes an
SSRF proxy.

## Security

- Non-HTTP(S) schemes, userinfo, and non-80/443 ports are rejected unless
  private access is explicitly enabled.
- DNS is resolved once and the dialer connects to the validated public IP, so a
  second DNS answer cannot redirect to a private address.
- Redirects are revalidated on every hop; depth is capped at 5.
- Response bodies are bounded; archived headers strip `Set-Cookie`,
  `Authorization`, and proxy credentials.
- No paywall, CAPTCHA, or authentication bypass. Rules come from robots.txt and
  domain policies.

## Operations

- `make smoke` (or `make alt-smoke`) exercises one manual crawl end to end and
  requires the archived artifact URI.
- Metrics: `wre_crawler_jobs_total`, `wre_crawler_fetch_duration_seconds`,
  `wre_crawler_bytes_total`, `wre_crawler_inflight`, `wre_crawler_retries_total`,
  `wre_crawler_dead_letter_total`, `wre_crawler_lease_recovered_total`,
  `wre_crawler_robots_denied_total`, `wre_crawler_links_discovered_total`.
