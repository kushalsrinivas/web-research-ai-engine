# Coding Standards

Status: Phase 0 baseline. Enforced by CI; not advisory.

## 1. Repository layout

```
/cmd/{api,scheduler,fetcher,indexer,reconciler}   Go entrypoints
/internal/...                                     Go packages (no cross-service imports)
/contracts
  /events/v1/*.schema.json
  /openapi.v1.yaml
  predicates.v1.yaml
/python
  /packages/wre_pipeline/                         shared state machine, contracts, clients
  /services/{parser,enricher,graph_builder,llm_gateway,research_orchestrator}/
  /tests/
/sql/migrations/                                  numbered, forward-only
/docs
/deploy/{compose,k8s}/
```

Rules:

- One deployable per directory listed in `/cmd` and `/python/services`.
- Go services must not import another service's `internal` package. Shared code lives in
  `/internal/platform` (logging, config, telemetry, DB, broker) or `/internal/contracts`.
- Python services depend on `wre_pipeline`, never on each other.
- SQL lives with its owning service but migrations are centralized in `/sql/migrations` for
  review and ordering.

## 2. Go

| Topic | Standard |
|---|---|
| Formatting | `gofmt`, `goimports`; enforced by CI |
| Lint | `golangci-lint` with `errcheck`, `govet`, `staticcheck`, `ineffassign`, `bodyclose`, `contextcheck`, `sqlclosecheck` |
| Errors | Wrap with `%w`; classify with sentinel types from `internal/platform/failure`; never compare error strings |
| Context | First parameter, always propagated; no `context.Background()` outside `main` and tests |
| Timeouts | Every HTTP client and DB call has an explicit timeout; no default client |
| Concurrency | `errgroup` with context; every goroutine has an owner and a termination path; CI runs `-race` |
| Panics | Only in `main` for impossible startup states; handlers recover and return 500 with a trace id |
| Logging | `log/slog` JSON; keys lowercase snake_case; no `fmt.Printf`; no secrets/PII |
| Config | Struct loaded and validated at startup; missing required value = exit non-zero |
| DB | `sqlc`-generated queries; no string-built SQL; no ORM; explicit column lists |
| HTTP | `chi` router, middleware for auth/trace/recover; request body limits on every route |
| Tests | Table-driven, `t.Parallel` where safe, `httptest` for handlers, `testcontainers` for integration |

## 3. Python

| Topic | Standard |
|---|---|
| Version | 3.12, `uv` for env and lockfile (`uv.lock` committed) |
| Format/lint | `ruff format` + `ruff check` (E,F,I,UP,B,SIM,ASYNC,RUF) |
| Types | `mypy --strict` on `wre_pipeline` and public service modules; `Any` requires a comment |
| Boundaries | Pydantic v2 models for every external input/output; never pass dicts across service boundaries |
| Async | `async` end-to-end for I/O; no `requests`, no blocking SDK calls in the event loop; CPU work in a process pool |
| Errors | Domain exceptions with `code` + `retryable`; map to the shared failure taxonomy; never bare `except:` |
| DB | `asyncpg` with explicit SQL and parameter binding; no ORM; transactions via an explicit context manager |
| Time | `datetime.now(UTC)`; no naive datetimes |
| Tests | `pytest` + `pytest-asyncio`; fixtures for artifacts; no network in unit tests (`pytest-socket`) |

Determinism requirements:

- Parsing, normalization, chunking, and fingerprinting must be pure functions of
  `(artifact, version, config)`. Iteration order over sets/dicts that affects output is a bug.
- Any randomness (sampling, jitter) is seeded and logged in tests.

## 4. Contracts

1. JSON field names are `snake_case` everywhere (DB, events, API). No translation layers.
2. Any shared struct/model is generated or validated against `/contracts`. Duplicated
   definitions are prohibited; CI fails if the Go and Python fixtures diverge.
3. Every event and API response has a fixture in `/contracts/fixtures/`, used by both
   languages.
4. Optional fields are explicitly nullable; absent vs null is documented per field.
5. IDs are strings, never integers. Timestamps are RFC 3339 UTC.

## 5. State machine and idempotency

- All transitions go through `pipeline.Transition(ctx, tx, ...)` (Go) or
  `transition(...)` (Python). Direct `UPDATE core.documents SET state=...` fails review.
- The guard `WHERE state = expected AND current_revision = revision` is mandatory; a zero-row
  update means "already handled" and is logged at debug level, not error.
- Idempotency keys:
  - DB writes: natural keys + `ON CONFLICT DO UPDATE` with a version check.
  - LLM calls: hash of `(model, prompt, params, schema)`; cache hit returns the stored parsed
    output.
  - External fetches: conditional GET; identical bytes produce the same artifact key.

## 6. Migrations

1. Forward-only, numbered `0001_...sql`, applied by a single migrator job before deploys.
2. Expand → migrate → contract. A single release may not add and drop the same column.
3. No `ALTER TABLE` that rewrites a table over 1 GB without a documented online procedure.
4. Every migration has a `-- rollback:` comment describing the forward fix (data rollback is
   not assumed).
5. Adding a `NOT NULL` column requires a default or a two-step backfill.

## 7. Dependencies

- New dependency requires: license compatible with commercial use, maintained in the last 12
  months, no known critical CVE, and a one-line justification in the PR.
- Pin exact versions; commit lockfiles. Renovate opens grouped PRs weekly; security updates
  merge within 48 h.
- No framework that owns the request lifecycle (no Django, no Spring). Libraries only.
- LLM SDKs are isolated inside `llm-gateway`; no other service imports a provider SDK.

## 8. Logging and errors

- One log line per handled unit of work, with `document_id`, `stage`, `attempt`, `trace_id`,
  `outcome`, `duration_ms`. No logging inside tight loops.
- Errors carry `class` from the shared taxonomy. A caught error that is logged and not
  re-raised must increment a metric; silent swallowing is prohibited.
- Never log raw document bodies, prompts, API keys, or full URLs with credentials. URL
  query strings are redacted when they look like tokens.

## 9. Git and review

- Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- PR ≤ 400 changed lines excluding tests and generated code; larger needs a stated reason.
- Every PR states: what changed, contract impact, rollback, and how it was tested.
- Architecture changes need an ADR approved before implementation.
- Two approvals for anything touching `core` schema, auth, fetcher policy, or the state
  machine.

## 10. Definition of done

- [ ] Unit tests for new logic; contract fixture updated if a shared shape changed.
- [ ] Integration test covering the happy path and one failure path.
- [ ] Metrics and traces emitted per `observability.md`.
- [ ] Docs updated (`docs/data`, `docs/engineering`) when a contract changed.
- [ ] No new lint/type suppressions without a comment.
- [ ] Rollback verified in staging or a documented reason it cannot be.
