"""Parser/ETL configuration, validated once at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

PIPELINE_VERSION = "1.0.0"
NORMALIZER_VERSION = "normalize-1.0.0"


class ConfigurationError(ValueError):
    """Raised when the environment cannot produce a valid ETL configuration."""


@dataclass(frozen=True, slots=True)
class EtlConfig:
    database_url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str
    s3_bucket_raw: str
    s3_bucket_parsed: str
    concurrency: int = 2
    dispatch_interval: float = 5.0
    poll_interval: float = 1.0
    lease_seconds: int = 300
    max_attempts: int = 3
    min_words: int = 25
    min_quality: float = 0.35
    max_text_bytes: int = 2 << 20

    @property
    def processing_version(self) -> str:
        return f"parser-{PIPELINE_VERSION}"


def load(environ: Mapping[str, str] | None = None) -> EtlConfig:
    source = os.environ if environ is None else environ

    database_url = _required(source, "DATABASE_URL")
    s3_endpoint = _required(source, "S3_ENDPOINT")

    return EtlConfig(
        database_url=database_url,
        s3_endpoint=s3_endpoint,
        s3_access_key=_value(source, "S3_ACCESS_KEY", "wre"),
        s3_secret_key=_value(source, "S3_SECRET_KEY", "wre-secret"),
        s3_region=_value(source, "S3_REGION", "us-east-1"),
        s3_bucket_raw=_value(source, "S3_BUCKET_RAW", "wre-raw"),
        s3_bucket_parsed=_value(source, "S3_BUCKET_PARSED", "wre-parsed"),
        concurrency=_int(source, "ETL_CONCURRENCY", 2, 1, 32),
        dispatch_interval=_float(source, "ETL_DISPATCH_INTERVAL_SECONDS", 5.0, 1.0, 300.0),
        poll_interval=_float(source, "ETL_POLL_INTERVAL_SECONDS", 1.0, 0.2, 60.0),
        lease_seconds=_int(source, "ETL_LEASE_SECONDS", 300, 30, 3600),
        max_attempts=_int(source, "ETL_MAX_ATTEMPTS", 3, 1, 10),
        min_words=_int(source, "ETL_MIN_WORDS", 25, 1, 5000),
        min_quality=_float(source, "ETL_MIN_QUALITY", 0.35, 0.0, 1.0),
        max_text_bytes=_int(source, "ETL_MAX_TEXT_BYTES", 2 << 20, 1024, 8 << 20),
    )


def _required(source: Mapping[str, str], key: str) -> str:
    value = _value(source, key, "")
    if not value:
        raise ConfigurationError(f"{key} is required for the parser ETL service")
    return value


def _value(source: Mapping[str, str], key: str, fallback: str) -> str:
    return source.get(key, "").strip() or fallback


def _int(source: Mapping[str, str], key: str, fallback: int, minimum: int, maximum: int) -> int:
    raw = _value(source, key, "")
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be in {minimum}..{maximum}, got {value}")
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
        raise ConfigurationError(f"{key} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be in {minimum}..{maximum}, got {value}")
    return value
