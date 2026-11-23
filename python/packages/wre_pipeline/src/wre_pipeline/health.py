"""Probe-only HTTP surface shared by Python services."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE_UP = Gauge(
    "wre_service_up",
    "1 while the service process is running.",
    ["version"],
)
REQUEST_COUNT = Counter(
    "wre_http_requests_total",
    "HTTP requests handled, by route, method and status class.",
    ["route", "method", "status_class"],
)
REQUEST_DURATION = Histogram(
    "wre_http_request_duration_seconds",
    "HTTP request latency, by route and method.",
    ["route", "method"],
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    fn: Callable[[], Awaitable[None]]


SERVICE_KEY: web.AppKey[str] = web.AppKey("service", str)
VERSION_KEY: web.AppKey[str] = web.AppKey("processor_version", str)
CHECKS_KEY: web.AppKey[Sequence[Check]] = web.AppKey("checks", Sequence[Check])
LOGGER_KEY: web.AppKey[logging.Logger] = web.AppKey("logger", logging.Logger)


def create_app(
    service: str,
    processor_version: str,
    checks: Sequence[Check],
    logger: logging.Logger,
) -> web.Application:
    app = web.Application(middlewares=[_logging_middleware, _metrics_middleware])
    app[SERVICE_KEY] = service
    app[VERSION_KEY] = processor_version
    app[CHECKS_KEY] = checks
    app[LOGGER_KEY] = logger
    SERVICE_UP.labels(version=processor_version).set(1)

    app.router.add_get("/healthz", _healthz, name="healthz")
    app.router.add_get("/readyz", _readyz, name="readyz")
    app.router.add_get("/metrics", _metrics, name="metrics")
    return app


async def _healthz(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": request.app[SERVICE_KEY],
            "version": request.app[VERSION_KEY],
        },
        headers={"Cache-Control": "no-store"},
    )


async def _readyz(request: web.Request) -> web.Response:
    results: dict[str, str] = {}
    ready = True
    checks = request.app[CHECKS_KEY]
    for check in checks:
        try:
            await asyncio.wait_for(check.fn(), timeout=2.0)
        except Exception:  # a failed dependency is a 503, not a crash
            results[check.name] = "error"
            ready = False
        else:
            results[check.name] = "ok"

    return web.json_response(
        {
            "status": "ok" if ready else "unavailable",
            "service": request.app[SERVICE_KEY],
            "version": request.app[VERSION_KEY],
            "checks": results,
        },
        status=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


async def _metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@web.middleware
async def _logging_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    start = time.perf_counter()
    logger = request.app[LOGGER_KEY]
    try:
        response = await handler(request)
    except Exception:
        logger.exception(
            "http request failed",
            extra={
                "route": _route(request),
                "method": request.method,
                "status": 500,
                "duration_ms": round((time.perf_counter() - start) * 1000),
            },
        )
        raise

    logger.info(
        "http request",
        extra={
            "route": _route(request),
            "method": request.method,
            "status": response.status,
            "duration_ms": round((time.perf_counter() - start) * 1000),
        },
    )
    return response


@web.middleware
async def _metrics_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    start = time.perf_counter()
    response = await handler(request)
    route = _route(request)
    REQUEST_COUNT.labels(
        route=route,
        method=request.method,
        status_class=_status_class(response.status),
    ).inc()
    REQUEST_DURATION.labels(route=route, method=request.method).observe(time.perf_counter() - start)
    return response


def _route(request: web.Request) -> str:
    resource = request.match_info.route.resource
    return resource.canonical if resource is not None else request.path


def _status_class(status: int) -> str:
    if status >= 500:
        return "5xx"
    if status >= 400:
        return "4xx"
    if status >= 300:
        return "3xx"
    return "2xx"
