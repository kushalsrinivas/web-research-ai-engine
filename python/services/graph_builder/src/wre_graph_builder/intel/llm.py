"""Safety helpers for optional LLM extraction output.

The deterministic extractor does not require an LLM. When an LLM stage is enabled,
callers must pass raw text through `parse_and_validate`, which repairs common
fence/prose wrapping, validates the contract, and returns a quarantine decision
instead of persisting malformed output.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import schemas

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def repair_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def parse_and_validate(raw: str, kind: str) -> tuple[dict[str, Any] | None, list[str]]:
    value = repair_json(raw)
    if value is None:
        return None, ["response is not a JSON object"]
    problems = schemas.errors(kind, value)
    if problems:
        return None, problems
    return value, []


def token_cost(tokens_in: int, tokens_out: int, price_in: float, price_out: float) -> float:
    return round(tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out, 6)
