#!/usr/bin/env python3
"""Phase 6 smoke: entities, events, claims, spans, and confidence separation."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

SCHEDULER = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:8081") + "/internal/v1"
ETL = os.environ.get("PARSER_URL", "http://127.0.0.1:8085") + "/internal/v1"
GRAPH = os.environ.get("GRAPH_URL", "http://127.0.0.1:8087") + "/internal/v1"
CRAWL_TERMINAL = {"archived", "failed", "dead_lettered", "skipped"}
ETL_TERMINAL = {"deduplicated", "duplicate", "quarantined", "failed"}
RUN_TERMINAL = {"done", "partial", "failed", "quarantined"}


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
            if exc.code == 404:
                return 404, {"error": detail}
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def crawl(url: str) -> str:
    status, created = call(SCHEDULER, "POST", "/crawl/jobs", {"url": url, "lane": "interactive"})
    assert status in (200, 202) and created.get("job", {}).get("job_id"), created
    job_id = created["job"]["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        _, job = call(SCHEDULER, "GET", f"/crawl/jobs/{job_id}")
        if job.get("status") in CRAWL_TERMINAL:
            assert job["status"] == "archived", job
            return str(job["document_id"])
        time.sleep(1)
    raise AssertionError(f"crawl timed out: {url}")


def wait_etl(document_id: str) -> dict:
    deadline = time.time() + 120
    while time.time() < deadline:
        status, detail = call(ETL, "GET", f"/etl/documents/{document_id}")
        if status == 200 and detail.get("state") in ETL_TERMINAL:
            return detail
        time.sleep(2)
    raise AssertionError(f"etl timed out: {document_id}")


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    document_id = crawl(f"http://api:8080/internal/v1/fixtures/article?intel={suffix}")
    wait_etl(document_id)

    status, queued = call(GRAPH, "POST", "/intel/extract", {"document_id": document_id})
    assert status == 202, queued

    deadline = time.time() + 120
    detail: dict = {}
    while time.time() < deadline:
        status, detail = call(GRAPH, "GET", f"/intel/documents/{document_id}")
        if status == 200 and (detail.get("run") or {}).get("status") in RUN_TERMINAL:
            break
        time.sleep(2)
    run = detail.get("run") or {}
    assert run.get("status") in {"done", "partial"}, run
    assert run.get("claim_count", 0) >= 4, run
    assert run.get("event_count", 0) >= 2, run
    assert run.get("quarantined_count", 0) == 0, run

    body = detail["document"]["body"]
    claims = detail.get("claims") or []
    events = detail.get("events") or []
    mentions = detail.get("mentions") or []
    surfaces = {(mention["surface_form"], mention["entity_type"]) for mention in mentions}
    assert ("Tata Electronics", "organization") in surfaces, surfaces
    assert ("Gujarat", "location") in surfaces, surfaces
    assert (
        "Ministry of Electronics and Information Technology",
        "government",
    ) in surfaces, surfaces

    predicates = {claim["predicate"] for claim in claims}
    assert {"announced_investment", "has_value", "located_in"} <= predicates, predicates
    amount_claim = next(claim for claim in claims if claim["predicate"] == "has_value")
    assert float(amount_claim["amount"]) == 150_000_000_000, amount_claim
    assert amount_claim["currency"] == "INR", amount_claim
    assert amount_claim["claim_type"] == "financial", amount_claim
    assert amount_claim["modality"] == "planned", amount_claim
    assert amount_claim["commitment_status"] == "planned", amount_claim
    assert amount_claim["claim_support_confidence"] == 1.0, amount_claim
    assert amount_claim["verification_confidence"] is None, amount_claim
    assert 0 <= amount_claim["extraction_confidence"] <= 1, amount_claim

    for claim in claims:
        assert body[claim["char_start"] : claim["char_end"]] == claim["quote"], claim
        assert claim["span_verified"] is True, claim

    investment = next(event for event in events if event["event_type"] == "investment_announcement")
    assert float(investment["amount"]) == 150_000_000_000, investment
    assert investment["currency"] == "INR", investment
    assert investment["subject_entity_id"], investment
    assert investment["event_classification_confidence"] > 0, investment
    policy = next(event for event in events if event["event_type"] == "policy_approval")
    assert policy["commitment_status"] == "approved", policy
    for event in events:
        assert body[event["char_start"] : event["char_end"]] == event["quote"], event

    _, subject_claims = call(
        GRAPH,
        "GET",
        f"/intel/claims?subject_entity_id={investment['subject_entity_id']}&limit=20",
    )
    assert subject_claims.get("claims"), subject_claims
    _, groups = call(GRAPH, "GET", "/intel/claim-groups?limit=10")
    assert groups.get("groups"), groups
    _, versions = call(GRAPH, "GET", "/intel/versions")
    assert any(
        version["version"] == run["extraction_version"] for version in versions["versions"]
    ), versions

    print(
        "intel smoke: ok "
        f"(doc {document_id}, mentions {len(mentions)}, events {len(events)}, "
        f"claims {len(claims)})"
    )


if __name__ == "__main__":
    main()
