"""Contract loading and validation for intelligence objects."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

_KINDS = {
    "mention": ("entity-mention.schema.json", "mentions"),
    "event": ("event.schema.json", "events"),
    "claim": ("claim.schema.json", "claims"),
}


def contracts_root() -> Path:
    configured = os.environ.get("CONTRACTS_DIR", "").strip()
    if configured:
        return Path(configured)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts"
        if candidate.is_dir():
            return candidate
    return Path("/app/contracts")


@lru_cache(maxsize=3)
def validator(kind: str) -> Draft202012Validator:
    filename, _ = _KINDS[kind]
    path = contracts_root() / "intelligence" / "v1" / filename
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def errors(kind: str, payload: dict[str, Any]) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.path) or '$'}: {error.message}"
        for error in sorted(validator(kind).iter_errors(payload), key=lambda item: item.path)
    ]


def valid(kind: str, payload: dict[str, Any]) -> bool:
    return not errors(kind, payload)


def contract_names() -> dict[str, str]:
    return {kind: filename for kind, (filename, _) in _KINDS.items()}
