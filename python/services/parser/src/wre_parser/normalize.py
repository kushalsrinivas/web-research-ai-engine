"""Deterministic cleaning, metadata normalization, hashing, and language detection."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import UTC, datetime

import py3langid  # type: ignore[import-untyped]

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_HSPACE = re.compile(r"[^\S\n]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_WORD = re.compile(r"\w+", re.UNICODE)
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = [_HSPACE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]


def normalize_authors(values: list[str] | str) -> list[str]:
    raw = values if isinstance(values, list) else [values]
    people: list[str] = []
    seen: set[str] = set()
    for value in raw:
        for candidate in re.split(r"\s*(?:;|\||&|\band\b)\s*", value):
            candidate = " ".join(unicodedata.normalize("NFKC", candidate).split())
            if not candidate or len(candidate) > 200:
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            people.append(candidate)
    return people


def parse_timestamp(raw: str | datetime | None) -> datetime | None:
    if raw is None or raw == "":
        return None
    value = raw if isinstance(raw, datetime) else _parse_iso_or_fuzzy(str(raw))
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    if not 1900 <= value.year <= 2100:
        return None
    return value


def _parse_iso_or_fuzzy(raw: str) -> datetime | None:
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    try:
        from dateutil import parser as date_parser  # type: ignore[import-untyped]

        parsed: datetime = date_parser.parse(candidate, fuzzy=False)
        return parsed
    except (ValueError, OverflowError, TypeError):
        return None


def detect_language(text: str) -> tuple[str, float]:
    sample = text[:20000]
    if word_count(sample) < 10:
        return "und", 0.0
    ranked = py3langid.rank(sample)
    if not ranked:
        return "und", 0.0
    language, top_score = ranked[0]
    if len(ranked) > 1:
        second_score = ranked[1][1]
        confidence = 1.0 / (1.0 + math.exp(min(0.0, second_score - top_score)))
    else:
        confidence = 0.5
    confidence = round(min(max(confidence, 0.0), 1.0), 4)
    if confidence < 0.6:
        return "und", confidence
    return language, confidence


def simhash64(text: str) -> int:
    tokens = [token.casefold() for token in _TOKEN.findall(text)]
    shingles = (
        [" ".join(tokens[index : index + 3]) for index in range(len(tokens) - 2)]
        if len(tokens) >= 3
        else tokens
    )
    if not shingles:
        return 0
    weights = [0] * 64
    for shingle in shingles:
        digest = int.from_bytes(
            hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    value = 0
    for bit in range(64):
        if weights[bit] > 0:
            value |= 1 << bit
    return value


def simhash_signed(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def simhash_hex(value: int) -> str:
    return f"0x{value & ((1 << 64) - 1):016x}"


def document_type(kind: str) -> str:
    return {
        "html": "article",
        "pdf": "document",
        "docx": "document",
        "feed": "feed",
        "json": "data",
        "xml": "xml",
        "text": "text",
    }.get(kind, "other")


def truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True
