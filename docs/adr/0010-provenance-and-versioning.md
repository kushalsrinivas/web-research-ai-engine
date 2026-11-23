# ADR-0010: Provenance and document versioning

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering, product

## Context

The product's promise is that every statement is traceable to a source, a timestamp, and a
verbatim span. That means:

- The bytes we fetched must never change after the fact.
- Re-extraction must not overwrite the evidence that produced an earlier claim.
- A citation from six months ago must resolve to the same revision it referenced.
- Legal takedowns must remove content without rewriting history.
- Reprocessing (new parser, new model) must be possible without refetching the web.

The rejected alternative is the common one: one mutable `documents` row with
`processed=true`, `content` rewritten on each fetch, and derived data overwritten in place.
It is simpler to build and impossible to audit.

## Decision

**Immutable artifacts + append-only events + explicit revisions.**

1. **Content addressing.** Raw artifacts are immutable and keyed by SHA-256. Re-fetching
   identical bytes is a no-op; different bytes produce a new artifact.
2. **Revisions.** `revision` increments only when normalized content changes.
   `(document_id, revision)` is the content version. Every derived row (`document_text`,
   chunks, chunks' embeddings, claims, mentions, edges) carries `document_id` and `revision`.
3. **Append-only lifecycle.** `core.document_events` is insert-only, with timestamp, attempt,
   processor version, trace id, artifact reference, and error. No `processed` boolean.
4. **Version stamps.** Every derived row carries the `processor_version` that produced it
   (parser, normalizer, chunker, model, prompt). Reprocessing writes new rows and supersedes
   old ones; it never mutates them.
5. **Claims are immutable.** A correction creates a new claim and sets `superseded_by` on the
   old one. A retraction sets `status='retracted'` with a reason; the row stays.
6. **Deletion is tombstoning.** Legal removal clears text and artifacts, retains the document
   identity, event history, and reason, and retracts dependent claims. Provenance is never
   silently rewritten.
7. **Citation resolution is pinned.** API responses include `document_id`, `revision`, and
   span. Reports store the same. A citation never floats to "latest".

## Consequences

Positive:

- Every claim can be re-verified against the exact bytes and extractor version that produced
  it.
- Model and parser changes are auditable A/B experiments, not irreversible overwrites.
- Legal and compliance questions ("what did we know on date X?") are answerable.
- Re-parsing and re-enrichment are cheap because raw artifacts are retained.
- Debugging a bad extraction is possible: replay the artifact with the old version.

Negative:

- Storage grows with revisions and versions; requires retention policy and GC (raw 12 months,
  derived 12 months, metadata indefinite).
- Queries must choose the active version; "latest" is a deliberate join, not `SELECT *`.
- More rows and more indexes; write amplification on re-enrichment.
- The pipeline library must enforce the contract; bypassing it corrupts provenance.
- Tombstoning is more complex than `DELETE`, and downstream caches must be invalidated.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Mutable document row with `processed` flag | Cannot audit, cannot reproduce, overwrites evidence — rejected explicitly in Phase 0 |
| Git-like content store in Postgres | Reinvents object storage; poor fit for large binaries |
| Event sourcing the entire corpus | Correct in spirit, operationally heavy; we use events for lifecycle and revisions for content, which is proportional |
| Store only latest revision | Cheapest, destroys historical citations; unacceptable |
| External web archive links for provenance | Third-party dependency, link rot, no guarantee of identical bytes |

## Enforcement

- `core.document_events` DB grants: `INSERT` only for application roles.
- `core.document_revisions`: update limited to `superseded_by`.
- Pipeline libraries are the only sanctioned write path; direct state updates fail review.
- A nightly integrity job samples 1% of artifacts and verifies SHA-256 and claim spans.
- CI includes a replay test: reprocess a captured transcript and assert convergence.

## Revisit trigger

- Storage cost for retained revisions exceeds budget → tier raw artifacts colder, never
  change the semantics.
- A regulator requires hard deletion (not tombstoning) → keep an approval-gated purge path
  that records a deletion certificate, and document the loss of provenance.
- Revision count per document exceeds ~20 in a year (high-churn pages) → add revision
  compaction for derived artifacts while keeping raw hashes and events.
