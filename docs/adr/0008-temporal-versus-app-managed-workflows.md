# ADR-0008: Temporal for research workflows, not for ingestion

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering

## Context

A research investigation is a long-running, interactive, dynamically planned workflow:

- 2–20 minutes wall clock, sometimes paused for human approval.
- Steps are decided at runtime (search → read → follow citations → search again).
- Must survive pod restarts, deploys, and provider timeouts mid-flight.
- Needs per-step retries, timeouts, cancellation, progress streaming, and auditability.
- Must enforce budgets across retries and parallel branches.

The ingestion pipeline is different: high-volume, short-lived, stateless handlers around a
durable log (ADR-0003). Temporal would be the wrong tool there — per-document workflows would
add overhead and a second durability system.

## Decision

Use **Temporal** for `research-orchestrator` only.

- One workflow type per investigation shape (`InvestigationWorkflow`), versioned.
- Activities: `search`, `read_document`, `get_claims`, `request_fetch`, `llm_step`,
  `persist_citation`, `finalize_report`.
- Task queue `research`, separate from all ingestion consumers, with its own worker deployment.
- Workflow state (plan, budget counters, citations, messages) is workflow-visible and
  queryable; the `research` Postgres schema stores the durable projection for API reads.
- Cancellation via workflow cancel; pause/resume via signals; human approval as a signal with
  a timeout.
- Determinism: all I/O in activities; no wall-clock/random in workflow code; versioning via
  `workflow.patched` for behavior changes.

Deployment: Temporal Cloud or self-hosted with Postgres persistence in Phase 1. Self-hosted
must not share the application Postgres instance.

## Consequences

Positive:

- Crash-safe by construction: a worker restart resumes at the last completed activity.
- Retries, timeouts, and heartbeats are platform features, not our code.
- Visibility into what each investigation is doing, without building an event store.
- Budget counters survive retries, preventing double spend on a retried LLM call (activities
  are idempotent and ledgered in `llm-gateway`).
- Signals give pause/approval/cancel without polling tables.

Negative:

- A significant new operational dependency and concept set (task queues, histories,
  determinism, versioning).
- Workflow code has strict constraints; a careless `time.Now()` or map iteration breaks replay.
- History size limits require `continue_as_new` for very long investigations.
- Activity idempotency is still our responsibility; Temporal retries, it does not dedupe.
- Self-hosting with Cassandra/Postgres adds a backup and upgrade path; Cloud adds cost and a
  data-processing dependency.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Application-managed state machine in Postgres | We would rebuild retries, timers, leases, replay, and visibility — the exact weeks Temporal saves, with subtle bugs |
| AWS Step Functions | Strong durability, but poor fit for a dynamic loop, vendor lock, and awkward local development |
| Inngest / Restate | Promising, but smaller ecosystems and fewer guarantees we can audit today |
| Celery chains | No durable workflow state, no replay, weak cancellation |
| Run investigations as Kafka consumers | Interactive latency and pause/approval semantics do not fit a log; also violates workload isolation |

## Revisit trigger

- Investigation p95 > 10 min and history sizes force frequent `continue_as_new`.
- Temporal Cloud cost exceeds the equivalent engineering time to self-host.
- A workflow requirement cannot be expressed (e.g., cross-workflow transactions) without
  contorting the model.
- Ingestion ever needs durable multi-step orchestration beyond the log (unlikely; that would
  be a separate decision, not a reason to move ingestion to Temporal).
