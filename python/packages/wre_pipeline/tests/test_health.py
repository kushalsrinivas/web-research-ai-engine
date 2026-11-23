from __future__ import annotations

import logging

from aiohttp.test_utils import TestClient, TestServer
from wre_pipeline.health import Check, create_app


async def test_healthz_and_metrics() -> None:
    app = create_app("api", "api@dev", [], logging.getLogger("test"))
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz")
        assert response.status == 200
        assert (await response.json())["status"] == "ok"

        metrics = await client.get("/metrics")
        body = await metrics.text()
        assert "wre_http_requests_total" in body
        assert 'route="/healthz"' in body


async def test_readyz_reports_failed_check() -> None:
    async def unavailable() -> None:
        raise RuntimeError("dependency down")

    app = create_app(
        "api",
        "api@dev",
        [Check("database", unavailable)],
        logging.getLogger("test"),
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")
        assert response.status == 503
        body = await response.json()
        assert body["status"] == "unavailable"
        assert body["checks"] == {"database": "error"}
