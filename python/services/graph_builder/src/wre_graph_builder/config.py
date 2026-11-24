"""Graph/dedup service configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphConfig:
    database_url: str
    tenant_id: str = "public"
    concurrency: int = 2
    dispatch_interval: float = 5.0
    poll_interval: float = 1.0
    lease_seconds: int = 300
    max_attempts: int = 3
    simhash_distance: int = 8
    title_floor: float = 0.35
    extraction_concurrency: int = 2
    extraction_dispatch_interval: float = 5.0
    extraction_poll_interval: float = 1.0
    extraction_lease_seconds: int = 300
    extraction_max_attempts: int = 3
    max_extract_chars: int = 200_000
    extraction_version: str = "extract-1.0.0"
    kg_rebuild_interval: float = 60.0
    kg_enabled: bool = True


def load(environ: Mapping[str, str] | None = None) -> GraphConfig:
    source = os.environ if environ is None else environ
    database_url = _value(source, "DATABASE_URL", "")
    if not database_url:
        raise ConfigurationError("DATABASE_URL is required for the graph service")
    return GraphConfig(
        database_url=database_url,
        tenant_id=_value(source, "GRAPH_TENANT_ID", "public"),
        concurrency=_int(source, "DEDUP_CONCURRENCY", 2, 1, 16),
        dispatch_interval=_float(source, "DEDUP_DISPATCH_INTERVAL_SECONDS", 5.0, 1.0, 300.0),
        poll_interval=_float(source, "DEDUP_POLL_INTERVAL_SECONDS", 1.0, 0.2, 60.0),
        lease_seconds=_int(source, "DEDUP_LEASE_SECONDS", 300, 30, 3600),
        max_attempts=_int(source, "DEDUP_MAX_ATTEMPTS", 3, 1, 10),
        simhash_distance=_int(source, "DEDUP_SIMHASH_DISTANCE", 8, 0, 32),
        title_floor=_float(source, "DEDUP_TITLE_FLOOR", 0.35, 0.0, 1.0),
        extraction_concurrency=_int(source, "EXTRACTION_CONCURRENCY", 2, 1, 8),
        extraction_dispatch_interval=_float(
            source, "EXTRACTION_DISPATCH_INTERVAL_SECONDS", 5.0, 1.0, 300.0
        ),
        extraction_poll_interval=_float(source, "EXTRACTION_POLL_INTERVAL_SECONDS", 1.0, 0.2, 60.0),
        extraction_lease_seconds=_int(source, "EXTRACTION_LEASE_SECONDS", 300, 30, 3600),
        extraction_max_attempts=_int(source, "EXTRACTION_MAX_ATTEMPTS", 3, 1, 10),
        max_extract_chars=_int(source, "MAX_EXTRACT_CHARS", 200_000, 1_000, 2_000_000),
        kg_rebuild_interval=_float(source, "KG_REBUILD_INTERVAL_SECONDS", 60.0, 10.0, 3600.0),
        kg_enabled=_bool(source, "KG_ENABLED", True),
    )


def _value(source: Mapping[str, str], key: str, fallback: str) -> str:
    return source.get(key, "").strip() or fallback


def _int(source: Mapping[str, str], key: str, fallback: int, minimum: int, maximum: int) -> int:
    raw = _value(source, key, "")
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be in {minimum}..{maximum}")
    return value


def _float(
    source: Mapping[str, str], key: str, fallback: float, minimum: float, maximum: float
) -> float:
    raw = _value(source, key, "")
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be in {minimum}..{maximum}")
    return value


def _bool(source: Mapping[str, str], key: str, fallback: bool) -> bool:
    raw = _value(source, key, "")
    if not raw:
        return fallback
    return raw.casefold() in {"1", "true", "yes", "on"}
