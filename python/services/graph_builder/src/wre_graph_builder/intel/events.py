"""Rule-based event extraction from sentences and resolved mentions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .entities import Mention
from .text import Sentence, amounts, commitment_value, dates, modality_value

_TRIGGERS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "investigation_started",
        re.compile(r"\b(?:investigat|probe|raid|inquiry|antitrust)\w*", re.I),
        0.8,
    ),
    (
        "acquisition_completed",
        re.compile(r"\b(?:acquisition|acquire|takeover|merger)\w*", re.I),
        0.85,
    ),
    (
        "construction_start",
        re.compile(r"\b(?:construction|groundbreaking|broke ground|inaugurat)\w*", re.I),
        0.85,
    ),
    ("product_launch", re.compile(r"\b(?:launch|unveil|introduce|roll out)\w*", re.I), 0.8),
    ("contract_signed", re.compile(r"\b(?:signed|contract|agreement|deal)\b", re.I), 0.8),
    (
        "policy_approval",
        re.compile(r"\b(?:approved|approval|cleared|cabinet|policy|scheme)\b", re.I),
        0.85,
    ),
    (
        "investment_announcement",
        re.compile(r"\b(?:invest|investment|investing|funding|capital expenditure)\w*", re.I),
        0.9,
    ),
    (
        "production_forecast",
        re.compile(r"\b(?:production|manufacturing)\b.*\b(?:begin|start|expected|20\d{2})\b", re.I),
        0.75,
    ),
]
_SKIP_EVENT_TYPES = {"production_forecast"}


@dataclass(slots=True)
class EventDraft:
    event_type: str
    subject_entity_id: str | None
    object_entity_id: str | None
    location_text: str | None
    event_time: datetime | None
    event_time_precision: str
    amount: float | None
    currency: str | None
    amount_original: str | None
    commitment_status: str
    modality: str
    description: str
    quote: str
    char_start: int
    char_end: int
    paragraph_index: int
    sentence_index: int
    confidence: float
    extraction_confidence: float
    event_classification_confidence: float
    entity_resolution_confidence: float | None
    evidence_document_ids: list[str]


def extract_events(
    sentence_list: list[Sentence],
    mentions: list[Mention],
    published_at: datetime | None,
) -> list[EventDraft]:
    events: list[EventDraft] = []
    for sentence in sentence_list:
        in_sentence = [
            mention
            for mention in mentions
            if mention.char_start >= sentence.start and mention.char_end <= sentence.end
        ]
        if not in_sentence:
            continue
        matches = [
            (event_type, base)
            for event_type, pattern, base in _TRIGGERS
            if event_type not in _SKIP_EVENT_TYPES and pattern.search(sentence.text)
        ]
        if not matches:
            continue
        event_type, base = max(matches, key=lambda item: item[1])
        subject = _subject_for(event_type, in_sentence)
        obj = _object_for(subject, in_sentence)
        location = next(
            (mention for mention in in_sentence if mention.entity_type == "location"), None
        )
        amount_matches = amounts(sentence.text)
        date_matches = dates(sentence.text)
        amount = amount_matches[0] if amount_matches else None
        date = date_matches[0] if date_matches else None
        if subject is None and amount is None and location is None and date is None:
            continue

        event_time = date.value if date else published_at
        precision = date.precision if date else ("day" if published_at else "unknown")
        if event_time is not None and event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        confidence = base
        if subject is not None:
            confidence += 0.04
        if amount is not None or location is not None:
            confidence += 0.02
        confidence = round(min(confidence, 0.97), 4)
        events.append(
            EventDraft(
                event_type=event_type,
                subject_entity_id=subject.entity_id if subject else None,
                object_entity_id=obj.entity_id if obj else None,
                location_text=location.surface if location else None,
                event_time=event_time,
                event_time_precision=precision,
                amount=amount.value if amount else None,
                currency=amount.currency if amount else None,
                amount_original=amount.raw if amount else None,
                commitment_status=commitment_value(sentence.text),
                modality=modality_value(sentence.text),
                description=sentence.text,
                quote=sentence.text,
                char_start=sentence.start,
                char_end=sentence.end,
                paragraph_index=sentence.paragraph_index,
                sentence_index=sentence.sentence_index,
                confidence=confidence,
                extraction_confidence=confidence,
                event_classification_confidence=round(base, 4),
                entity_resolution_confidence=(
                    round(subject.resolution_confidence, 4)
                    if subject and subject.resolution_confidence is not None
                    else None
                ),
                evidence_document_ids=[],
            )
        )
        if len(events) >= 20:
            break
    return events


def _subject_for(event_type: str, mentions: list[Mention]) -> Mention | None:
    preferred = (
        ("government", "organization", "person")
        if event_type == "policy_approval"
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


def _object_for(subject: Mention | None, mentions: list[Mention]) -> Mention | None:
    for mention in mentions:
        if subject is not None and mention is subject:
            continue
        if mention.entity_id and mention.entity_type in {
            "organization",
            "government",
            "project",
            "policy",
        }:
            return mention
    return None
