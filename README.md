# Web Research Engine

A provenance-first web research platform. Phase 1 is the repository, development
environment, and infrastructure foundation described in `docs/architecture/` and the
Phase 1 brief.

The service inventory and storage choices follow the accepted Phase 0 architecture. In
particular, OpenSearch, Qdrant, Redis, and Neo4j are **not** in this stack: ADR-0005,
ADR-0006, and ADR-0007 defer them until their revisit triggers. PostgreSQL + pgvector,
Redpanda, S3-compatible object storage (Silo), and Temporal are the active stores.

## Quick start

Prerequisites: Docker Desktop with Compose v2, Go 1.26+, Python 3.12+, and `uv`.

```bash
cp .env.example .env          # optional; local defaults are already in compose
make dev                      # build and start the core stack, wait for health
make smoke                    # health, migrations, traces, and log correlation
```

Web entry points (all bound to `127.0.0.1`):

| Service | URL |
|---|---|
| api | http://127.0.0.1:8080/healthz |
| Grafana | http://127.0.0.1:3000 (admin/admin) |
| Prometheus | http://127.0.0.1:9090 |
| Silo console | http://127.0.0.1:9001 |
| Redpanda console | `docker compose --profile tools up -d redpanda-console` then http://127.0.0.1:8090 |
| Temporal UI | `docker compose --profile tools up -d temporal-ui` then http://127.0.0.1:8233 |

`make clean` deletes the local volumes. `make logs` follows all container logs.

If a local PostgreSQL already owns port 5432, set `POSTGRES_PORT` to a free host port
(for example `55433`) in `.env`; the containers still use 5432 internally.

## Services

Every service has `/healthz` (liveness), `/readyz` (dependency readiness), and `/metrics`
(Prometheus). Python services expose only these probe endpoints for now; business surfaces
are added in later phases.

| Service | Language | Health port | Plane |
|---|---|---|---|
| `api` | Go | 8080 | edge |
| `scheduler` | Go | 8081 | ingestion / source registry |
| `fetcher` | Go | 8082 | ingestion |
| `indexer` | Go | 8083 | ingestion |
| `reconciler` | Go | 8084 | ops |
| `parser` | Python | 8085 | ingestion |
| `enricher` | Python | 8086 | intelligence |
| `graph-builder` | Python | 8087 | intelligence |
| `llm-gateway` | Python | 8088 | intelligence |
| `research-orchestrator` | Python | 8089 | research |

## Source registry

Phase 2 lives in `scheduler` under `/internal/v1`:

- Source CRUD, promotion, health, configuration versions.
- Seed, topic, and graph discovery; topic providers are replaceable (`DISCOVERY_PROVIDER`).
- Eight-dimension quality scoring with model version and evaluation timestamp.
- Adaptive scheduling with priority, reliability, domain floors, exponential backoff, and
  `Retry-After`.
- Per-domain concurrency/rate/robots policies.
- A provisioned Grafana `Source Registry` dashboard and aggregate Prometheus metrics.

See `docs/engineering/source-registry.md` for the schema, API, and scheduling rules.

## Repository layout

```
cmd/                    Go entrypoints (one directory per deployable)
internal/platform/      shared Go bootstrap: config, logging, telemetry, health
internal/contracts/     Go contract fixtures test
contracts/events/v1/    versioned JSON Schemas for the event envelope and payloads
contracts/fixtures/     fixtures shared by Go and Python contract tests
python/packages/wre_pipeline/   shared Python runtime
python/services/               Python deployables (one package per service)
internal/sources/              source registry, discovery, scoring, scheduling API
sql/migrations/                numbered, forward-only SQL
deploy/compose/                Dockerfiles, Prometheus, Loki, Promtail, Tempo, OTel, Grafana
scripts/                       migration runner and smoke checks
docs/                          Phase 0 architecture, data contracts, ADRs
```

## Configuration

Configuration is environment-driven and validated at startup. `APP_ENV=staging` or
`production` fails fast when `DATABASE_URL`, `KAFKA_BROKERS`, `S3_ENDPOINT`, or
`OTEL_EXPORTER_OTLP_ENDPOINT` is missing. Development defaults are in
`docker-compose.yml`; see `.env.example` for the full list.

Secrets are never baked into images and never committed. Local compose uses throwaway
credentials. Staging and production must inject credentials from a secret manager or the
host's secret store; the only process that receives an LLM provider key is `llm-gateway`.
See `docs/engineering/environments.md`.

## Migrations

`migrate` is a one-shot compose service that starts after PostgreSQL is healthy and
completes before application services start. It applies numbered files in order, records
each filename and SHA-256 in `schema_migrations`, and fails if an applied file changes.

```bash
make migrate     # apply pending migrations against the local stack
```

`0002_core_schema.sql` implements the normative DDL in
`docs/data/canonical-document-schema.md`. In staging, the same service runs on every
`docker compose up`, which is the automatic migration path for Phase 1.

## Observability

- Services emit OTLP traces to the collector, which forwards to Tempo.
- Services expose RED metrics on `/metrics`; Prometheus scrapes all ten services.
- Services write one JSON log line per unit of work with `service`, `trace_id`, and
  `span_id`. Promtail ships container logs to Loki.
- Grafana provisions Prometheus, Loki, and Tempo. In Loki, a log line's `trace_id` is a
  link to the trace; in Tempo, a trace links back to matching logs.
- `make smoke` proves the join: it calls the Go API and Python parser with explicit
  `traceparent` headers, checks both traces in Tempo, then checks the API trace id appears
  in Loki logs.

SLOs, metric names, and alert thresholds remain as documented in
`docs/engineering/observability.md`. Dashboard JSON is a Phase 2 deliverable; Grafana
Explore is provisioned now.

## Testing and CI

```bash
make test       # go test ./... and pytest
make lint       # gofmt, go vet, golangci-lint, ruff, mypy
make contracts  # event schemas and fixtures in both languages
make smoke      # integration check against the running compose stack
```

CI (`.github/workflows/ci.yml`) runs on every pull request: Go format/vet/lint/tests,
Python format/lint/types/tests, both contract suites, Docker image builds for all ten
services, vulnerability and secret scans, and a compose integration job that runs the
same smoke check. `release.yml` publishes images to GHCR on `main` and deploys to staging
when `STAGING_HOST` is configured.

## Staging

Staging is one Docker host running this compose file with `APP_ENV=staging` and real
credentials injected through its environment or secret manager. The release workflow
pulls GHCR images and runs `docker compose up`; the `migrate` service applies pending
migrations before application containers start. Required repository variables and secrets
are listed in `docs/engineering/environments.md`.

## Deliberate Phase 1–2 gaps

- The non-scheduler service mains are still health/telemetry skeletons; the scheduler now
  owns the Phase 2 source registry and discovery API.
- No Kubernetes manifests; the brief explicitly requires containers first.
- Only the Source Registry dashboard is provisioned; broader operational dashboards follow.
- OpenSearch, Qdrant, Neo4j, and Redis are omitted per the ADRs. Reintroduce only through
  their revisit triggers or a new ADR.
- A headless-browser fetcher sidecar is deferred until fetch/render logic exists.
