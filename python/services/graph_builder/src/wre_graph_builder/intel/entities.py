"""Hybrid entity mention extraction: rules, gazetteers, and spaCy NER."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

ENTITY_TYPES = {
    "person",
    "organization",
    "government",
    "location",
    "product",
    "industry",
    "technology",
    "financial_instrument",
    "project",
    "policy",
    "event",
    "work",
    "concept",
    "other",
}

_PRIORITY = {
    "government": 10,
    "organization": 9,
    "person": 8,
    "policy": 7,
    "project": 6,
    "location": 5,
    "product": 4,
    "technology": 3,
    "financial_instrument": 2,
    "industry": 1,
    "event": 1,
    "work": 1,
    "concept": 1,
    "other": 0,
}

_COUNTRIES = {
    "india",
    "united states",
    "china",
    "japan",
    "south korea",
    "taiwan",
    "germany",
    "france",
    "united kingdom",
    "singapore",
    "vietnam",
    "indonesia",
    "malaysia",
    "thailand",
    "australia",
    "canada",
    "brazil",
    "russia",
    "saudi arabia",
    "united arab emirates",
    "netherlands",
    "italy",
    "spain",
    "switzerland",
}
_INDIAN_STATES = {
    "andhra pradesh",
    "arunachal pradesh",
    "assam",
    "bihar",
    "chhattisgarh",
    "goa",
    "gujarat",
    "haryana",
    "himachal pradesh",
    "jharkhand",
    "karnataka",
    "kerala",
    "madhya pradesh",
    "maharashtra",
    "manipur",
    "meghalaya",
    "mizoram",
    "nagaland",
    "odisha",
    "punjab",
    "rajasthan",
    "sikkim",
    "tamil nadu",
    "telangana",
    "tripura",
    "uttar pradesh",
    "uttarakhand",
    "west bengal",
    "delhi",
    "jammu and kashmir",
    "ladakh",
    "puducherry",
    "chandigarh",
}
_CITIES = {
    "mumbai",
    "new delhi",
    "delhi",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "chennai",
    "kolkata",
    "pune",
    "ahmedabad",
    "surat",
    "vadodara",
    "gandhinagar",
    "jaipur",
    "lucknow",
    "noida",
    "gurugram",
    "gurgaon",
    "kochi",
    "coimbatore",
    "indore",
    "bhopal",
    "patna",
    "chandigarh",
    "visakhapatnam",
    "mysuru",
    "nagpur",
    "tokyo",
    "seoul",
    "taipei",
    "beijing",
    "shanghai",
    "washington",
    "london",
    "paris",
    "berlin",
    "singapore",
    "dubai",
    "abu dhabi",
    "new york",
    "san francisco",
}
_LOCATIONS = {value: "location" for value in _COUNTRIES | _INDIAN_STATES | _CITIES}

_TECHNOLOGIES = {
    "euv lithography": "technology",
    "semiconductor manufacturing": "technology",
    "lithography": "technology",
    "artificial intelligence": "technology",
    "machine learning": "technology",
    "green hydrogen": "technology",
    "quantum computing": "technology",
    "battery technology": "technology",
    "solar photovoltaic": "technology",
    "5g": "technology",
    "6g": "technology",
}
_PRODUCTS = {
    "semiconductor chips": "product",
    "semiconductors": "product",
    "chips": "product",
    "electric vehicles": "product",
    "ev batteries": "product",
    "solar panels": "product",
    "lithium-ion batteries": "product",
}
_INDUSTRIES = {
    "automotive": "industry",
    "energy": "industry",
    "semiconductor": "industry",
    "semiconductors industry": "industry",
    "electronics": "industry",
    "pharmaceutical": "industry",
    "pharma": "industry",
    "finance": "industry",
    "defence": "industry",
    "defense": "industry",
    "agriculture": "industry",
    "telecom": "industry",
    "steel": "industry",
    "cement": "industry",
    "textiles": "industry",
}
_INSTRUMENTS = {
    "bonds": "financial_instrument",
    "stocks": "financial_instrument",
    "shares": "financial_instrument",
    "equity": "financial_instrument",
    "ipo": "financial_instrument",
    "loan": "financial_instrument",
    "debt": "financial_instrument",
    "mutual fund": "financial_instrument",
    "derivatives": "financial_instrument",
}
_GOVERNMENT = re.compile(
    r"\b(?:Ministry|Department|Government)\s+of\s+[A-Z][A-Za-z&.\- ]{2,60}"
    r"|\b(?:[A-Z][A-Za-z&.\-]+\s+){0,4}(?:Ministry|Department|Commission|Authority|Agency|Council|Parliament|Cabinet)\b"
)
_ORGANIZATION = re.compile(
    r"\b(?:[A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,5}\s+"
    r"(?:Ltd|Limited|Inc|Corp|Corporation|PLC|GmbH|Technologies|Electronics|Industries|"
    r"Systems|Group|Bank|University|Institute|Foundation|Company))\b"
)
_PERSON = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr|Prof|CEO|Chairman|Chairperson|Minister|Secretary|Director)\b\.?\s+"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}"
)
_PROJECT_NAMED = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&\-]+\s+){1,3}(?:Project|Facility|Plant|Fab|Corridor|Park|Hub)\b"
)
_PROJECT_GENERIC = re.compile(
    r"\b(?:semiconductor|manufacturing|fabrication|assembly|testing|research)\s+"
    r"(?:facility|plant|fab|unit|park|hub|project)\b",
    re.IGNORECASE,
)
_POLICY_NAMED = re.compile(r"\b(?:[A-Z][A-Za-z0-9&.\-]+\s+){0,3}(?:Policy|Scheme|Act)\b")
_POLICY_KNOWN = re.compile(
    r"\b(?:production-linked incentive|pli scheme|incentive scheme|industrial policy|"
    r"semiconductor policy)\b",
    re.IGNORECASE,
)
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")

_STOPWORDS = {
    "the company",
    "company",
    "government",
    "officials",
    "analysts",
    "report",
    "reports",
    "it",
    "they",
    "he",
    "she",
    "we",
    "this",
    "that",
    "these",
    "those",
    "india today",
    "inr",
    "usd",
    "eur",
    "gdp",
    "ceo",
    "cto",
    "cfo",
    "ipo",
    "the ministry",
    "the government",
    "the project",
    "the facility",
}
_TITLE_PREFIX = re.compile(r"^(?:The|A|An)\s+", re.IGNORECASE)
_NESTED_TYPES = {"industry", "technology", "product", "financial_instrument"}
_NESTED_HOSTS = {"project", "organization", "policy", "government"}

_NLP: Any = None
_NLP_LOCK = threading.Lock()
_NLP_READY = False


@dataclass(slots=True)
class Mention:
    surface: str
    char_start: int
    char_end: int
    entity_type: str
    confidence: float
    extractor: str
    entity_id: str | None = None
    resolution_confidence: float | None = None


def extract_mentions(text: str, language: str) -> list[Mention]:
    candidates: list[Mention] = []
    candidates.extend(_dictionary_mentions(text))
    candidates.extend(_rule_mentions(text))
    if language in {"en", "und"}:
        candidates.extend(_spacy_mentions(text))
    merged = _merge(candidates)
    return merged[:80]


def _dictionary_mentions(text: str) -> list[Mention]:
    found: list[Mention] = []
    for phrase, entity_type in (
        list(_LOCATIONS.items())
        + list(_TECHNOLOGIES.items())
        + list(_PRODUCTS.items())
        + list(_INDUSTRIES.items())
        + list(_INSTRUMENTS.items())
    ):
        pattern = re.compile(rf"(?<![\w]){re.escape(phrase)}(?![\w])", re.IGNORECASE)
        for match in pattern.finditer(text):
            found.append(
                Mention(match.group(0), match.start(), match.end(), entity_type, 0.85, "dictionary")
            )
    return found


def _rule_mentions(text: str) -> list[Mention]:
    found: list[Mention] = []
    for pattern, entity_type, confidence, extractor in (
        (_GOVERNMENT, "government", 0.92, "rule:government"),
        (_ORGANIZATION, "organization", 0.9, "rule:organization"),
        (_PERSON, "person", 0.82, "rule:person"),
        (_PROJECT_NAMED, "project", 0.8, "rule:project"),
        (_PROJECT_GENERIC, "project", 0.8, "rule:project"),
        (_POLICY_NAMED, "policy", 0.78, "rule:policy"),
        (_POLICY_KNOWN, "policy", 0.78, "rule:policy"),
    ):
        for match in pattern.finditer(text):
            surface, start, end = _trim(match.group(0), match.start())
            if surface:
                found.append(Mention(surface, start, end, entity_type, confidence, extractor))
    for match in _ACRONYM.finditer(text):
        if match.group(0).casefold() in {"inr", "usd", "eur", "gdp", "pli"}:
            continue
        found.append(
            Mention(
                match.group(0), match.start(), match.end(), "organization", 0.55, "rule:acronym"
            )
        )
    return found


def _spacy_mentions(text: str) -> list[Mention]:
    nlp = _load_nlp()
    if nlp is None:
        return []
    with _NLP_LOCK:
        doc = nlp(text[:200_000])
    found: list[Mention] = []
    for entity in doc.ents:
        surface, start, end = _trim(entity.text, entity.start_char)
        if not surface:
            continue
        label = entity.label_
        context = f"{surface} {text[max(0, start - 20) : min(len(text), end + 30)]}".casefold()
        if label in {"ORG", "NORP"}:
            entity_type = (
                "government"
                if any(
                    word in context
                    for word in ("ministry", "government", "department", "authority", "commission")
                )
                else "organization"
            )
        elif label == "PERSON":
            entity_type = "person"
        elif label in {"GPE", "LOC", "FAC"}:
            entity_type = "location"
        elif label == "PRODUCT":
            entity_type = "product"
        elif label == "EVENT":
            entity_type = "event"
        elif label == "LAW":
            entity_type = "policy"
        else:
            continue
        found.append(Mention(surface, start, end, entity_type, 0.75, "spacy:en_core_web_sm"))
    return found


def _load_nlp() -> Any:
    global _NLP, _NLP_READY
    if _NLP_READY:
        return _NLP
    with _NLP_LOCK:
        if _NLP_READY:
            return _NLP
        try:
            import spacy

            _NLP = spacy.load(
                "en_core_web_sm",
                disable=["parser", "tagger", "lemmatizer", "textcat", "attribute_ruler"],
            )
        except Exception:
            _NLP = None
        _NLP_READY = True
    return _NLP


def _merge(candidates: list[Mention]) -> list[Mention]:
    accepted: list[Mention] = []
    for candidate in sorted(
        candidates, key=lambda item: (-(item.char_end - item.char_start), item.char_start)
    ):
        if _is_stopword(candidate.surface) or candidate.entity_type not in ENTITY_TYPES:
            continue
        overlapping = [
            existing
            for existing in accepted
            if candidate.char_start < existing.char_end and candidate.char_end > existing.char_start
        ]
        if overlapping:
            nested = candidate.entity_type in _NESTED_TYPES and all(
                existing.entity_type in _NESTED_HOSTS
                and candidate.char_start >= existing.char_start
                and candidate.char_end <= existing.char_end
                for existing in overlapping
            )
            if not nested:
                continue
        accepted.append(candidate)
    accepted.sort(key=lambda item: item.char_start)
    deduped: list[Mention] = []
    seen: set[tuple[int, int, str]] = set()
    for mention in accepted:
        key = (mention.char_start, mention.char_end, mention.entity_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(mention)
    return deduped


def _is_stopword(surface: str) -> bool:
    cleaned = " ".join(surface.split()).casefold()
    return (
        len(cleaned) < 2
        or cleaned in _STOPWORDS
        or cleaned.isdigit()
        or all(char.isdigit() or char.isspace() for char in cleaned)
    )


def _trim(surface: str, start: int) -> tuple[str, int, int]:
    prefix = _TITLE_PREFIX.match(surface)
    if prefix and len(surface) > prefix.end():
        start += prefix.end()
        surface = surface[prefix.end() :]
    stripped = surface.strip()
    start += len(surface) - len(surface.lstrip())
    return stripped, start, start + len(stripped)
