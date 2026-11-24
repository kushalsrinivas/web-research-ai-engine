from __future__ import annotations

from datetime import UTC, datetime

from wre_graph_builder.intel import claims, entities, events, llm, schemas
from wre_graph_builder.intel.text import amounts, commitment_value, dates, sentences

ARTICLE = (
    "Tata Electronics announced plans to invest ₹15,000 crore in a semiconductor facility "
    "in Gujarat. The facility is expected to begin production in 2028, subject to regulatory "
    "approvals and the availability of water and power infrastructure.\n\n"
    "The Ministry of Electronics and Information Technology approved the project under the "
    "semiconductor policy. Officials said the project would create several thousand direct "
    "and indirect jobs and support the local supplier base over the next decade."
)


def _resolved_mentions(text: str) -> list[entities.Mention]:
    mentions = entities.extract_mentions(text, "en")
    for mention in mentions:
        mention.entity_id = f"e-{mention.entity_type}"
        mention.resolution_confidence = 0.9
    return mentions


def test_amounts_and_dates_normalize() -> None:
    found = amounts("The company will invest ₹15,000 crore and $2 billion by 2028.")
    assert len(found) == 2
    assert found[0].value == 150_000_000_000
    assert found[0].currency == "INR"
    assert found[1].value == 2_000_000_000
    assert found[1].currency == "USD"
    years = dates("Production is expected in 2028, with a review in March 2029.")
    assert [item.raw for item in years] == ["2028", "March 2029"]
    assert years[0].precision == "year"
    assert years[1].precision == "month"


def test_mentions_cover_named_entities_and_spans() -> None:
    mentions = _resolved_mentions(ARTICLE)
    surfaces = {(mention.surface, mention.entity_type) for mention in mentions}
    assert ("Tata Electronics", "organization") in surfaces
    assert ("Gujarat", "location") in surfaces
    assert (
        "Ministry of Electronics and Information Technology",
        "government",
    ) in surfaces
    assert ("semiconductor facility", "project") in surfaces
    for mention in mentions:
        assert ARTICLE[mention.char_start : mention.char_end] == mention.surface


def test_events_are_structured() -> None:
    mentions = _resolved_mentions(ARTICLE)
    drafts = events.extract_events(sentences(ARTICLE), mentions, datetime(2026, 11, 20, tzinfo=UTC))
    by_type = {draft.event_type: draft for draft in drafts}
    investment = by_type["investment_announcement"]
    assert investment.amount == 150_000_000_000
    assert investment.currency == "INR"
    assert investment.location_text == "Gujarat"
    assert investment.subject_entity_id == "e-organization"
    assert investment.commitment_status in {"planned", "announced"}
    policy = by_type["policy_approval"]
    assert policy.subject_entity_id == "e-government"
    assert policy.commitment_status == "approved"
    for draft in drafts:
        assert ARTICLE[draft.char_start : draft.char_end] == draft.quote


def test_claims_have_exact_spans_and_separate_confidence() -> None:
    mentions = _resolved_mentions(ARTICLE)
    drafts = claims.extract_claims(sentences(ARTICLE), mentions, ARTICLE)
    predicates = {draft.predicate for draft in drafts}
    assert {"announced_investment", "has_value", "located_in", "production_forecast"} <= predicates
    amount_claim = next(draft for draft in drafts if draft.predicate == "has_value")
    assert amount_claim.amount == 150_000_000_000
    assert amount_claim.currency == "INR"
    assert amount_claim.claim_type == "financial"
    location_claim = next(draft for draft in drafts if draft.predicate == "located_in")
    assert location_claim.object_entity_id == "e-location"
    assert location_claim.object_literal is None
    for draft in drafts:
        assert ARTICLE[draft.char_start : draft.char_end] == draft.quote
        assert draft.span_verified is True
        assert 0 <= draft.extraction_confidence <= 1
        assert draft.verification_confidence is None


def test_claim_and_event_contracts() -> None:
    mentions = _resolved_mentions(ARTICLE)
    claim = claims.extract_claims(sentences(ARTICLE), mentions, ARTICLE)[0]
    claim_payload = {
        "document_id": "doc",
        "revision": 1,
        "claim_type": claim.claim_type,
        "subject_text": claim.subject_text,
        "predicate": claim.predicate,
        "object_literal": claim.object_literal,
        "object_entity_id": claim.object_entity_id,
        "claim_text": claim.claim_text,
        "quote": claim.quote,
        "char_start": claim.char_start,
        "char_end": claim.char_end,
        "span_verified": claim.span_verified,
        "modality": claim.modality,
        "commitment_status": claim.commitment_status,
        "extraction_confidence": claim.extraction_confidence,
        "claim_support_confidence": claim.claim_support_confidence,
        "extractor_version": "extract-1.0.0",
    }
    assert schemas.valid("claim", claim_payload)

    event = events.extract_events(sentences(ARTICLE), mentions, None)[0]
    event_payload = {
        "document_id": "doc",
        "revision": 1,
        "event_type": event.event_type,
        "commitment_status": event.commitment_status,
        "modality": event.modality,
        "description": event.description,
        "quote": event.quote,
        "char_start": event.char_start,
        "char_end": event.char_end,
        "confidence": event.confidence,
        "extraction_confidence": event.extraction_confidence,
        "event_classification_confidence": event.event_classification_confidence,
        "extractor_version": "extract-1.0.0",
    }
    assert schemas.valid("event", event_payload)
    event_payload["quote"] = ""
    assert not schemas.valid("event", event_payload)


def test_llm_repair_and_validation() -> None:
    raw = '```json\n{"document_id":"doc"}\n```'
    repaired = llm.repair_json(raw)
    assert repaired == {"document_id": "doc"}
    value, errors = llm.parse_and_validate("not json", "claim")
    assert value is None and errors
    value, errors = llm.parse_and_validate(
        '{"document_id":"doc","revision":1,"claim_type":"financial","subject_text":"A",'
        '"predicate":"has_value","claim_text":"A has 1","quote":"A has 1","char_start":0,'
        '"char_end":7,"span_verified":true,"modality":"confirmed",'
        '"commitment_status":"announced","extraction_confidence":0.9,'
        '"claim_support_confidence":1.0,"extractor_version":"extract-1.0.0"}',
        "claim",
    )
    assert value is not None and not errors


def test_modality_distinguishes_states() -> None:
    assert commitment_value("The government approved the policy") == "approved"
    assert commitment_value("The company plans to invest") == "planned"
    assert commitment_value("Production is expected in 2028") == "expected"
    assert commitment_value("Construction began last week") == "under_construction"
