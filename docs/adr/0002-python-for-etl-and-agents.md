# ADR-0002: Python for ETL and agent orchestration

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering

## Context

The intelligence plane must do things where Python's ecosystem is decisive:

- Main-content extraction (`trafilatura`, `readability-lxml`), encoding detection, language ID.
- Entity extraction, sentence splitting, and claim extraction with model SDKs.
- Embeddings (`sentence-transformers`, provider SDKs) and tokenization.
- Long-running agent workflows with the Temporal Python SDK.
- Fast iteration on prompts and schemas.

These workloads are CPU/LLM-bound and asynchronous, not latency-critical at the edge.

## Decision

Use Python 3.12 for `parser`, `enricher`, `graph-builder`, `llm-gateway`, and
`research-orchestrator`, managed with `uv`.

Rules that keep it operable:

- Async end-to-end for I/O (`httpx`, `asyncpg`); CPU work in a process pool, never inline in
  the event loop.
- Pydantic v2 models at every boundary; `mypy --strict` on shared packages.
- Deterministic pure functions for parsing/normalization/chunking, verified by golden files.
- One shared package (`wre_pipeline`) for state transitions, contracts, and clients.
- No ORM, no framework that owns the request lifecycle.

## Consequences

Positive:

- Extraction and model work ship in days, not weeks.
- Prompt and schema changes are code-reviewable and testable against fixtures.
- The Temporal Python SDK covers the dynamic planning loop the research workflow needs.
- Dependency isolation: only `llm-gateway` imports provider SDKs.

Negative:

- GIL and per-process memory: scale by process/pod, not threads.
- Packaging and cold start are worse than Go; images are larger and require dependency
  pinning.
- Type safety is opt-in; enforced with mypy and boundary validation.
- Two runtimes to secure and patch (see ADR-0001).

## Alternatives considered

| Alternative | Why not |
|---|---|
| Go for parsing/enrichment | Weaker NLP ecosystem; would require reimplementing extraction quality that already exists |
| Separate TypeScript worker | No advantage for NLP; adds a third runtime |
| Notebook-driven pipeline | Not deployable or testable; unacceptable for provenance-critical data |
| Vendor-managed extraction API | Adds cost and a data-egress boundary for raw content; loses local determinism |

## Revisit trigger

- A single stage exceeds 50 docs/s CPU-bound per node and Python's overhead becomes the
  bottleneck → extract that stage into a Go/Rust service with the same contract.
- Temporal's Python SDK blocks a required workflow feature.
- Dependency footprint or cold start becomes an operational problem at scale.
