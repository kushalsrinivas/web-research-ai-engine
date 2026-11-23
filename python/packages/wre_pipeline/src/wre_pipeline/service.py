"""Bootstrap shared by Python services."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from aiohttp import web

from .config import Config, load
from .health import Check, create_app
from .log import setup_logging
from .telemetry import init_telemetry


def run_service(
    service: str,
    default_port: int,
    checks: Sequence[Check] = (),
    configure: Callable[[Config, web.Application], None] | None = None,
) -> None:
    config = load(service, default_port)
    logger = setup_logging(config)
    shutdown_telemetry = init_telemetry(config)
    app = create_app(service, config.processor_version, checks, logger)
    if configure is not None:
        configure(config, app)

    try:
        logger.info(
            "service listening",
            extra={"port": config.port, "version": config.processor_version, "env": config.env},
        )
        web.run_app(
            app,
            host="0.0.0.0",
            port=config.port,
            print=None,
            access_log=None,
            shutdown_timeout=config.shutdown_timeout,
        )
    finally:
        shutdown_telemetry()
        logger.info("service stopped")
