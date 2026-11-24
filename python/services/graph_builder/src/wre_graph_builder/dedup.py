"""Deterministic near-duplicate and lineage helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Candidate:
    document_id: str
    revision: int
    tenant_id: str
    host: str
    title: str
    body: str
    simhash: int
    content_hash: str
    language: str
    published_at: datetime | None
    first_seen_at: datetime
    cluster_id: str | None
    state: str


@dataclass(frozen=True, slots=True)
class Member:
    document_id: str
    host: str
    relation: str
    origin_kind: str


def tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(text)]


def shingles(text: str, size: int = 5) -> set[int]:
    words = tokens(text)
    if len(words) < size:
        return {
            int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
            for word in words
        }
    return {
        int.from_bytes(
            hashlib.blake2b(" ".join(words[index : index + size]).encode(), digest_size=8).digest(),
            "big",
        )
        for index in range(len(words) - size + 1)
    }


def jaccard(left: set[Any], right: set[Any]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return intersection / len(left | right)


def title_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = set(tokens(left)), set(tokens(right))
    token_score = jaccard(left_tokens, right_tokens)
    char_score = SequenceMatcher(None, " ".join(tokens(left)), " ".join(tokens(right))).ratio()
    return max(token_score, char_score)


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def official_origin(host: str, title: str, body: str) -> bool:
    hostname = urlparse(f"http://{host}").hostname or host
    hostname = hostname.casefold()
    if hostname.endswith((".gov", ".gov.in", ".gov.uk", ".mil", ".europa.eu")):
        return True
    sample = f"{title} {body[:4000]}".casefold()
    return "press release" in sample or "for immediate release" in sample


def classify(
    body_similarity: float,
    title_score: float,
    simhash_distance: int,
    same_host: bool,
    official: bool,
) -> tuple[str, str, float] | None:
    """Return (member relation, origin kind, similarity) or None for no link."""
    near_duplicate = body_similarity >= 0.6 or (simhash_distance <= 3 and body_similarity >= 0.35)
    related = title_score >= 0.5 and body_similarity >= 0.12
    if not near_duplicate and not related:
        return None
    origin = "official" if official else "media"
    if near_duplicate:
        relation = "repeated_claim" if same_host else "syndicated_copy"
        return relation, origin, round(body_similarity, 4)
    relation = "follows_up" if same_host else "independent"
    return relation, origin, round(max(body_similarity, 0.0), 4)


def evidence_strength(members: list[Member], lineage_counts: dict[str, int]) -> dict[str, object]:
    copies = {"syndicated_copy", "repeated_claim"}
    independent_hosts = {
        member.host for member in members if member.relation not in copies and member.host
    }
    syndicated = sum(1 for member in members if member.relation in copies)
    official = sum(1 for member in members if member.origin_kind == "official")
    independent = len(independent_hosts)
    effective = independent
    evidence_score = min(1.0, 0.3 + 0.25 * effective + 0.10 * official)
    inflation = len(members) / max(1, effective)
    return {
        "document_count": len(members),
        "independent_sources": independent,
        "syndicated_copies": syndicated,
        "official_statements": official,
        "effective_sources": effective,
        "evidence_score": round(evidence_score, 4),
        "inflation_ratio": round(inflation, 2),
        "lineage": lineage_counts,
    }


def origin_sort_key(
    published_at: datetime | None, first_seen_at: datetime
) -> tuple[datetime, datetime]:
    return (published_at or first_seen_at, first_seen_at)
