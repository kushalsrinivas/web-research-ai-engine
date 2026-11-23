# Claim Schema

Status: Phase 0 baseline. Owned by `graph-builder`. A claim is the smallest unit of
evidence-bearing knowledge in the system.

## 1. Why claims exist

Search finds documents. Research needs assertions with sources. A claim is a normalized
statement extracted from one document at one span, resolvable to entities, comparable across
documents, and impossible to store without its provenance.

Hard rule: **a claim without a document span and a verbatim quote is not a claim** and is
discarded at write time.

## 2. DDL

```sql
create schema graph;

create type graph.polarity as enum ('asserts', 'denies');
create type graph.claim_status as enum ('active', 'superseded', 'retracted', 'unverified');
create type graph.claim_relation as enum ('supports', 'contradicts', 'refines', 'duplicates');

create table graph.claims (
  claim_id           text primary key,                 -- ULID
  tenant_id          text not null default 'public',

  subject_entity_id  text references graph.entities(entity_id),
  subject_text       text not null,                    -- surface form, always kept
  predicate          text not null,                    -- controlled vocabulary, see §3
  object_entity_id   text references graph.entities(entity_id),
  object_literal     text,                             -- when the object is not an entity
  object_datatype    text,                             -- 'string'|'date'|'number'|'quantity'|'url'
  polarity           graph.polarity not null default 'asserts',
  claim_text         text not null,                    -- normalized sentence, no quotes

  quote              text not null,                    -- verbatim span from the source
  document_id        text not null,
  revision           integer not null,
  content_hash       bytea not null,                   -- revision hash at extraction time
  char_start         integer not null,
  char_end           integer not null,
  span_verified      boolean not null default false,   -- quote matched at write time

  confidence         real not null check (confidence >= 0 and confidence <= 1),
  extractor_version  text not null,
  model_id           text,
  status             graph.claim_status not null default 'active',
  superseded_by      text references graph.claims(claim_id),
  retracted_reason   text,
  created_at         timestamptz not null default now(),

  constraint object_exactly_one check (
    (object_entity_id is not null)::int + (object_literal is not null)::int = 1
  ),
  constraint span_order check (char_start >= 0 and char_end > char_start),
  constraint quote_present check (length(quote) > 0),
  constraint verified_quote check (span_verified or status = 'unverified'),
  constraint supersede_consistency check (
    (status = 'superseded') = (superseded_by is not null)
  )
);

create index claims_subject on graph.claims (subject_entity_id, predicate, status);
create index claims_object_entity on graph.claims (object_entity_id, status);
create index claims_document on graph.claims (document_id, revision, status);
create index claims_predicate on graph.claims (predicate, status);
```

```sql
create table graph.claim_relations (
  relation_id      bigserial primary key,
  from_claim_id    text not null references graph.claims(claim_id),
  to_claim_id      text not null references graph.claims(claim_id),
  relation         graph.claim_relation not null,
  confidence       real not null,
  method           text not null,          -- 'same_span' | 'entity_match' | 'llm' | 'operator'
  created_at       timestamptz not null default now(),
  unique (from_claim_id, to_claim_id, relation),
  constraint no_self_relation check (from_claim_id <> to_claim_id)
);
```

## 3. Predicate vocabulary (v1)

Closed set, versioned in `contracts/predicates.v1.yaml`. Extraction must map to one of these
or emit `related_to` with low confidence.

| Group | Predicates |
|---|---|
| Identity | `is_a`, `alias_of`, `part_of`, `has_part` |
| People | `works_for`, `founded`, `founded_by`, `board_member_of`, `authored`, `born_in`, `died_in` |
| Organizations | `acquired`, `acquired_by`, `merged_with`, `subsidiary_of`, `competes_with`, `partner_of` |
| Products/tech | `developed_by`, `released_on`, `uses`, `depends_on`, `compatible_with`, `supersedes` |
| Places | `located_in`, `headquartered_in`, `operates_in` |
| Publishing | `published_on`, `published_by`, `cites`, `responds_to` |
| Dates/quantities | `occurred_on`, `has_value`, `has_count` |
| Fallback | `related_to` (requires confidence ≥ 0.6 and is down-ranked in retrieval) |

Rules:

- `predicate` is lowercase snake_case; no free text.
- Direction is defined by the predicate, not by sentence order. `A acquired B` and
  `B acquired_by A` are the same fact; extraction normalizes to one direction per predicate.
- Dates in `object_literal` are ISO 8601. Numbers carry `object_datatype='number'` and no
  units in the literal (units go in `claim_text` until a quantity type exists).

## 4. Write-time validation

`graph-builder` must, in order:

1. Load `document_text.body` for `(document_id, revision)`.
2. Verify `body[char_start:char_end] == quote`; if not, set `status='unverified'` and do not
   link the claim to retrieval. This is a defect signal, not a retryable failure.
3. Resolve subject and object through `graph-builder`'s resolver. Unresolved subjects keep
   `subject_entity_id = null` and are indexed by `subject_text`.
4. Reject a claim that duplicates an existing `(document_id, revision, subject, predicate,
   object, char_start)` with the same extractor version.
5. Insert; other extracted claims for the same revision are marked `superseded` only when
   produced by a **newer** extractor version.

No claim is ever updated in place. Correction = new row + `superseded_by` on the old one.

## 5. Contradiction handling

The system never silently chooses a truth. A contradiction is a query, not a mutation:

```sql
-- active claims about the same subject+predicate with different objects
select subject_entity_id, predicate,
       array_agg(distinct coalesce(object_entity_id, object_literal)) as values,
       count(distinct coalesce(object_entity_id, object_literal)) as n
from graph.claims
where status = 'active' and subject_entity_id = $1
group by subject_entity_id, predicate
having count(distinct coalesce(object_entity_id, object_literal)) > 1;
```

- Research responses present conflicting claims with source recency, source count, and
  confidence; the synthesis layer may state that sources disagree, never that one is correct.
- `polarity='denies'` is kept separate from a missing claim. "X denies Y" is not "not Y".
- A claim retracted by legal takedown becomes `status='retracted'` with a reason; dependent
  citations are excluded, and the event remains.

## 6. Confidence

`confidence` is the extractor's calibrated probability that the claim faithfully represents
the source sentence. It is **not** truth confidence.

Calibration: monthly check against a hand-labeled sample of 200 claims per extractor version;
if expected calibration error > 0.1, the version is not promoted. Stored confidence is used
for ranking only after calibration; uncalibrated versions are flagged.

Source trust is a separate, query-time signal (host allowlist, recency, corroboration count)
and is never baked into the claim.

## 7. Indexing and retrieval

- Claim search uses `subject_entity_id` + `predicate` + `object_*`, plus trigram over
  `claim_text` for recall.
- Every claim carries `document_id`, `revision`, `char_start`, `char_end`; the API always
  returns them. A claim response without a resolvable source is a bug.
- Aggregations (how many sources assert X) are computed from `status='active'` and
  `span_verified=true` only.

## 8. Volume and growth

| Dimension | Phase 1 target |
|---|---|
| Claims per document | 0–20, p95 8 |
| Active claims | 40M at 5M documents |
| Extraction cost | ≤ $0.004 per document at batch rates |
| Claim write throughput | 100/s peak |

If active claims exceed 100M, partition `graph.claims` by `subject_entity_id` hash before
considering a graph database (see ADR-0007).
