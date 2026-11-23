"""JSON logging with trace correlation."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from .config import Config

_EXTRA_FIELDS = (
    "document_id",
    "revision",
    "stage",
    "attempt",
    "outcome",
    "duration_ms",
    "route",
    "method",
    "status",
    "port",
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with trace_id as the log/trace join key."""

    def __init__(self, service: str, env: str) -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "env": self.env,
            "msg": record.getMessage(),
        }

        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            payload["trace_id"] = format(span.trace_id, "032x")
            payload["span_id"] = format(span.span_id, "016x")

        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info is not None:
            payload["error"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


def setup_logging(config: Config) -> logging.Logger:
    logger = logging.getLogger(config.service)
    logger.setLevel(_level(config.log_level))
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(config.service, config.env))
    logger.addHandler(handler)
    return logger


def _level(name: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }[name]
