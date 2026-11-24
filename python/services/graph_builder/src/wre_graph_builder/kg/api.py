"""Temporal knowledge-graph API and named queries."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiohttp import web

from ..runtime import GRAPH_KEY
from . import builder, integrity, queries


def mount(app: web.Application) -> None:
    app.router.add_get("/internal/v1/kg/stats", stats)
    app.router.add_get("/internal/v1/kg/versions", versions)
    app.router.add_get("/internal/v1/kg/relationships", relationships)
    app.router.add_get("/internal/v1/kg/entities/{entity_id}/neighborhood", neighborhood)
    app.router.add_get("/internal/v1/kg/events/{event_id}/entities", event_entities)
    app.router.add_get("/internal/v1/kg/integrity", integrity_check)
    app.router.add_post("/internal/v1/kg/rebuild", rebuild)
    app.router.add_post("/internal/v1/kg/query", query)


async def stats(request: web.Request) -> web.Response:
    return _json(await queries.stats(request.app[GRAPH_KEY].db))


async def versions(request: web.Request) -> web.Response:
    rows = await queries.versions(request.app[GRAPH_KEY].db, _limit(request.query.get("limit"), 20))
    return _json({"versions": rows})


async def relationships(request: web.Request) -> web.Response:
    rows = await queries.relationships(
        request.app[GRAPH_KEY].db,
        relationship_type=request.query.get("type", ""),
        entity_id=request.query.get("entity_id", ""),
        as_of=request.query.get("as_of"),
        limit=_limit(request.query.get("limit"), 100),
    )
    return _json({"relationships": rows})


async def neighborhood(request: web.Request) -> web.Response:
    depth = _depth(request.query.get("depth"))
    rows = await queries.entity_neighborhood(
        request.app[GRAPH_KEY].db, request.match_info["entity_id"], depth
    )
    return _json({"entity_id": request.match_info["entity_id"], "neighborhood": rows})


async def event_entities(request: web.Request) -> web.Response:
    rows = await queries.event_entities(request.app[GRAPH_KEY].db, request.match_info["event_id"])
    return _json({"event_id": request.match_info["event_id"], "entities": rows})


async def integrity_check(request: web.Request) -> web.Response:
    findings = await integrity.issues(request.app[GRAPH_KEY].db)
    return _json(
        {
            "issues": findings,
            "errors": sum(1 for finding in findings if finding["severity"] == "error"),
            "warnings": sum(1 for finding in findings if finding["severity"] == "warning"),
        }
    )


async def rebuild(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    result = await builder.build(runtime.db, runtime.logger, "manual rebuild")
    return _json(result, status=202)


async def query(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    name = str(body.get("name") or "")
    raw_params = body.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    db = runtime.db
    if name == "investments_by_location_industry":
        rows = await queries.investments_by_location_industry(
            db, str(params.get("location") or ""), str(params.get("industry") or "")
        )
    elif name == "policies_for_investments":
        rows = await queries.policies_for_investments(db, _limit(params.get("limit"), 50))
    elif name == "shared_suppliers":
        rows = await queries.shared_suppliers(db, _limit(params.get("limit"), 50))
    elif name == "delayed_projects":
        rows = await queries.delayed_projects(db, _limit(params.get("limit"), 50))
    elif name == "developments_after_policy":
        rows = await queries.developments_after_policy(
            db, str(params.get("policy") or ""), _limit(params.get("limit"), 50)
        )
    elif name == "claims_multiple_sources":
        rows = await queries.claims_multiple_sources(
            db, max(1, int(params.get("min_sources", 2))), _limit(params.get("limit"), 50)
        )
    elif name == "connected_to_event":
        rows = await queries.connected_to_event(
            db, str(params.get("event_id") or ""), _depth(params.get("depth"))
        )
    elif name == "entity_neighborhood":
        rows = await queries.entity_neighborhood(
            db, str(params.get("entity_id") or ""), _depth(params.get("depth"))
        )
    elif name == "event_entities":
        rows = await queries.event_entities(db, str(params.get("event_id") or ""))
    else:
        return _error(400, f"unknown query {name!r}")
    return _json({"name": name, "rows": rows})


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
    return min(max(value, 1), 500)


def _depth(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return min(max(value, 1), 4)
