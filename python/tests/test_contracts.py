from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2] / "contracts"
SCHEMA_ROOT = ROOT / "events" / "v1"
PAYLOAD_SCHEMA_FOR = {
    "document.scheduled": "document.scheduled.schema.json",
    "document.fetched": "document.fetched.schema.json",
    "document.parsed": "document.parsed.schema.json",
    "document.enriched": "document.enriched.schema.json",
    "document.indexed": "document.gate.schema.json",
    "document.kg_updated": "document.gate.schema.json",
    "document.available": "document.available.schema.json",
    "document.failed": "document.failed.schema.json",
    "document.dead_letter": "document.dead_letter.schema.json",
    "document.duplicate": "document.duplicate.schema.json",
    "document.reprocess": "document.reprocess.schema.json",
}


def load_schema(path: Path) -> dict[str, Any]:
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return schema


def test_every_schema_compiles() -> None:
    schemas = sorted(SCHEMA_ROOT.rglob("*.schema.json"))
    assert schemas
    for path in schemas:
        load_schema(path)


def test_valid_fixtures_match_envelope_and_payload() -> None:
    envelope = Draft202012Validator(load_schema(SCHEMA_ROOT / "envelope.schema.json"))
    fixtures = sorted((ROOT / "fixtures" / "events").glob("*.valid.json"))
    assert fixtures

    for path in fixtures:
        value = json.loads(path.read_text())
        assert envelope.is_valid(value), f"envelope rejected {path.name}"

        event_type = value["event_type"]
        payload_file = PAYLOAD_SCHEMA_FOR[event_type]
        payload_schema = Draft202012Validator(load_schema(SCHEMA_ROOT / "payloads" / payload_file))
        assert payload_schema.is_valid(value["payload"]), f"payload rejected for {event_type}"


def test_invalid_fixtures_are_rejected() -> None:
    envelope = Draft202012Validator(load_schema(SCHEMA_ROOT / "envelope.schema.json"))
    fixtures = sorted((ROOT / "fixtures" / "events" / "invalid").glob("*.json"))
    assert fixtures

    for path in fixtures:
        value = json.loads(path.read_text())
        assert not envelope.is_valid(value), f"expected {path.name} to be rejected"


def test_every_event_type_has_fixture_and_schema() -> None:
    fixtures = sorted((ROOT / "fixtures" / "events").glob("*.valid.json"))
    fixture_types = {json.loads(path.read_text())["event_type"] for path in fixtures}
    assert fixture_types == set(PAYLOAD_SCHEMA_FOR)
    assert all(json.loads(path.read_text()) for path in fixtures)
