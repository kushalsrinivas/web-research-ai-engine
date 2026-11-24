"""Deterministic entity-name normalization and confidence scoring."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_LEGAL_SUFFIX = re.compile(
    r"\b(private\s+limited|pvt\.?\s+ltd\.?|public\s+limited|limited|ltd\.?|llc|inc\.?|"
    r"incorporated|corp\.?|corporation|gmbh|plc|llp|co\.?|company)\s*$",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[.,'\"()\[\]{}:;!?/\\]")
_SPACE = re.compile(r"\s+")

AUTO_MATCH = 0.95
REVIEW_MATCH = 0.78


@dataclass(frozen=True, slots=True)
class EntityInput:
    name: str
    entity_type: str | None = None
    domain: str | None = None
    country: str | None = None
    official_ids: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    entity_id: str
    canonical_name: str
    normalized_name: str
    alias_keys: list[str]
    external_ids: dict[str, str]
    country: str | None
    entity_type: str
    confidence: float


def display_name(name: str) -> str:
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", name)).strip()


def normalize_name(name: str, entity_type: str | None = None) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " and ")
    text = _PUNCTUATION.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    if entity_type == "organization":
        text = _LEGAL_SUFFIX.sub("", text).strip()
    return text


def alias_key(name: str, entity_type: str | None = None) -> str:
    return normalize_name(name, entity_type)


def token_set(name: str) -> set[str]:
    return set(normalize_name(name).split())


def name_similarity(left: str, right: str) -> float:
    left_key, right_key = normalize_name(left), normalize_name(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    token_score = len(set(left_key.split()) & set(right_key.split())) / len(
        set(left_key.split()) | set(right_key.split())
    )
    char_score = SequenceMatcher(None, left_key, right_key).ratio()
    return max(token_score, char_score)


def score_candidate(
    data: EntityInput, candidate: MatchCandidate
) -> tuple[float, dict[str, object]]:
    normalized = normalize_name(data.name, data.entity_type)
    exact = normalized == candidate.normalized_name
    alias_match = normalized in candidate.alias_keys
    similarity = max(
        [name_similarity(data.name, candidate.canonical_name)]
        + [name_similarity(data.name, alias) for alias in candidate.alias_keys]
    )
    type_match = bool(data.entity_type and data.entity_type == candidate.entity_type)
    country_match = bool(
        data.country
        and candidate.country
        and data.country.casefold() == candidate.country.casefold()
    )

    domain = (data.domain or "").casefold() or None
    candidate_domain = str(candidate.external_ids.get("domain") or "").casefold() or None
    domain_match = bool(domain and candidate_domain and domain == candidate_domain)

    input_ids = {str(value) for value in (data.official_ids or {}).values() if value}
    candidate_ids = {str(value) for value in candidate.external_ids.values() if value}
    external_match = bool(input_ids & candidate_ids)

    if external_match:
        score = 0.99
    elif exact:
        score = 1.0
    elif alias_match:
        score = 0.98
    elif domain_match:
        score = 0.95
    else:
        score = min(
            0.94,
            0.45
            + 0.35 * similarity
            + 0.05 * (1 if type_match else 0)
            + 0.03 * (1 if country_match else 0),
        )

    corroborated = (external_match or exact or alias_match or domain_match) and bool(
        data.entity_type and type_match
    )
    features: dict[str, object] = {
        "exact_name": exact,
        "alias_match": alias_match,
        "external_id_match": external_match,
        "domain_match": domain_match,
        "type_match": type_match,
        "country_match": country_match,
        "name_similarity": round(similarity, 4),
        "corroborated": corroborated,
    }
    return round(score, 4), features


def decide(score: float, features: dict[str, object]) -> str:
    if score >= AUTO_MATCH and features.get("corroborated"):
        return "auto_match"
    if score >= REVIEW_MATCH:
        return "review"
    return "keep_separate"
