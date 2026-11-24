# Entity, event, and claim extraction

Phase 6 turns `core.document_text` into structured intelligence. Extraction runs in
`graph-builder` (port 8087) after ETL and dedup; every object carries its source document,
revision, exact span, extraction version, and separate confidence fields.

## Orchestration

```
document text
  → language
  → mentions (rules + gazetteers + spaCy en_core_web_sm)
  → entity resolution (Phase 5 resolver)
  → events
  → claims
  → span alignment
  → JSON Schema validation
  → persistence (graph.mentions / events / claims / edges)
  → quarantine for invalid objects
```

Each document gets a `graph.extraction_runs` row. A run is `done`, `partial` (some objects
quarantined), or `failed`. The latest successful run feeds the `graph.live_claims` and
`graph.live_events` views; older runs remain for audit.

## Entity mentions

Supported types: person, organization, government, location, product, industry, technology,
financial instrument, project, policy, event, work, concept, other.

Extraction combines:

- **Rules**: ministries/departments/authorities, legal-name suffixes, person titles, named
  projects/facilities, policies/schemes/acts, acronyms.
- **Gazetteers**: countries, Indian states and major cities, industry/technology/product/
  financial-instrument terms.
- **NLP**: spaCy `en_core_web_sm` for PERSON, ORG, GPE/LOC, PRODUCT, EVENT, LAW.
- **Resolution**: every mention is passed through the Phase 5 canonical entity resolver.
  Auto-matches attach to an existing entity; ambiguous names create a separate entity and
  remain in `graph.entity_match_candidates` for review.

Mentions are stored in `graph.entity_mentions` and `mentions` edges
(document → entity). Confidence is the resolution confidence, not a truth score.

## Events

`graph.events` stores structured events with:

- type (`investment_announcement`, `policy_approval`, `construction_start`,
  `acquisition_completed`, `product_launch`, `contract_signed`, `investigation_started`)
- subject and object entity IDs, location
- normalized `amount` (numeric) plus `currency`, `amount_original`, and original text
- `event_time` with `event_time_precision`
- `commitment_status` and `modality`
- exact quote/span and paragraph/sentence location
- `confidence`, `extraction_confidence`, `event_classification_confidence`,
  `entity_resolution_confidence`, `verification_confidence`
- `extractor_version`, `model_id`, `evidence_document_ids`

Amounts parse ₹/Rs/INR/USD/EUR/GBP with crore/lakh/million/billion/trillion multipliers.
An announced investment is stored as `commitment_status = announced|planned`, never as
committed capital; approved/funded/under_construction/operational/completed are distinct.

## Claims

`graph.claims` follows the Phase 0 claim contract: subject/object, controlled predicate,
verbatim `quote`, `char_start`/`char_end`, `span_verified`, document and revision,
`content_hash`, and a single object (entity XOR literal). Added fields:

- `claim_type` (announcement, financial, location, forecast_intention, policy, construction,
  acquisition, product, contract, investigation, general)
- `modality` (confirmed, announced, planned, proposed, expected, alleged, estimated)
- `commitment_status` (announced, planned, proposed, approved, funded, under_construction,
  operational, completed, expected, alleged, estimated)
- `claim_key` and normalized subject/predicate/object for cross-source grouping
- `amount`, `currency`, `amount_original`, `location_text`, `temporal_qualifier`
- `extraction_confidence`, `entity_resolution_confidence`, `claim_support_confidence`,
  `verification_confidence`

The example sentence produces announcement, financial, location, and forecast claims. Each
claim's `quote` is verified against `document_text.body` at write time; a mismatch is
quarantined.

Claim normalization groups equivalent statements by `claim_key` while keeping modality and
commitment status on every row. Proposed, announced, approved, funded, under construction,
operational, and completed are never collapsed.

## Confidence separation

| Field | Meaning |
|---|---|
| `extraction_confidence` | extractor's confidence in the span/parse |
| `entity_resolution_confidence` | confidence in subject/object resolution |
| `event_classification_confidence` | confidence in the event type |
| `claim_support_confidence` | span alignment/support (1.0 for exact quotes) |
| `verification_confidence` | reserved for cross-source verification; null today |

A model can confidently extract an incorrect statement. Extraction confidence is never
treated as factual truth.

## Validation and quarantine

`contracts/intelligence/v1/` contains JSON Schemas for mentions, events, and claims. Every
object is validated before persistence. Invalid objects are written to
`graph.extraction_quarantine` with stage, reason, and payload, and are excluded from the
trusted tables. LLM output, when used, must pass `intel.llm.parse_and_validate`, which
repairs fenced/prose-wrapped JSON, validates, and returns a quarantine decision instead of
persisting malformed output. The deterministic extractor does not require an LLM.

Runs record `llm_tokens_in`, `llm_tokens_out`, and `llm_cost_usd` so a future LLM stage can
report cost without a schema change.

## API

All endpoints are on `http://127.0.0.1:8087/internal/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/intel/stats` | claims, events, mentions, runs, quarantine |
| POST | `/intel/scan` | enqueue documents with text |
| POST | `/intel/extract` | enqueue one document or scan |
| GET | `/intel/documents/{id}` | document, run, mentions, events, claims, quarantine |
| GET | `/intel/claims` | filters: subject_entity_id, claim_key, claim_type |
| GET | `/intel/claim-groups` | normalized claim groups across documents |
| GET | `/intel/events` | filters: event_type, subject_entity_id |
| GET | `/intel/versions` | extraction versions |
| GET | `/intel/quarantine` | rejected objects |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `EXTRACTION_CONCURRENCY` | 2 | documents extracted in parallel |
| `EXTRACTION_DISPATCH_INTERVAL_SECONDS` | 5 | dispatcher interval |
| `EXTRACTION_POLL_INTERVAL_SECONDS` | 1 | worker claim interval |
| `EXTRACTION_LEASE_SECONDS` | 300 | job lease |
| `EXTRACTION_MAX_ATTEMPTS` | 3 | infrastructure retry budget |
| `MAX_EXTRACT_CHARS` | 200000 | text prefix processed per document |

## Limits

- Coreference is not resolved ("the company" is not linked to a prior entity).
- Claim predicates are a controlled extension of the Phase 0 vocabulary; `related_to` is the
  fallback for unsupported patterns.
- The LLM enrichment adapter is optional and awaits the `llm-gateway` contract; the
  repair/validation/quarantine utilities are implemented and tested.
- Extraction writes Postgres and graph edges. Kafka `doc.*` publication remains a later
  integration; the run audit and `graph.edges` are the durable outputs today.
