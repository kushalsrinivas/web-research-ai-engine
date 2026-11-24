from __future__ import annotations

from aiohttp import web
from wre_pipeline.config import Config
from wre_pipeline.health import LOGGER_KEY, Check
from wre_pipeline.service import run_service

from . import api
from .config import GraphConfig
from .config import load as load_graph
from .intel import api as intel_api
from .kg import api as kg_api
from .runtime import GRAPH_KEY, Runtime

_CONFIG_KEY: web.AppKey[GraphConfig] = web.AppKey("graph_config", GraphConfig)
_runtime: dict[str, Runtime] = {}


async def _postgres_check() -> None:
    runtime = _runtime.get("runtime")
    if runtime is None:
        raise RuntimeError("graph runtime is not started")
    await runtime.db.ping()


async def _startup(app: web.Application) -> None:
    runtime = await Runtime.create(app[_CONFIG_KEY], app[LOGGER_KEY])
    app[GRAPH_KEY] = runtime
    _runtime["runtime"] = runtime
    await runtime.start()


async def _cleanup(app: web.Application) -> None:
    runtime = app.get(GRAPH_KEY)
    _runtime.pop("runtime", None)
    if runtime is not None:
        await runtime.stop()


def configure(_: Config, app: web.Application) -> None:
    app[_CONFIG_KEY] = load_graph()
    api.mount(app)
    intel_api.mount(app)
    kg_api.mount(app)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)


def main() -> None:
    run_service(
        "graph-builder",
        8087,
        checks=[Check("postgres", _postgres_check)],
        configure=configure,
    )


if __name__ == "__main__":
    main()
