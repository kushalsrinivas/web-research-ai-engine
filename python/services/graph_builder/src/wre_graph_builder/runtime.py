"""Graph service runtime: dedup dispatcher and workers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid

from aiohttp import web
from wre_pipeline.db import Database

from . import queue
from .config import GraphConfig
from .intel import pipeline as extraction_pipeline
from .intel import queue as extraction_queue
from .intel import repository as extraction_repo
from .kg import builder as kg_builder
from .metrics import (
    DEDUP_CLUSTERS,
    DEDUP_QUEUE_DEPTH,
    KG_BUILD_SECONDS,
    KG_REBUILDS,
    KG_RELATIONSHIPS,
)
from .service import EntityService
from .worker import DedupWorker


class Runtime:
    def __init__(
        self,
        config: GraphConfig,
        db: Database,
        entities: EntityService,
        dedup_worker: DedupWorker,
        extractor: extraction_pipeline.ExtractionWorker,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.db = db
        self.entities = entities
        self.dedup_worker = dedup_worker
        self.extractor = extractor
        self.logger = logger
        self.owner = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._jobs: set[asyncio.Task[None]] = set()
        self._extraction_jobs: set[asyncio.Task[None]] = set()

    @classmethod
    async def create(cls, config: GraphConfig, logger: logging.Logger) -> Runtime:
        db = Database(config.database_url)
        await db.open()
        entities = EntityService(db, logger)
        await extraction_repo.register_version(
            db, config.extraction_version, extraction_pipeline.extraction_config_hash()
        )
        return cls(
            config,
            db,
            entities,
            DedupWorker(db, config, logger),
            extraction_pipeline.ExtractionWorker(
                db, entities, config.extraction_version, config.max_extract_chars, logger
            ),
            logger,
        )

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="dedup-dispatcher"),
            asyncio.create_task(self._worker_loop(), name="dedup-worker"),
            asyncio.create_task(self._extraction_dispatch_loop(), name="extraction-dispatcher"),
            asyncio.create_task(self._extraction_worker_loop(), name="extraction-worker"),
        ]
        if self.config.kg_enabled:
            self._tasks.append(asyncio.create_task(self._projection_loop(), name="kg-projection"))
        self.logger.info(
            "graph runtime started",
            extra={"concurrency": self.config.concurrency, "tenant": self.config.tenant_id},
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._jobs:
            task.cancel()
        await asyncio.gather(*self._jobs, return_exceptions=True)
        for task in self._extraction_jobs:
            task.cancel()
        await asyncio.gather(*self._extraction_jobs, return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await queue.release_owner(self.db, self.owner)
        await extraction_queue.release_owner(self.db, self.owner)
        await self.db.close()
        self.logger.info("graph runtime stopped")

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                recovered = await queue.recover_leases(self.db)
                inserted = await queue.dispatch(self.db, 100)
                snapshot = await queue.stats(self.db)
                DEDUP_QUEUE_DEPTH.set(int(snapshot["jobs"].get("pending", 0)))
                DEDUP_CLUSTERS.set(int(snapshot["clusters"]))
                if recovered or inserted:
                    self.logger.info(
                        "dedup dispatch",
                        extra={"queued": inserted, "recovered_leases": recovered},
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("dedup dispatch failed")
            await _sleep(self._stop, self.config.dispatch_interval)

    async def _extraction_dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                recovered = await extraction_queue.recover_leases(self.db)
                inserted = await extraction_queue.dispatch(
                    self.db, self.config.extraction_version, 200
                )
                if recovered or inserted:
                    self.logger.info(
                        "extraction dispatch",
                        extra={"queued": inserted, "recovered_leases": recovered},
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("extraction dispatch failed")
            await _sleep(self._stop, self.config.extraction_dispatch_interval)

    async def _projection_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if await kg_builder.needs_rebuild(self.db):
                    started = __import__("time").monotonic()
                    result = await kg_builder.build(self.db, self.logger, "scheduled rebuild")
                    KG_RELATIONSHIPS.set(result["relationships"])
                    KG_BUILD_SECONDS.observe(__import__("time").monotonic() - started)
                    KG_REBUILDS.labels(status="done").inc()
            except asyncio.CancelledError:
                return
            except Exception:
                KG_REBUILDS.labels(status="failed").inc()
                self.logger.exception("graph projection failed")
            await _sleep(self._stop, self.config.kg_rebuild_interval)

    async def _extraction_worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                while len(self._extraction_jobs) < self.config.extraction_concurrency:
                    job = await extraction_queue.claim(
                        self.db, self.owner, self.config.extraction_lease_seconds
                    )
                    if job is None:
                        break
                    task = asyncio.create_task(self.extractor.process(job))
                    self._extraction_jobs.add(task)
                    task.add_done_callback(self._extraction_jobs.discard)
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("extraction claim failed")
            await _sleep(self._stop, self.config.extraction_poll_interval)

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                while len(self._jobs) < self.config.concurrency:
                    job = await queue.claim(self.db, self.owner, self.config.lease_seconds)
                    if job is None:
                        break
                    task = asyncio.create_task(self.dedup_worker.process(job))
                    self._jobs.add(task)
                    task.add_done_callback(self._jobs.discard)
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("dedup claim failed")
            await _sleep(self._stop, self.config.poll_interval)


GRAPH_KEY: web.AppKey[Runtime] = web.AppKey("graph", Runtime)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
