"""Environment-driven configuration, validated once at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEVELOPMENT = "development"
STAGING = "staging"
PRODUCTION = "production"
_ENVIRONMENTS = {DEVELOPMENT, STAGING, PRODUCTION}
_REQUIRED_OUTSIDE_DEV = (
    "DATABASE_URL",
    "KAFKA_BROKERS",
    "S3_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


class ConfigurationError(ValueError):
    """Raised when the environment cannot produce a valid configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    service: str
    env: str
    port: int
    log_level: str
    sample_ratio: float
    otlp_endpoint: str
    build_version: str
    shutdown_timeout: float = 10.0

    @property
    def processor_version(self) -> str:
        return f"{self.service}@{self.build_version}"


def load(
    service: str,
    default_port: int,
    environ: Mapping[str, str] | None = None,
) -> Config:
    source = os.environ if environ is None else environ
    env = _value(source, "APP_ENV", DEVELOPMENT)
    if env not in _ENVIRONMENTS:
        raise ConfigurationError(f"APP_ENV must be development, staging or production, got {env!r}")

    port = default_port
    raw_port = _value(source, "PORT", "")
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError(
                f"PORT must be an integer in 1..65535, got {raw_port!r}"
            ) from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"PORT must be an integer in 1..65535, got {raw_port!r}")

    log_level = _value(source, "LOG_LEVEL", "info").lower()
    if log_level not in {"debug", "info", "warn", "error"}:
        raise ConfigurationError("LOG_LEVEL must be debug, info, warn or error")

    sample_ratio = 0.1 if env != DEVELOPMENT else 1.0
    raw_ratio = _value(source, "OTEL_SAMPLE_RATIO", "")
    if raw_ratio:
        try:
            sample_ratio = float(raw_ratio)
        except ValueError as exc:
            raise ConfigurationError(
                f"OTEL_SAMPLE_RATIO must be in 0..1, got {raw_ratio!r}"
            ) from exc
        if not 0.0 <= sample_ratio <= 1.0:
            raise ConfigurationError(f"OTEL_SAMPLE_RATIO must be in 0..1, got {raw_ratio!r}")

    if env != DEVELOPMENT:
        for key in _REQUIRED_OUTSIDE_DEV:
            if not _value(source, key, ""):
                raise ConfigurationError(f"{key} is required when APP_ENV={env}")

    return Config(
        service=service,
        env=env,
        port=port,
        log_level=log_level,
        sample_ratio=sample_ratio,
        otlp_endpoint=_value(source, "OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        build_version=_value(source, "SERVICE_VERSION", "dev"),
    )


def _value(source: Mapping[str, str], key: str, fallback: str) -> str:
    return source.get(key, "").strip() or fallback
