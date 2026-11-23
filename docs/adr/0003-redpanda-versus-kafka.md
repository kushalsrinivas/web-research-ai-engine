# ADR-0003: Redpanda as the event log (Kafka-compatible)

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering

## Context

The pipeline needs a durable, replayable log that:

- Decouples Go producers from Python consumers.
- Survives consumer outages without losing work.
- Supports multiple independent consumer groups over the same data (`indexer` and
  `graph-builder` both consume `doc.enriched`).
- Preserves per-document ordering downstream and per-host ordering for fetching.
- Lets a small team operate it without a JVM specialist.

The alternative considered seriously was a Postgres queue (`SKIP LOCKED`). At 2–10 docs/s it
would work, but it couples queue contention with the primary database, makes fan-out to two
consumers awkward, and loses replayability without building a second table anyway.

## Decision

Use **Redpanda** as the event log, with the Kafka protocol.

- Client libraries are Kafka clients (`franz-go` in Go, `confluent-kafka`/`aiokafka` in
  Python). No Redpanda-specific API is used.
- `acks=all`, `min.insync.replicas=2`, 3 brokers, 7-day retention.
- Topic layout and keys are fixed in `docs/data/event-schema.md`.
- Redpanda's built-in schema registry is **not** used in Phase 1; JSON Schema in
  `/contracts` is the authority (see `event-schema.md` §1). It remains available if we
  migrate to Avro.

## Consequences

Positive:

- Single binary, no JVM, no ZooKeeper/KRaft controller quirks; local development is one
  container.
- Lower tail latency and simpler disk management than a comparable Kafka cluster.
- Kafka API compatibility means a future move to Kafka (MSK/Confluent) or another compatible
  system is a config change plus a migration window, not a rewrite.
- Built-in tiered storage (optional) keeps broker disks small.

Negative:

- Smaller ecosystem and fewer operators with experience than Kafka.
- **License:** Redpanda is source-available (BSL), not Apache 2.0. Internal use is permitted.
  Offering the platform as a managed streaming service would require review. Kafka is Apache
  2.0 and has no such restriction.
- Some Kafka tooling assumes JVM-era defaults; tuning still requires care.
- Managed Kafka may be cheaper at very small scale because of operational overhead.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| Apache Kafka (KRaft) | Mature and license-clean, but heavier ops and JVM tuning for a small team; keep as the migration target |
| NATS JetStream | Excellent latency and simplicity, weaker ecosystem for durable multi-consumer log semantics and tooling |
| RabbitMQ | A queue, not a replayable log; fan-out and replay are awkward |
| Postgres `SKIP LOCKED` | Viable MVP, but queue contention on the primary, no replay, and fan-out requires a second mechanism |
| Direct HTTP between services | Couples availability; loses backpressure and replay |

## Revisit trigger

- Operations burden exceeds ~2 h/month, or a durability incident occurs.
- Managed Kafka (MSK/Confluent) becomes cheaper than self-hosted Redpanda at our volume.
- A need for Kafka transactions/exactly-once emerges (we do not rely on it; at-least-once plus
  idempotency is sufficient).
- The license model changes or the product is offered as a managed service.
