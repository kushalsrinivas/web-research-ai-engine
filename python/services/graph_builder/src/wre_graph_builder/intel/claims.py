"""Rule-based claim extraction with exact evidence spans and normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import resolver
from .entities import Mention
from .text import (
    Sentence,
    amounts,
    claim_key,
    commitment_value,
    dates,
    modality_value,
    polarity,
)

_TRIGGERS: list[tuple[str, str, str, re.Pattern[str], float]] = [
    (
        "investigation_started",
        "investigation",
        "started_investigation",
        re.compile(r"\b(?:investigat|probe|raid|inquiry|antitrust)\w*", re.I),
        0.8,
    ),
    (
        "acquisition_completed",
        "acquisition",
        "completed_acquisition",
        re.compile(r"\b(?:acquisition|acquire|takeover|merger)\w*", re.I),
        0.85,
    ),
    (
        "construction_start",
        "construction",
        "started_construction",
        re.compile(r"\b(?:construction|groundbreaking|broke ground|inaugurat)\w*", re.I),
        0.85,
    ),
    (
        "product_launch",
        "product",
        "launched_product",
        re.compile(r"\b(?:launch|unveil|introduce|roll out)\w*", re.I),
        0.8,
    ),
    (
        "contract_signed",
        "contract",
        "signed_contract",
        re.compile(r"\b(?:signed|contract|agreement|deal)\b", re.I),
        0.8,
    ),
    (
        "policy_approval",
        "policy",
        "approved_policy",
        re.compile(r"\b(?:approved|approval|cleared|cabinet|policy|scheme)\b", re.I),
        0.85,
    ),
    (
        "investment_announcement",
        "announcement",
        "announced_investment",
        re.compile(r"\b(?:invest|investment|investing|funding|capital expenditure)\w*", re.I),
        0.9,
    ),
    (
        "production_forecast",
        "forecast_intention",
        "production_forecast",
        re.compile(
            r"\b(?:production|manufacturing|output|operations?)\b.*"
            r"\b(?:begin|start|expected|20\d{2})\b",
            re.I,
        ),
        0.8,
    ),
]


@dataclass(slots=True)
class ClaimDraft:
    claim_type: str
    subject_entity_id: str | None
    subject_text: str
    predicate: str
    object_entity_id: str | None
    object_literal: str | None
    object_datatype: str
    polarity: str
    claim_text: str
    quote: str
    char_start: int
    char_end: int
    span_verified: bool
    paragraph_index: int
    sentence_index: int
    modality: str
    commitment_status: str
    temporal_qualifier: str | None
    amount: float | None
    currency: str | None
    amount_original: str | None
    location_text: str | None
    claim_key: str
    normalized_subject: str
    normalized_predicate: str
    normalized_object: str
    extraction_confidence: float
    entity_resolution_confidence: float | None
    claim_support_confidence: float
    verification_confidence: float | None


def extract_claims(
    sentence_list: list[Sentence],
    mentions: list[Mention],
    body: str,
) -> list[ClaimDraft]:
    claims: list[ClaimDraft] = []
    for sentence in sentence_list:
        in_sentence = [
            mention
            for mention in mentions
            if mention.char_start >= sentence.start and mention.char_end <= sentence.end
        ]
        matches = [
            (event_type, claim_type, predicate, base)
            for event_type, claim_type, predicate, pattern, base in _TRIGGERS
            if pattern.search(sentence.text)
        ]
        if not matches:
            continue
        _, claim_type, predicate, base = max(matches, key=lambda item: item[3])
        subject = _subject_for(claim_type, in_sentence)
        sentence_amounts = amounts(sentence.text)
        sentence_location = next(
            (mention for mention in in_sentence if mention.entity_type == "location"), None
        )
        if (
            subject is None
            and not sentence_amounts
            and sentence_location is None
            and not dates(sentence.text)
        ):
            continue
        subject_text = subject.surface if subject else "document"
        subject_id = subject.entity_id if subject else None
        subject_type = subject.entity_type if subject else None
        modality = modality_value(sentence.text)
        commitment = commitment_value(sentence.text)
        polarity_value = polarity(sentence.text)
        confidence = round(min(base + (0.04 if subject else 0.0), 0.97), 4)
        resolution_confidence = (
            round(subject.resolution_confidence, 4)
            if subject and subject.resolution_confidence is not None
            else None
        )
        amount = amounts(sentence.text)
        location = next(
            (mention for mention in in_sentence if mention.entity_type == "location"), None
        )
        date_list = dates(sentence.text)

        main_object = presentation_object(claim_type, predicate)
        claims.append(
            ClaimDraft(
                claim_type=claim_type,
                subject_entity_id=subject_id,
                subject_text=subject_text,
                predicate=predicate,
                object_entity_id=None,
                object_literal=main_object,
                object_datatype="string",
                polarity=polarity_value,
                claim_text=sentence.text,
                quote=sentence.text,
                char_start=sentence.start,
                char_end=sentence.end,
                span_verified=_quote_ok(body, sentence.start, sentence.end, sentence.text),
                paragraph_index=sentence.paragraph_index,
                sentence_index=sentence.sentence_index,
                modality=modality,
                commitment_status=commitment,
                temporal_qualifier=date_list[0].raw if date_list else None,
                amount=amount[0].value if amount else None,
                currency=amount[0].currency if amount else None,
                amount_original=amount[0].raw if amount else None,
                location_text=location.surface if location else None,
                claim_key=claim_key(
                    subject_text,
                    predicate,
                    main_object,
                    date_list[0].raw if date_list else None,
                    amount[0].value if amount else None,
                    amount[0].currency if amount else None,
                ),
                normalized_subject=_normalize(subject_text, subject_type),
                normalized_predicate=predicate,
                normalized_object=_normalize(main_object),
                extraction_confidence=confidence,
                entity_resolution_confidence=resolution_confidence,
                claim_support_confidence=1.0,
                verification_confidence=None,
            )
        )

        if amount:
            first = amount[0]
            quote = sentence.text[first.start : first.end]
            start, end = sentence.start + first.start, sentence.start + first.end
            literal = f"{first.value:.2f}"
            claims.append(
                _sub_claim(
                    sentence=sentence,
                    subject_text=subject_text,
                    subject_id=subject_id,
                    subject_type=subject_type,
                    predicate="has_value",
                    claim_type="financial",
                    object_literal=literal,
                    object_entity_id=None,
                    object_datatype="number",
                    quote=quote,
                    start=start,
                    end=end,
                    body=body,
                    modality=modality,
                    commitment=commitment,
                    temporal=first.unit,
                    amount=first.value,
                    currency=first.currency,
                    amount_original=first.raw,
                    location_text=location.surface if location else None,
                    confidence=0.92,
                    resolution_confidence=resolution_confidence,
                )
            )

        if location:
            quote = location.surface
            start, end = location.char_start, location.char_end
            claims.append(
                _sub_claim(
                    sentence=sentence,
                    subject_text=subject_text,
                    subject_id=subject_id,
                    subject_type=subject_type,
                    predicate="located_in",
                    claim_type="location",
                    object_literal=None if location.entity_id else location.surface,
                    object_entity_id=location.entity_id,
                    object_datatype="string",
                    quote=quote,
                    start=start,
                    end=end,
                    body=body,
                    modality=modality,
                    commitment=commitment,
                    temporal=None,
                    amount=None,
                    currency=None,
                    amount_original=None,
                    location_text=location.surface,
                    confidence=0.9,
                    resolution_confidence=(location.resolution_confidence or 0.6),
                )
            )

        if date_list:
            date_match = date_list[0]
            quote = sentence.text[date_match.start : date_match.end]
            start, end = sentence.start + date_match.start, sentence.start + date_match.end
            predicate = (
                "expected_production_start"
                if _production_sentence(sentence.text)
                else "occurred_on"
            )
            claims.append(
                _sub_claim(
                    sentence=sentence,
                    subject_text=subject_text,
                    subject_id=subject_id,
                    subject_type=subject_type,
                    predicate=predicate,
                    claim_type="forecast_intention"
                    if modality in {"expected", "planned", "proposed"}
                    else "general",
                    object_literal=date_match.value.date().isoformat(),
                    object_entity_id=None,
                    object_datatype="date",
                    quote=quote,
                    start=start,
                    end=end,
                    body=body,
                    modality=modality,
                    commitment=commitment,
                    temporal=date_match.raw,
                    amount=None,
                    currency=None,
                    amount_original=None,
                    location_text=location.surface if location else None,
                    confidence=0.85,
                    resolution_confidence=resolution_confidence,
                )
            )

        if len(claims) >= 30:
            break
    return _dedupe(claims)[:30]


def _sub_claim(
    *,
    sentence: Sentence,
    subject_text: str,
    subject_id: str | None,
    subject_type: str | None,
    predicate: str,
    claim_type: str,
    object_literal: str | None,
    object_entity_id: str | None,
    object_datatype: str,
    quote: str,
    start: int,
    end: int,
    body: str,
    modality: str,
    commitment: str,
    temporal: str | None,
    amount: float | None,
    currency: str | None,
    amount_original: str | None,
    location_text: str | None,
    confidence: float,
    resolution_confidence: float | None,
) -> ClaimDraft:
    key = claim_key(
        subject_text,
        predicate,
        object_literal or object_entity_id or "",
        temporal,
        amount,
        currency,
    )
    return ClaimDraft(
        claim_type=claim_type,
        subject_entity_id=subject_id,
        subject_text=subject_text,
        predicate=predicate,
        object_entity_id=object_entity_id,
        object_literal=object_literal,
        object_datatype=object_datatype,
        polarity="asserts",
        claim_text=quote,
        quote=quote,
        char_start=start,
        char_end=end,
        span_verified=_quote_ok(body, start, end, quote),
        paragraph_index=sentence.paragraph_index,
        sentence_index=sentence.sentence_index,
        modality=modality,
        commitment_status=commitment,
        temporal_qualifier=temporal,
        amount=amount,
        currency=currency,
        amount_original=amount_original,
        location_text=location_text,
        claim_key=key,
        normalized_subject=_normalize(subject_text, subject_type),
        normalized_predicate=predicate,
        normalized_object=_normalize(object_literal or object_entity_id or ""),
        extraction_confidence=round(confidence, 4),
        entity_resolution_confidence=resolution_confidence,
        claim_support_confidence=1.0 if _quote_ok(body, start, end, quote) else 0.0,
        verification_confidence=None,
    )


def presentation_object(claim_type: str, predicate: str) -> str:
    return {
        "announcement": "investment",
        "policy": "policy approval",
        "construction": "construction start",
        "acquisition": "acquisition",
        "product": "product launch",
        "contract": "contract",
        "investigation": "investigation",
    }.get(claim_type, predicate.replace("_", " "))


def _subject_for(claim_type: str, mentions: list[Mention]) -> Mention | None:
    preferred = (
        ("government", "organization", "person")
        if claim_type in {"policy", "investigation"}
        else ("organization", "government", "person", "project")
    )
    for entity_type in preferred:
        for mention in mentions:
            if mention.entity_type == entity_type and mention.entity_id:
                return mention
    for mention in mentions:
        if mention.entity_id and mention.entity_type not in {
            "location",
            "industry",
            "financial_instrument",
        }:
            return mention
    return None


def _normalize(value: str, entity_type: str | None = None) -> str:
    return resolver.normalize_name(value, entity_type or "other") or value.casefold()


def _quote_ok(body: str, start: int, end: int, quote: str) -> bool:
    return 0 <= start < end <= len(body) and body[start:end] == quote


def _production_sentence(text: str) -> bool:
    lower = text.casefold()
    return any(
        word in lower for word in ("production", "manufacturing", "facility", "plant", "output")
    )


def _dedupe(claims: list[ClaimDraft]) -> list[ClaimDraft]:
    seen: set[tuple[str, int, int]] = set()
    result: list[ClaimDraft] = []
    for claim in claims:
        key = (claim.predicate, claim.char_start, claim.char_end)
        if key in seen:
            continue
        seen.add(key)
        result.append(claim)
    return result
