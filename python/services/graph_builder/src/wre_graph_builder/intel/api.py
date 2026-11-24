"""Internal API for extraction runs, mentions, events, claims, and quarantine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiohttp import web

from ..runtime import GRAPH_KEY
from . import queue, repository


def mount(app: web.Application) -> None:
    app.router.add_get("/internal/v1/intel/stats", stats)
    app.router.add_post("/internal/v1/intel/scan", scan)
    app.router.add_post("/internal/v1/intel/extract", extract)
    app.router.add_get("/internal/v1/intel/documents/{document_id}", document)
    app.router.add_get("/internal/v1/intel/claims", claims)
    app.router.add_get("/internal/v1/intel/claim-groups", claim_groups)
    app.router.add_get("/internal/v1/intel/events", events)
    app.router.add_get("/internal/v1/intel/quarantine", quarantine)
    app.router.add_get("/internal/v1/intel/versions", versions)


async def stats(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    snapshot = await repository.stats(runtime.db)
    snapshot["jobs"] = await queue.stats(runtime.db)
    snapshot["version"] = runtime.config.extraction_version
    return _json(snapshot)


async def scan(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    recovered = await queue.recover_leases(runtime.db)
    inserted = await queue.dispatch(runtime.db, runtime.config.extraction_version, 500)
    return _json({"queued": inserted, "recovered_leases": recovered})


async def extract(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    document_id = _optional(body.get("document_id"))
    if document_id:
        inserted = await queue.enqueue_document(runtime.db, document_id)
        return _json({"queued": inserted, "document_id": document_id}, status=202)
    inserted = await queue.dispatch(runtime.db, runtime.config.extraction_version, 500)
    return _json({"queued": inserted}, status=202)


async def document(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    detail = await repository.document_intel(runtime.db, request.match_info["document_id"])
    if detail is None:
        return _error(404, "document not found")
    return _json(detail)


async def claims(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    rows = await repository.list_claims(
        runtime.db,
        subject_entity_id=_optional(request.query.get("subject_entity_id")),
        claim_key=_optional(request.query.get("claim_key")),
        claim_type=_optional(request.query.get("claim_type")),
        limit=_limit(request.query.get("limit"), 100),
    )
    return _json({"claims": rows})


async def claim_groups(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    rows = await repository.claim_groups(runtime.db, _limit(request.query.get("limit"), 50))
    return _json({"groups": rows})


async def events(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    rows = await repository.list_events(
        runtime.db,
        event_type=_optional(request.query.get("event_type")),
        subject_entity_id=_optional(request.query.get("subject_entity_id")),
        limit=_limit(request.query.get("limit"), 100),
    )
    return _json({"events": rows})


async def quarantine(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    rows = await repository.quarantine_rows(runtime.db, _limit(request.query.get("limit"), 50))
    return _json({"quarantine": rows})


async def versions(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    return _json(
        {
            "versions": await repository.versions(runtime.db),
            "active": runtime.config.extraction_version,
        }
    )


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _json(value: Any, status: int = 200) -> web.Response:
    return web.json_response(
        value, status=status, dumps=_dumps, headers={"Cache-Control": "no-store"}
    )


def _error(status: int, message: str) -> web.Response:
    return _json({"error": message}, status=status)


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=_default, ensure_ascii=False)


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not json serializable: {type(value).__name__}")


def _limit(raw: Any, fallback: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return min(max(value, 1), 1000)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
