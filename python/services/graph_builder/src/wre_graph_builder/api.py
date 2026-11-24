"""Internal APIs for dedup, story clusters, lineage, and entity resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiohttp import web

from . import queue, repository
from .resolver import EntityInput
from .runtime import GRAPH_KEY
from .service import ENTITY_TYPES


def mount(app: web.Application) -> None:
    app.router.add_get("/internal/v1/dedup/stats", dedup_stats)
    app.router.add_post("/internal/v1/dedup/scan", dedup_scan)
    app.router.add_post("/internal/v1/dedup/documents/{document_id}", dedup_enqueue)
    app.router.add_get("/internal/v1/dedup/clusters", list_clusters)
    app.router.add_get("/internal/v1/dedup/clusters/{cluster_id}", cluster_detail)
    app.router.add_get("/internal/v1/dedup/documents/{document_id}", document_dedup)
    app.router.add_get("/internal/v1/dedup/evidence", evidence)
    app.router.add_post("/internal/v1/dedup/semantic-candidates", add_semantic_candidate)
    app.router.add_get("/internal/v1/dedup/semantic-candidates", list_semantic_candidates)
    app.router.add_post(
        "/internal/v1/dedup/semantic-candidates/{candidate_id}/review", review_semantic_candidate
    )
    app.router.add_get("/internal/v1/graph/stats", graph_stats)
    app.router.add_get("/internal/v1/graph/entities", list_entities)
    app.router.add_post("/internal/v1/graph/entities", create_entity)
    app.router.add_get("/internal/v1/graph/entities/{entity_id}", get_entity)
    app.router.add_post("/internal/v1/graph/entities/{entity_id}/merge", merge_entity)
    app.router.add_post("/internal/v1/graph/resolve", resolve)
    app.router.add_get("/internal/v1/graph/candidates", list_candidates)
    app.router.add_post("/internal/v1/graph/candidates/{candidate_id}/review", review_candidate)


async def dedup_stats(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    snapshot = await repository.dedup_stats(runtime.db)
    snapshot["jobs"] = (await queue.stats(runtime.db))["jobs"]
    return _json(snapshot)


async def dedup_scan(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    recovered = await queue.recover_leases(runtime.db)
    inserted = await queue.dispatch(runtime.db, 500)
    return _json({"queued": inserted, "recovered_leases": recovered})


async def dedup_enqueue(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    inserted = await repository.enqueue_document(runtime.db, request.match_info["document_id"])
    return _json({"queued": inserted}, status=202)


async def list_clusters(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    clusters = await repository.list_clusters(runtime.db, _limit(request.query.get("limit"), 50))
    return _json({"clusters": clusters})


async def cluster_detail(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    cluster = await repository.cluster_detail(runtime.db, request.match_info["cluster_id"])
    if cluster is None:
        return _error(404, "cluster not found")
    return _json(cluster)


async def document_dedup(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    detail = await repository.document_dedup(runtime.db, request.match_info["document_id"])
    if detail is None:
        return _error(404, "document is not clustered")
    return _json(detail)


async def evidence(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    cluster_id = request.query.get("cluster_id")
    document_id = request.query.get("document_id")
    if cluster_id:
        return _json(await repository.evidence_for_cluster(runtime.db, cluster_id))
    if document_id:
        result = await repository.evidence_for_document(runtime.db, document_id)
        if result is None:
            return _error(404, "document is not clustered")
        return _json(result)
    return _error(400, "cluster_id or document_id is required")


async def add_semantic_candidate(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid json")
    document_id = str(body.get("document_id") or "")
    candidate_id = str(body.get("candidate_document_id") or "")
    if not document_id or not candidate_id or document_id == candidate_id:
        return _error(400, "document_id and candidate_document_id are required and distinct")
    try:
        similarity = float(body.get("similarity", 0))
    except (TypeError, ValueError):
        return _error(400, "similarity must be a number")
    if not 0 <= similarity <= 1:
        return _error(400, "similarity must be in 0..1")
    rows = await runtime.db.fetch_all(
        "select tenant_id from core.documents where document_id = any(%s::text[])",
        ([document_id, candidate_id],),
    )
    if len(rows) != 2:
        return _error(404, "document or candidate not found")
    row = rows[0]
    created = await repository.insert_semantic_candidate(
        runtime.db,
        tenant_id=row["tenant_id"],
        document_id=document_id,
        candidate_document_id=candidate_id,
        similarity=similarity,
        model_version=str(body.get("model_version") or "manual-1.0.0"),
        features=body.get("features") if isinstance(body.get("features"), dict) else {},
    )
    return _json({"candidate_id": created, "decision_status": "review"}, status=202)


async def list_semantic_candidates(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    status = request.query.get("review_status")
    if status and status not in {"pending", "accepted", "rejected"}:
        return _error(400, "invalid review_status")
    rows = await repository.list_semantic_candidates(
        runtime.db, status, _limit(request.query.get("limit"), 50)
    )
    return _json({"candidates": rows})


async def review_semantic_candidate(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    review = str(body.get("review") or "")
    if review not in {"accepted", "rejected"}:
        return _error(400, "review must be accepted or rejected")
    row = await repository.review_semantic_candidate(
        runtime.db, request.match_info["candidate_id"], review
    )
    if row is None:
        return _error(404, "candidate not found")
    return _json(row)


async def graph_stats(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    return _json(await repository.graph_stats(runtime.db))


async def list_entities(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    entity_type = request.query.get("type")
    if entity_type and entity_type not in ENTITY_TYPES:
        return _error(400, "invalid entity type")
    rows = await repository.list_entities(
        runtime.db, request.query.get("query"), entity_type, _limit(request.query.get("limit"), 50)
    )
    return _json({"entities": rows})


async def create_entity(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    entity_type = str(body.get("entity_type") or "other")
    if not name:
        return _error(400, "name is required")
    if entity_type not in ENTITY_TYPES:
        return _error(400, "invalid entity_type")
    data = EntityInput(
        name=name,
        entity_type=entity_type,
        domain=_optional(body.get("domain")),
        country=_optional(body.get("country")),
        official_ids=body.get("official_ids")
        if isinstance(body.get("official_ids"), dict)
        else None,
    )
    entity: dict[str, Any] | None = await repository.create_entity(
        runtime.db, runtime.config.tenant_id, data, 0.8
    )
    if entity is None:
        return _error(500, "entity could not be created")
    for alias in body.get("aliases") or []:
        if isinstance(alias, str) and alias.strip():
            await repository.add_alias(runtime.db, entity["entity_id"], alias, entity_type)
    if body.get("description"):
        await runtime.db.execute(
            "update graph.entities set description = %s where entity_id = %s",
            (str(body["description"])[:2000], entity["entity_id"]),
        )
    entity = await repository.get_entity(runtime.db, entity["entity_id"])
    if entity is None:
        return _error(404, "entity not found")
    return _json(entity, status=201)


async def get_entity(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    entity = await repository.get_entity(runtime.db, request.match_info["entity_id"])
    if entity is None:
        return _error(404, "entity not found")
    return _json(entity)


async def merge_entity(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    into = str(body.get("into_entity_id") or "")
    if not into:
        return _error(400, "into_entity_id is required")
    try:
        confidence = float(body.get("confidence", 0.9))
    except (TypeError, ValueError):
        return _error(400, "confidence must be a number")
    entity = await repository.merge_entities(
        runtime.db,
        request.match_info["entity_id"],
        into,
        str(body.get("method") or "operator"),
        min(max(confidence, 0.0), 1.0),
        str(body.get("performed_by") or "api"),
    )
    if entity is None:
        return _error(404, "entity not found or invalid merge")
    return _json(entity)


async def resolve(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return _error(400, "name is required")
    entity_type = body.get("entity_type")
    if entity_type is not None and entity_type not in ENTITY_TYPES:
        return _error(400, "invalid entity_type")
    data = EntityInput(
        name=name,
        entity_type=entity_type,
        domain=_optional(body.get("domain")),
        country=_optional(body.get("country")),
        official_ids=body.get("official_ids")
        if isinstance(body.get("official_ids"), dict)
        else None,
    )
    result = await runtime.entities.resolve(
        runtime.config.tenant_id, data, create_missing=bool(body.get("create_missing", True))
    )
    return _json(result)


async def list_candidates(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    review_status = request.query.get("review_status")
    if review_status and review_status not in {"pending", "accepted", "rejected", "not_required"}:
        return _error(400, "invalid review_status")
    rows = await repository.list_candidates(
        runtime.db, review_status, _limit(request.query.get("limit"), 50)
    )
    return _json({"candidates": rows})


async def review_candidate(request: web.Request) -> web.Response:
    runtime = request.app[GRAPH_KEY]
    body = await _body(request)
    review = str(body.get("review") or "")
    if review not in {"accepted", "rejected"}:
        return _error(400, "review must be accepted or rejected")
    result = await runtime.entities.review(request.match_info["candidate_id"], review)
    if result is None:
        return _error(404, "candidate not found")
    return _json(result)


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


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
