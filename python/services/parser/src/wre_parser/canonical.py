"""Canonical document assembly and schema validation."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from .extract import Extraction


class CanonicalError(ValueError):
    """The assembled document does not satisfy the canonical contract."""


def contracts_root() -> Path:
    configured = os.environ.get("CONTRACTS_DIR", "").strip()
    if configured:
        return Path(configured)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts"
        if candidate.is_dir():
            return candidate
    return Path("/app/contracts")


@lru_cache(maxsize=1)
def validator() -> Draft202012Validator:
    path = contracts_root() / "documents" / "v1" / "canonical-document.schema.json"
    schema = __import__("json").loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def build(
    *,
    document: dict[str, Any],
    revision: dict[str, Any],
    source_id: str | None,
    extraction: Extraction,
    text: str,
    language: str,
    language_confidence: float,
    quality: dict[str, Any],
    content_hash: str,
    raw_uri: str,
    parsed_uri: str,
    processing_version: str,
    parser_version: str,
    normalizer_version: str,
    published_at: datetime | None,
    updated_at: datetime | None,
    retrieved_at: datetime,
    warnings: list[str],
) -> dict[str, Any]:
    canonical = {
        "document_id": document["document_id"],
        "source_id": source_id,
        "revision": revision["revision"],
        "tenant_id": document["tenant_id"],
        "canonical_url": document["canonical_url"],
        "original_url": revision["fetched_url"],
        "title": extraction.title.strip(),
        "content": text,
        "language": language,
        "language_confidence": language_confidence,
        "document_type": _document_type(extraction),
        "author": extraction.authors,
        "published_at": _iso(published_at),
        "updated_at": _iso(updated_at),
        "retrieved_at": _iso(retrieved_at),
        "content_hash": content_hash,
        "raw_object_uri": raw_uri,
        "parsed_object_uri": parsed_uri,
        "processing_version": processing_version,
        "parser_version": parser_version,
        "normalizer_version": normalizer_version,
        "metadata": _metadata(extraction),
        "warnings": sorted(set(warnings)),
        "quality": quality,
        "provenance": {
            "http_status": revision.get("http_status"),
            "content_type": revision.get("content_type"),
            "fetched_url": revision["fetched_url"],
            "raw_artifact_uri": raw_uri,
            "extractor": extraction.extractor,
        },
    }
    errors = sorted(validator().iter_errors(canonical), key=lambda error: error.path)
    if errors:
        first = errors[0]
        raise CanonicalError(f"{first.json_path}: {first.message}")
    return canonical


def parsed_representation(extraction: Extraction) -> dict[str, Any]:
    return {
        "kind": extraction.kind,
        "extractor": extraction.extractor,
        "title": extraction.title,
        "text": extraction.text,
        "authors": extraction.authors,
        "published_raw": extraction.published_raw,
        "updated_raw": extraction.updated_raw,
        "description": extraction.description,
        "canonical_url": extraction.canonical_url,
        "tags": extraction.tags,
        "images": extraction.images,
        "links": extraction.links,
        "headings": extraction.headings,
        "visible_chars": extraction.visible_chars,
        "warnings": extraction.warnings,
        "metadata": extraction.metadata,
    }


def _document_type(extraction: Extraction) -> str:
    from .normalize import document_type

    return document_type(extraction.kind)


def _metadata(extraction: Extraction) -> dict[str, Any]:
    metadata = dict(extraction.metadata)
    metadata["description"] = extraction.description
    metadata["tags"] = extraction.tags
    metadata["images"] = extraction.images
    metadata["links"] = extraction.links
    metadata["extractor"] = extraction.extractor
    return metadata


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
