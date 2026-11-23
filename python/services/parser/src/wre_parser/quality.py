"""Document quality scoring with separate, explainable dimensions."""

from __future__ import annotations

from typing import Any

from .extract import Extraction

_WEIGHTS = {
    "text_completeness": 0.30,
    "extraction_quality": 0.20,
    "metadata_completeness": 0.15,
    "language_confidence": 0.15,
    "boilerplate_ratio": 0.10,
    "structural_quality": 0.10,
}


def score(
    extraction: Extraction,
    text: str,
    language: str,
    language_confidence: float,
    words: int,
) -> dict[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    completeness = _clamp(words / 300)
    extraction_quality = _extraction_quality(extraction, text)
    metadata = _metadata(extraction, language)
    boilerplate = _boilerplate(extraction, text)
    structure = _structure(extraction, lines)

    dimensions = {
        "text_completeness": round(completeness, 4),
        "extraction_quality": round(extraction_quality, 4),
        "metadata_completeness": round(metadata, 4),
        "language_confidence": round(_clamp(language_confidence), 4),
        "boilerplate_ratio": round(boilerplate, 4),
        "structural_quality": round(structure, 4),
    }
    total = sum(dimensions[name] * weight for name, weight in _WEIGHTS.items())
    reasons = {
        "text_completeness": "short_content",
        "extraction_quality": "weak_extraction",
        "metadata_completeness": "missing_metadata",
        "language_confidence": "uncertain_language",
        "boilerplate_ratio": "high_boilerplate",
        "structural_quality": "weak_structure",
    }
    return {
        "score": round(_clamp(total), 4),
        "dimensions": dimensions,
        "reasons": [reasons[name] for name, value in dimensions.items() if value < 0.5],
    }


def _extraction_quality(extraction: Extraction, text: str) -> float:
    if not text.strip():
        return 0.0
    if "trafilatura_failed" in extraction.warnings:
        return 0.3
    if "fallback_extraction" in extraction.warnings:
        return 0.6
    if extraction.kind == "html":
        return 1.0
    if extraction.warnings:
        return 0.7
    return 0.9


def _metadata(extraction: Extraction, language: str) -> float:
    score = 0.0
    if extraction.title:
        score += 0.35
    if extraction.published_raw:
        score += 0.25
    if extraction.authors:
        score += 0.20
    if extraction.description:
        score += 0.05
    if language != "und":
        score += 0.15
    return _clamp(score)


def _boilerplate(extraction: Extraction, text: str) -> float:
    if not text.strip():
        return 0.0
    if extraction.visible_chars <= 0:
        return 1.0
    extracted = len(" ".join(text.split()))
    return _clamp(extracted / extraction.visible_chars)


def _structure(extraction: Extraction, lines: list[str]) -> float:
    line_score = _clamp(len(lines) / 8)
    if extraction.kind != "html":
        return line_score
    heading_score = _clamp(extraction.headings / 3)
    return _clamp(0.7 * line_score + 0.3 * heading_score)


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)
