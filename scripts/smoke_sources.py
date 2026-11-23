#!/usr/bin/env python3
"""CRUD/scoring/scheduling smoke for the source registry. Stdlib only."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:8081") + "/internal/v1"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    domain = f"smoke-{suffix}.example"
    status, source = call(
        "POST",
        "/sources",
        {
            "name": "Smoke source",
            "base_url": f"https://{domain}/feed",
            "source_type": "rss",
            "language": "en",
            "crawl_frequency_seconds": 600,
            "priority": 6,
            "metadata": {"topics": ["smoke"]},
        },
    )
    assert status == 201 and source, (status, source)
    source_id = source["id"]
    try:
        status, due = call("GET", "/sources/due?limit=20")
        assert status == 200 and any(item["id"] == source_id for item in due["items"]), due

        status, disabled = call("PATCH", f"/sources/{source_id}", {"crawl_enabled": False})
        assert status == 200 and not disabled["crawl_enabled"], disabled
        assert disabled.get("next_crawl_at") is None, disabled

        status, enabled = call("PATCH", f"/sources/{source_id}", {"crawl_enabled": True})
        assert status == 200 and enabled["crawl_enabled"] and enabled.get("next_crawl_at"), enabled

        status, score = call(
            "POST",
            f"/sources/{source_id}/score",
            {
                "model_version": "source-quality-v1",
                "dimensions": {"authority": 0.9},
                "details": {"source": "smoke"},
            },
        )
        assert status == 201 and 0 <= score["composite"] <= 1, (status, score)

        status, latest = call("GET", f"/sources/{source_id}/score")
        assert status == 200 and latest["source_id"] == source_id, latest

        status, outcome = call(
            "POST",
            f"/sources/{source_id}/outcomes",
            {"status": "success", "items_found": 2, "http_status": 200},
        )
        assert status == 201 and outcome["health"]["state"] == "healthy", outcome

        status, outcome = call(
            "POST",
            f"/sources/{source_id}/outcomes",
            {
                "status": "failure",
                "error": "upstream 503",
                "retry_after_seconds": 60,
                "http_status": 503,
            },
        )
        assert status == 201 and outcome["health"]["state"] == "degraded", outcome

        status, policy = call(
            "PUT",
            f"/domain-policies/{domain}",
            {
                "max_concurrency": 2,
                "requests_per_minute": 60,
                "crawl_delay_ms": 500,
                "robots_policy": "respect",
                "allowed": True,
                "notes": "smoke",
            },
        )
        assert status == 200 and policy["domain"] == domain, policy

        status, health = call("GET", f"/sources/{source_id}/health")
        assert status == 200 and health["state"] == "degraded", health

        status, history = call("GET", f"/sources/{source_id}/history")
        assert status == 200 and len(history["items"]) == 2, history

        status, versions = call("GET", f"/sources/{source_id}/versions")
        assert status == 200 and len(versions["items"]) >= 2, versions
    finally:
        call("DELETE", f"/sources/{source_id}")
        try:
            call("DELETE", f"/domain-policies/{domain}")
        except RuntimeError:
            pass
    print("sources smoke: ok")


if __name__ == "__main__":
    main()
