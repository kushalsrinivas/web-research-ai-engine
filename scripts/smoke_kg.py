#!/usr/bin/env python3
"""Phase 7 smoke: temporal graph projection, queries, and rebuild idempotency."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GRAPH = os.environ.get("GRAPH_URL", "http://127.0.0.1:8087") + "/internal/v1"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        GRAPH + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code == 404:
                return 404, {"error": detail}
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def query(name: str, params: dict) -> list[dict]:
    _, result = call("POST", "/kg/query", {"name": name, "params": params})
    return result.get("rows", [])


def main() -> None:
    status, first = call("POST", "/kg/rebuild")
    assert status in (200, 202), first
    version = first["version"]

    investments = query(
        "investments_by_location_industry",
        {"location": "Gujarat", "industry": "semiconductor"},
    )
    assert any(row["company"] == "Tata Electronics" for row in investments), investments

    policies = query("policies_for_investments", {"limit": 20})
    assert any("semiconductor policy" in row["policy"] for row in policies), policies

    _, events = call("GET", "/intel/events?event_type=investment_announcement&limit=5")
    assert events.get("events"), events
    event = events["events"][0]
    _, entities = call("GET", f"/kg/events/{event['event_id']}/entities")
    labels = {row["label"] for row in entities.get("entities", [])}
    assert "Tata Electronics" in labels, entities
    assert "Gujarat" in labels, entities

    connected = query(
        "connected_to_event", {"event_id": event["event_id"], "depth": 2}
    )
    assert any(row["kind"] == "document" for row in connected), connected
    assert any(row["kind"] == "entity" for row in connected), connected

    _, relationships = call("GET", "/kg/relationships?type=INVESTED_IN&limit=10")
    rows = relationships.get("relationships", [])
    assert rows, relationships
    assert any(row.get("source_document_id") for row in rows), rows
    assert any(row.get("valid_from") and row.get("observed_at") for row in rows), rows

    multi = query("claims_multiple_sources", {"min_sources": 2, "limit": 10})
    assert isinstance(multi, list)

    status, second = call("POST", "/kg/rebuild")
    assert status in (200, 202), second
    assert second["version"] != version, second
    assert second["relationships"] == first["relationships"], (first, second)

    _, versions = call("GET", "/kg/versions?limit=10")
    by_version = {row["version"]: row for row in versions.get("versions", [])}
    assert by_version[second["version"]]["status"] == "active", by_version
    assert by_version[version]["status"] == "inactive", by_version

    _, integrity = call("GET", "/kg/integrity")
    assert integrity.get("errors") == 0, integrity

    print(
        "kg smoke: ok "
        f"(version {second['version']}, relationships {second['relationships']}, "
        f"investments {len(investments)}, policies {len(policies)})"
    )


if __name__ == "__main__":
    main()
