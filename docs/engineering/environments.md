# Environments

Status: Phase 1 implementation note. This document explains how the three environments in
`docs/architecture/deployment-architecture.md` are configured.

## 1. Environments

| Environment | How it runs | Data | Secrets |
|---|---|---|---|
| `development` | `docker-compose.yml` on a workstation | Synthetic, disposable volumes | Local defaults in compose |
| `staging` | The same compose file on a single Docker host | Scrubbed or synthetic corpus | Host environment or secret manager |
| `production` | Kubernetes, per `deployment-architecture.md` | Full corpus | Secret manager with encryption and access control |

Phase 1 deliberately stops at containers. Kubernetes manifests are not in this repository
yet.

## 2. Configuration

Configuration is environment variables, loaded once, validated at startup. Missing
required values outside development exit non-zero rather than silently defaulting.

Required when `APP_ENV` is `staging` or `production`:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string; include `sslmode` where the host supports it |
| `KAFKA_BROKERS` | Redpanda/Kafka bootstrap brokers |
| `S3_ENDPOINT` | S3-compatible object store endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint |

Service-specific values:

| Variable | Used by | Default (development) |
|---|---|---|
| `APP_ENV` | all | `development` |
| `LOG_LEVEL` | all | `info` |
| `OTEL_SAMPLE_RATIO` | all | `1.0` development, `0.1` otherwise |
| `SERVICE_VERSION` | all | `dev`; baked into images as the Git SHA |
| `DATABASE_URL` | all | `postgres://wre:wre@postgres:5432/wre?sslmode=disable` |
| `KAFKA_BROKERS` | all | `redpanda:9092` |
| `TEMPORAL_HOST` | research services | `temporal:7233` |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | ingestion services | `http://silo:9000`, dev credentials |
| `S3_BUCKET_RAW`, `S3_BUCKET_PARSED`, `S3_BUCKET_ENRICHED` | ingestion services | `wre-raw`, `wre-parsed`, `wre-enriched` |
| `FETCHER_MAX_CONCURRENCY`, `FETCHER_TIMEOUT_SECONDS` | fetcher | `20`, `30` |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` | llm-gateway only | `stub`, `stub`, empty |
| `POSTGRES_PORT` | compose host mapping | `5432`; change if a local PostgreSQL owns it |

Env files are read only by Compose for local development. `.env` is gitignored;
`.env.example` contains placeholders, not credentials.

## 3. Secrets

- Never commit a real credential, token, private key, or `.env` file. CI runs gitleaks.
- Never pass provider credentials to any service except `llm-gateway`.
- Build images from the repository context only; `.dockerignore` excludes local env files.
- Staging must not reuse development buckets, databases, or credentials.
- Production secrets live in the platform secret manager and are mounted as files or
  injected as environment variables by the orchestrator.

## 4. Migrations

The `migrate` compose service is the single migrator. It runs after PostgreSQL is healthy
and must complete before application services start. It applies `sql/migrations/*.sql` in
lexical order, records `(filename, sha256, applied_at)` in `schema_migrations`, and refuses
to continue when an already-applied file changes.

Runbook:

```bash
make migrate                         # local, explicit
docker compose up -d --wait          # staging, automatic
```

- Migrations are forward-only and reviewed before merge.
- Use expand → backfill → contract. Do not add and drop the same column in one release.
- A destructive migration needs an approved rollback/forward-fix plan.
- The migrate role must be able to create the `vector` and `pg_trgm` extensions, or the
  platform must pre-install them. The local pgvector image includes both.
- If staging fails halfway, the failed migration is not recorded; fix forward with a new
  migration instead of editing the applied file.

## 5. Staging deployment

The release workflow publishes images to GHCR and, when the repository variable
`STAGING_HOST` is set, connects over SSH and runs the compose stack on `main`.

Repository configuration:

| Kind | Name | Meaning |
|---|---|---|
| Variable | `STAGING_HOST` | SSH host; unset disables deployment |
| Variable | `STAGING_USER` | SSH user |
| Variable | `STAGING_DIR` | Checkout on the host, default `/opt/wre` |
| Secret | `STAGING_SSH_KEY` | Private key accepted by the host |

The host needs Docker, the repository checked out at `STAGING_DIR`, and a non-committed
`.env` with `APP_ENV=staging` and production-like credentials. `docker compose up` pulls
the images, runs migrations to completion, then starts the services and waits for their
health checks. The workflow also exports `VERSION` as the Git SHA so runtime processor
versions match the image build.

## 6. Exit criteria mapping

| Phase 1 criterion | Where it is satisfied |
|---|---|
| All services start through the development environment | `docker-compose.yml`, `make dev` |
| Every service has a health endpoint | Go `internal/platform/health`; Python `wre_pipeline.health`; compose healthchecks |
| CI runs on every pull request | `.github/workflows/ci.yml` |
| Migrations run automatically in staging | `migrate` compose service with `depends_on` |
| Shared event schemas are version-controlled | `contracts/events/v1/` plus fixtures and tests in both languages |
| Logs and traces correlate across services | W3C trace propagation, `trace_id` in JSON logs, Tempo/Loki/Grafana provisioning, `make smoke` |
