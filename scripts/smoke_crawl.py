#!/usr/bin/env python3
"""Crawler smoke: enqueue one URL, wait for the archived job. Stdlib only."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:8081") + "/internal/v1"
TARGET = os.environ.get("CRAWL_SMOKE_URL", "http://api:8080/healthz")
TERMINAL = {"archived", "failed", "dead_lettered", "skipped"}


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
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
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def main() -> None:
    status, created = call("POST", "/crawl/jobs", {"url": TARGET, "lane": "interactive"})
    assert status in (200, 202) and created.get("job"), (status, created)
    job_id = created["job"]["job_id"]

    deadline = time.time() + 120
    job: dict = {}
    while time.time() < deadline:
        _, job = call("GET", f"/crawl/jobs/{job_id}")
        if job.get("status") in TERMINAL:
            break
        time.sleep(2)
    assert job.get("status") == "archived", job
    assert str(job.get("artifact_uri", "")).startswith("s3://"), job

    _, stats = call("GET", "/crawl/stats")
    assert int(stats.get("jobs_total", 0)) >= 1, stats
    print(f"crawl smoke: ok (job {job_id}, artifact {job['artifact_uri']})")


if __name__ == "__main__":
    main()
