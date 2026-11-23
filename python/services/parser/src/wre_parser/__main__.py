from __future__ import annotations

import logging

from aiohttp import web
from wre_pipeline.config import Config
from wre_pipeline.health import LOGGER_KEY, Check
from wre_pipeline.service import run_service

from . import api
from .config import EtlConfig
from .config import load as load_etl
from .runtime import ETL_KEY, Runtime

_ETL_CONFIG_KEY: web.AppKey[EtlConfig] = web.AppKey("etl_config", EtlConfig)
_runtime: dict[str, Runtime] = {}


async def _postgres_check() -> None:
    runtime = _runtime.get("runtime")
    if runtime is None:
        raise RuntimeError("etl runtime is not started")
    await runtime.db.ping()


async def _s3_check() -> None:
    runtime = _runtime.get("runtime")
    if runtime is None:
        raise RuntimeError("etl runtime is not started")
    await runtime.store.ping()


async def _startup(app: web.Application) -> None:
    config = app[_ETL_CONFIG_KEY]
    logger = app[LOGGER_KEY]
    runtime = await Runtime.create(config, logger)
    app[ETL_KEY] = runtime
    _runtime["runtime"] = runtime
    await runtime.start()


async def _cleanup(app: web.Application) -> None:
    runtime = app.get(ETL_KEY)
    _runtime.pop("runtime", None)
    if runtime is not None:
        await runtime.stop()


def configure(_: Config, app: web.Application) -> None:
    app[_ETL_CONFIG_KEY] = load_etl()
    api.mount(app)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)


def main() -> None:
    logging.getLogger("wre.parser").setLevel(logging.INFO)
    run_service(
        "parser",
        8085,
        checks=[Check("postgres", _postgres_check), Check("s3", _s3_check)],
        configure=configure,
    )


if __name__ == "__main__":
    main()
