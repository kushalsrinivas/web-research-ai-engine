# ADR-0001: Go for crawling and API infrastructure

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering

## Context

The ingestion plane and public API have different needs from the intelligence plane:

- Thousands of concurrent HTTP fetches with strict timeouts and bounded memory per connection.
- Per-host rate limiting and circuit breaking at high concurrency.
- Low tail latency on a read-heavy API (search p95 200 ms).
- Single-binary deploys, fast cold start, easy profiling in production.
- A small team that must operate this without a large SRE function.

Parsing, NLP, and agent work pull in the opposite direction: the Python ecosystem is where
extraction and model tooling live (ADR-0002).

## Decision

Use Go for `api`, `scheduler`, `fetcher`, `indexer`, and `reconciler`.

Specific choices inside Go:

- `net/http` + `chi`; no framework.
- `sqlc` for queries; no ORM.
- `log/slog` and OpenTelemetry.
- `errgroup` for concurrency; `context` propagated everywhere.

## Consequences

Positive:

- Goroutines map cleanly to tens of thousands of in-flight fetches with a small resident set.
- Static binaries and distroless images reduce attack surface and deploy time.
- `pprof` and the race detector are available without extra tooling.
- The fetcher's SSRF policy is easier to audit in one small, typed codebase.

Negative:

- Two languages in one repo (Go + Python). Mitigated by generated/shared contracts and
  fixtures in `/contracts`, and by a CI test that loads the same state-machine fixture in both.
- Go has no equivalent of pandas/spaCy; nobody will be tempted to put extraction logic here,
  which is a feature.
- More boilerplate for JSON handling than Python; mitigated by code generation from schemas.

## Alternatives considered

| Alternative | Why not |
|---|---|
| All Python | GIL and per-connection memory make 10k concurrent fetches expensive; tail latency under GC is harder to control |
| All Go | Extraction/NLP/model tooling is materially weaker; would slow Phase 1 |
| Rust for fetcher | Better resource profile, but slower iteration and a third language for a marginal gain at 10 pages/s |
| Node.js | Event loop is fine for I/O, but CPU-bound parsing and indexing sit awkwardly; weaker typing at boundaries |

## Revisit trigger

- Sustained crawl rate > 100 pages/s, where per-connection overhead dominates.
- The team consolidates to one language for hiring reasons.
- Go's HTTP stack cannot meet a new fetch requirement (e.g., HTTP/3 quirks) that Python/Rust
  handles materially better.
