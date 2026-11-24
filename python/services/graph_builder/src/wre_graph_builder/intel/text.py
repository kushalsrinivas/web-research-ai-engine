"""Deterministic text utilities for extraction: spans, amounts, dates, modality."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

_AMOUNT = re.compile(
    r"(?P<currency>₹|Rs\.?|INR|USD|\$|EUR|€|GBP|£)\s*"
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>crore|lakh|million|billion|trillion|mn|bn)?",
    re.IGNORECASE,
)
_AMOUNT_UNIT = re.compile(
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>crore|lakh|million|billion|trillion)\b(?!\s*(?:rupees|dollars))",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(?:"
    r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|20\d{2}"
    r")\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r".+?(?:[.!?](?=\s|$)|$)", re.DOTALL)
_PARAGRAPH = re.compile(r"[^\n]+(?:\n(?!\n)[^\n]+)*")

_UNITS = {
    "crore": 10_000_000,
    "lakh": 100_000,
    "million": 1_000_000,
    "mn": 1_000_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}
_CURRENCIES = {
    "₹": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

CONFIRMED = "confirmed"
ANNOUNCED = "announced"
PLANNED = "planned"
PROPOSED = "proposed"
EXPECTED = "expected"
ALLEGED = "alleged"
ESTIMATED = "estimated"

COMMITMENTS = {
    ANNOUNCED,
    PLANNED,
    PROPOSED,
    "approved",
    "funded",
    "under_construction",
    "operational",
    "completed",
    EXPECTED,
    ALLEGED,
    ESTIMATED,
}


@dataclass(frozen=True, slots=True)
class Sentence:
    start: int
    end: int
    text: str
    paragraph_index: int
    sentence_index: int


@dataclass(frozen=True, slots=True)
class AmountMatch:
    start: int
    end: int
    raw: str
    value: float
    currency: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class DateMatch:
    start: int
    end: int
    raw: str
    value: datetime
    precision: str


def sentences(text: str) -> list[Sentence]:
    result: list[Sentence] = []
    for paragraph_index, paragraph in enumerate(_PARAGRAPH.finditer(text)):
        block = paragraph.group(0)
        for sentence_index, match in enumerate(_SENTENCE.finditer(block)):
            raw = match.group(0)
            stripped = raw.strip()
            if not stripped:
                continue
            start = paragraph.start() + match.start() + (len(raw) - len(raw.lstrip()))
            end = start + len(stripped)
            result.append(Sentence(start, end, stripped, paragraph_index, sentence_index))
    return result


def amounts(text: str) -> list[AmountMatch]:
    found: list[AmountMatch] = []
    used: list[tuple[int, int]] = []
    for match in _AMOUNT.finditer(text):
        value = _number(match.group("value"))
        if value is None:
            continue
        unit = (match.group("unit") or "").casefold() or None
        currency = _CURRENCIES.get(match.group("currency").casefold())
        found.append(
            AmountMatch(
                match.start(),
                match.end(),
                match.group(0),
                round(value * _UNITS.get(unit or "", 1), 2),
                currency,
                unit,
            )
        )
        used.append((match.start(), match.end()))
    for match in _AMOUNT_UNIT.finditer(text):
        if any(start <= match.start() < end for start, end in used):
            continue
        value = _number(match.group("value"))
        if value is None:
            continue
        unit = match.group("unit").casefold()
        found.append(
            AmountMatch(
                match.start(),
                match.end(),
                match.group(0),
                round(value * _UNITS.get(unit, 1), 2),
                None,
                unit,
            )
        )
    found.sort(key=lambda item: item.start)
    return found


def dates(text: str) -> list[DateMatch]:
    result: list[DateMatch] = []
    for match in _DATE.finditer(text):
        parsed = _parse_date(match.group(0))
        if parsed is None:
            continue
        value, precision = parsed
        result.append(DateMatch(match.start(), match.end(), match.group(0), value, precision))
    return result


def _parse_date(raw: str) -> tuple[datetime, str] | None:
    value = raw.strip()
    try:
        if re.fullmatch(r"\d{4}", value):
            return datetime(int(value), 1, 1, tzinfo=UTC), "year"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.fromisoformat(value).replace(tzinfo=UTC), "day"
        match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", value)
        if match:
            month = _MONTHS.get(match.group(2).casefold())
            if month:
                return datetime(int(match.group(3)), month, int(match.group(1)), tzinfo=UTC), "day"
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
        if match:
            month = _MONTHS.get(match.group(1).casefold())
            if month:
                return datetime(int(match.group(3)), month, int(match.group(2)), tzinfo=UTC), "day"
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", value)
        if match:
            month = _MONTHS.get(match.group(1).casefold())
            if month:
                return datetime(int(match.group(2)), month, 1, tzinfo=UTC), "month"
    except ValueError:
        return None
    return None


def modality(sentence: str) -> tuple[str, str]:
    lower = sentence.casefold()
    if any(word in lower for word in ("alleged", "reportedly", "accused")):
        return ALLEGED, "alleged"
    if any(word in lower for word in ("estimated", "approximately", "about ", "around ")):
        return ESTIMATED, "estimated"
    if any(
        word in lower for word in ("expected", "forecast", "projected", "due to begin", "slated")
    ):
        return EXPECTED, "expected"
    if any(word in lower for word in ("proposed", "proposal", "proposes")):
        return PROPOSED, "proposed"
    if any(
        word in lower for word in ("plans to", "plan to", "intends", "will invest", "will build")
    ):
        return PLANNED, "planned"
    if any(word in lower for word in ("announced", "unveiled", "declared")):
        return ANNOUNCED, "announced"
    if any(word in lower for word in ("approved", "approval", "cleared", "cabinet")):
        return CONFIRMED, "approved"
    if any(word in lower for word in ("funded", "funding approved", "sanctioned")):
        return CONFIRMED, "funded"
    if any(
        word in lower
        for word in ("under construction", "construction began", "broke ground", "groundbreaking")
    ):
        return CONFIRMED, "under_construction"
    if any(
        word in lower
        for word in ("operational", "commissioned", "production began", "started production")
    ):
        return CONFIRMED, "operational"
    if any(word in lower for word in ("completed", "closed the acquisition", "signed")):
        return CONFIRMED, "completed"
    return CONFIRMED, "announced"


def modality_value(sentence: str) -> str:
    return modality(sentence)[0]


def commitment_value(sentence: str) -> str:
    return modality(sentence)[1]


def polarity(sentence: str) -> str:
    lower = sentence.casefold()
    if any(word in lower for word in ("denies", "denied", "refuted", "rejected the claim")):
        return "denies"
    return "asserts"


def claim_key(
    subject: str,
    predicate: str,
    object_text: str,
    year: str | None,
    amount: float | None,
    currency: str | None,
) -> str:
    raw = "|".join(
        [
            _key(subject),
            _key(predicate),
            _key(object_text),
            year or "",
            "" if amount is None else f"{amount:.2f}",
            currency or "",
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None
