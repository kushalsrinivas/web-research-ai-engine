from __future__ import annotations

import json
import logging

from opentelemetry import trace
from wre_pipeline.log import JsonFormatter


def test_formatter_includes_trace_and_extra_fields() -> None:
    record = logging.LogRecord(
        name="parser",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unit of work",
        args=(),
        exc_info=None,
    )
    record.document_id = "01JB2Q1H7S5D9F4K2M8N6P3R0T"
    record.duration_ms = 42

    span_context = trace.SpanContext(
        trace_id=int("4bf92f3577b34da6a3ce929d0e0e4736", 16),
        span_id=int("00f067aa0ba902b7", 16),
        is_remote=False,
    )
    with trace.use_span(trace.NonRecordingSpan(span_context), end_on_exit=False):
        payload = json.loads(JsonFormatter("parser", "development").format(record))

    assert payload["service"] == "parser"
    assert payload["env"] == "development"
    assert payload["msg"] == "unit of work"
    assert payload["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert payload["span_id"] == "00f067aa0ba902b7"
    assert payload["document_id"] == "01JB2Q1H7S5D9F4K2M8N6P3R0T"
    assert payload["duration_ms"] == 42
