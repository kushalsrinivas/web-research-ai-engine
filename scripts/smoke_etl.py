#!/usr/bin/env python3
"""ETL smoke: crawl a page, normalize it, quarantine an invalid page, reprocess."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

SCHEDULER = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:8081") + "/internal/v1"
PARSER = os.environ.get("PARSER_URL", "http://127.0.0.1:8085") + "/internal/v1"
TERMINAL_CRAWL = {"archived", "failed", "dead_lettered", "skipped"}
TERMINAL_ETL = {"deduplicated", "duplicate", "quarantined", "failed"}


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def crawl(url: str, timeout: float = 120) -> str:
    status, created = call(SCHEDULER, "POST", "/crawl/jobs", {"url": url, "lane": "interactive"})
    assert status in (200, 202) and created.get("job"), created
    job_id = str(created["job"].get("job_id") or "")
    assert job_id, created
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, job = call(SCHEDULER, "GET", f"/crawl/jobs/{job_id}")
        if job.get("status") in TERMINAL_CRAWL:
            assert job["status"] == "archived", job
            return str(job["document_id"])
        time.sleep(1)
    raise AssertionError(f"crawl {url} did not finish: {job}")


def wait_etl(document_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        _, detail = call(PARSER, "GET", f"/etl/documents/{document_id}")
        if detail.get("state") in TERMINAL_ETL:
            return detail
        time.sleep(2)
    raise AssertionError(f"etl for {document_id} did not finish: {detail}")


def main() -> None:
    good = crawl("http://api:8080/metrics")
    detail = wait_etl(good)
    assert detail["state"] in {"deduplicated", "duplicate"}, detail
    outputs = detail.get("outputs") or []
    assert outputs, detail
    output = outputs[-1]
    assert output["quality_score"] > 0, output
    assert str(output["canonical_uri"]).startswith("s3://"), output
    assert str(output["parsed_uri"]).startswith("s3://"), output
    revision = detail["current_revision"]

    bad = crawl("http://api:8080/readyz")
    bad_detail = wait_etl(bad)
    assert bad_detail["state"] == "quarantined", bad_detail
    assert str(bad_detail.get("failed_stage", "")).startswith("etl."), bad_detail

    status, queued = call(
        PARSER,
        "POST",
        "/etl/reprocess",
        {
            "document_ids": [good],
            "mode": "reparse",
            "target_version": output["processing_version"],
            "reason": "smoke",
            "requested_by": "smoke",
        },
    )
    assert status == 202 and queued.get("queued", 0) >= 1, queued

    deadline = time.time() + 60
    ready = False
    while time.time() < deadline and not ready:
        _, jobs = call(PARSER, "GET", f"/etl/jobs?document_id={good}&limit=20")
        rows = jobs.get("jobs", [])
        active = any(job["status"] in {"pending", "running"} for job in rows)
        done = any(job["mode"] == "reparse" and job["status"] == "done" for job in rows)
        ready = done and not active
        time.sleep(1)
    assert ready, "reparse job did not complete"

    status, backfill = call(
        PARSER,
        "POST",
        "/etl/reprocess",
        {"document_ids": [good], "mode": "backfill", "reason": "smoke backfill"},
    )
    assert status == 202 and backfill.get("queued", 0) >= 1, backfill

    _, comparison = call(PARSER, "GET", f"/etl/quality?document_id={good}")
    assert comparison.get("outputs"), comparison
    print(
        "etl smoke: ok "
        f"(doc {good} rev {revision}, quality {output['quality_score']}, "
        f"quarantined {bad})"
    )


if __name__ == "__main__":
    main()
