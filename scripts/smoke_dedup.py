#!/usr/bin/env python3
"""Phase 5 smoke: near-duplicate clustering, lineage, and entity resolution."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

SCHEDULER = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:8081") + "/internal/v1"
GRAPH = os.environ.get("GRAPH_URL", "http://127.0.0.1:8087") + "/internal/v1"
ETL = os.environ.get("PARSER_URL", "http://127.0.0.1:8085") + "/internal/v1"
CRAWL_TERMINAL = {"archived", "failed", "dead_lettered", "skipped"}
ETL_TERMINAL = {"deduplicated", "duplicate", "quarantined", "failed"}


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


def wait_cluster(document_id: str) -> dict:
    deadline = time.time() + 120
    while time.time() < deadline:
        status, detail = call(GRAPH, "GET", f"/dedup/documents/{document_id}")
        if status == 200 and detail.get("member"):
            return detail
        time.sleep(2)
    raise AssertionError(f"dedup timed out: {document_id}")


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    first = crawl("http://api:8080/internal/v1/fixtures/article")
    second = crawl(f"http://api:8080/internal/v1/fixtures/article?dedup={suffix}")
    for document_id in (first, second):
        wait_etl(document_id)
        status, _ = call(GRAPH, "POST", f"/dedup/documents/{document_id}")
        assert status in (200, 202), status

    one = wait_cluster(first)
    two = wait_cluster(second)
    cluster_id = one["member"]["cluster_id"]
    assert two["member"]["cluster_id"] == cluster_id, (one, two)

    _, evidence = call(GRAPH, "GET", f"/dedup/evidence?cluster_id={cluster_id}")
    assert evidence["document_count"] >= 2, evidence
    assert evidence["independent_sources"] >= 1, evidence
    assert evidence["syndicated_copies"] >= 1, evidence
    assert evidence["inflation_ratio"] >= 1, evidence

    status, entity = call(
        GRAPH,
        "POST",
        "/graph/entities",
        {
            "name": "Tata Electronics",
            "entity_type": "organization",
            "domain": "tata.com",
            "country": "IN",
            "aliases": ["Tata Electronics Private Limited"],
            "official_ids": {"wikidata": "Q-SMOKE-TATA"},
        },
    )
    assert status in (200, 201) and entity.get("entity_id"), entity
    entity_id = entity["entity_id"]

    status, resolved = call(
        GRAPH,
        "POST",
        "/graph/resolve",
        {
            "name": "Tata Electronics Pvt Ltd",
            "entity_type": "organization",
            "domain": "tata.com",
            "country": "IN",
        },
    )
    assert status == 200 and resolved["decision"] == "auto_match", resolved
    assert resolved["entity"]["entity_id"] == entity_id, resolved

    status, ambiguous = call(
        GRAPH,
        "POST",
        "/graph/resolve",
        {
            "name": f"Tata Electronic {suffix}",
            "entity_type": "organization",
            "country": "IN",
        },
    )
    assert status == 200 and ambiguous["decision"] in {"review", "keep_separate"}, ambiguous
    if ambiguous["decision"] == "review":
        status, reviewed = call(
            GRAPH,
            "POST",
            f"/graph/candidates/{ambiguous['candidate_id']}/review",
            {"review": "accepted"},
        )
        assert status == 200 and reviewed["review_status"] == "accepted", reviewed

    status, separate = call(
        GRAPH,
        "POST",
        "/graph/resolve",
        {"name": f"Northwind Weather Systems {suffix}", "entity_type": "organization"},
    )
    assert status == 200, separate
    entity_id = (separate.get("entity") or {}).get("entity_id")
    if separate["decision"] == "review":
        _, accepted = call(
            GRAPH,
            "POST",
            f"/graph/candidates/{separate['candidate_id']}/review",
            {"review": "accepted"},
        )
        entity_id = accepted.get("resolved_entity_id") or accepted.get("candidate_entity_id")
    assert entity_id, separate

    status, _ = call(
        GRAPH,
        "POST",
        "/dedup/semantic-candidates",
        {
            "document_id": first,
            "candidate_document_id": second,
            "similarity": 0.93,
            "model_version": "smoke-1.0.0",
            "features": {"same_event": True},
        },
    )
    assert status == 202, status
    _, pending = call(GRAPH, "GET", "/dedup/semantic-candidates?review_status=pending&limit=20")
    assert any(
        candidate["document_id"] == first and candidate["candidate_document_id"] == second
        for candidate in pending["candidates"]
    ), pending

    _, stats = call(GRAPH, "GET", "/graph/stats")
    assert stats["entities"] >= 1, stats
    print(
        "dedup smoke: ok "
        f"(cluster {cluster_id}, docs {first}/{second}, independent "
        f"{evidence['independent_sources']}, entity {entity_id})"
    )


if __name__ == "__main__":
    main()
