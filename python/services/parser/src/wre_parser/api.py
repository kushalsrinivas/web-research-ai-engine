"""Internal ETL API: status, reprocessing, backfills, quality, rollback."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiohttp import web

from . import queue
from .pipeline import activate_version
from .runtime import ETL_KEY


def mount(app: web.Application) -> None:
    app.router.add_get("/internal/v1/etl/stats", stats)
    app.router.add_get("/internal/v1/etl/jobs", list_jobs)
    app.router.add_get("/internal/v1/etl/documents/{document_id}", document_detail)
    app.router.add_get("/internal/v1/etl/versions", versions)
    app.router.add_get("/internal/v1/etl/quality", quality)
    app.router.add_post("/internal/v1/etl/reprocess", reprocess)
    app.router.add_post("/internal/v1/etl/rollback", rollback)
    app.router.add_post("/internal/v1/etl/dispatch", dispatch)


async def stats(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    snapshot = await queue.stats(runtime.db)
    snapshot["processing_version"] = runtime.config.processing_version
    return _json(snapshot)


async def list_jobs(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    status = request.query.get("status")
    if status and status not in {"pending", "running", "done", "failed", "dead_letter"}:
        return _error(400, "invalid status filter")
    limit = _limit(request.query.get("limit"), 50)
    jobs = await queue.list_jobs(runtime.db, status, request.query.get("document_id"), limit)
    return _json({"jobs": jobs})


async def document_detail(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    detail = await queue.document_detail(runtime.db, request.match_info["document_id"])
    if detail is None:
        return _error(404, "document not found")
    return _json(detail)


async def versions(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    rows = await queue.versions(runtime.db)
    return _json({"versions": rows, "active": runtime.config.processing_version})


async def quality(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    document_id = request.query.get("document_id")
    if not document_id:
        return _error(400, "document_id is required")
    rows = await queue.quality_comparison(runtime.db, document_id)
    if not rows:
        return _error(404, "no outputs for document")
    return _json({"document_id": document_id, "outputs": rows})


async def reprocess(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid json")
    if not isinstance(body, dict):
        return _error(400, "body must be an object")

    mode = str(body.get("mode", "reparse"))
    if mode not in {"reparse", "backfill"}:
        return _error(400, "mode must be reparse or backfill")
    target_version = str(body.get("target_version") or runtime.config.processing_version)
    if target_version != runtime.config.processing_version:
        return _error(
            400,
            "only the active processing version can be produced; use rollback for older outputs",
        )

    document_ids = body.get("document_ids")
    if document_ids is not None and (
        not isinstance(document_ids, list)
        or not all(isinstance(item, str) for item in document_ids)
    ):
        return _error(400, "document_ids must be a list of strings")

    try:
        from_time = _timestamp(body.get("from"))
        to_time = _timestamp(body.get("to"))
    except ValueError as exc:
        return _error(400, str(exc))
    if from_time and to_time and from_time > to_time:
        return _error(400, "from must be before to")

    inserted = await queue.enqueue_reprocess(
        runtime.db,
        target_version=target_version,
        mode=mode,
        document_ids=document_ids,
        source_id=_optional(body.get("source_id")),
        from_time=from_time,
        to_time=to_time,
        reason=str(body.get("reason") or "manual reprocess")[:500],
        requested_by=str(body.get("requested_by") or "operator")[:200],
        limit=_limit(body.get("limit"), 1000),
    )
    return _json({"mode": mode, "queued": inserted, "target_version": target_version}, status=202)


async def rollback(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid json")
    document_id = str(body.get("document_id") or "")
    version = str(body.get("processing_version") or "")
    if not document_id or not version:
        return _error(400, "document_id and processing_version are required")
    revision = body.get("revision")
    if revision is None:
        detail = await queue.document_detail(runtime.db, document_id)
        revision = detail["current_revision"] if detail else None
    if not isinstance(revision, int):
        return _error(400, "document not found")
    result = await activate_version(
        runtime.db, document_id, revision, version, runtime.config.min_quality
    )
    if result is None:
        return _error(404, "output not found")
    return _json(result)


async def dispatch(request: web.Request) -> web.Response:
    runtime = request.app[ETL_KEY]
    recovered = await queue.recover_leases(runtime.db)
    inserted = await queue.dispatch(runtime.db, runtime.config.processing_version, 500)
    return _json({"queued": inserted, "recovered_leases": recovered})


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


def _timestamp(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {raw!r}") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _limit(raw: Any, fallback: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return min(max(value, 1), 10000)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
