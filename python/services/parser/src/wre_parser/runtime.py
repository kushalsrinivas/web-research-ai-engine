"""Runtime wiring: database, object store, dispatcher, and worker tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid

from aiohttp import web

from . import queue
from .config import EtlConfig
from .db import Database
from .metrics import ETL_QUEUE_DEPTH
from .pipeline import Processor
from .storage import ObjectStore


class Runtime:
    def __init__(
        self,
        config: EtlConfig,
        db: Database,
        store: ObjectStore,
        processor: Processor,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.db = db
        self.store = store
        self.processor = processor
        self.logger = logger
        self.owner = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._jobs: set[asyncio.Task[None]] = set()

    @classmethod
    async def create(cls, config: EtlConfig, logger: logging.Logger) -> Runtime:
        db = Database(config.database_url)
        await db.open()
        store = ObjectStore(
            config.s3_endpoint,
            config.s3_access_key,
            config.s3_secret_key,
            config.s3_region,
            config.s3_bucket_raw,
            config.s3_bucket_parsed,
        )
        await queue.register_version(
            db, config.processing_version, "parser", processor_config_hash()
        )
        processor = Processor(db, store, config, logger)
        return cls(config, db, store, processor, logger)

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="etl-dispatcher"),
            asyncio.create_task(self._worker_loop(), name="etl-worker"),
        ]
        self.logger.info(
            "etl runtime started",
            extra={
                "version": self.config.processing_version,
                "concurrency": self.config.concurrency,
            },
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._jobs:
            task.cancel()
        await asyncio.gather(*self._jobs, return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await queue.release_owner(self.db, self.owner)
        await self.db.close()
        self.logger.info("etl runtime stopped")

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                recovered = await queue.recover_leases(self.db)
                inserted = await queue.dispatch(self.db, self.config.processing_version, 100)
                snapshot = await queue.stats(self.db)
                ETL_QUEUE_DEPTH.set(snapshot["queue_depth"])
                if recovered or inserted:
                    self.logger.info(
                        "etl dispatch",
                        extra={"queued": inserted, "recovered_leases": recovered},
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("etl dispatch failed")
            await _sleep(self._stop, self.config.dispatch_interval)

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                while len(self._jobs) < self.config.concurrency:
                    job = await queue.claim(self.db, self.owner, self.config.lease_seconds)
                    if job is None:
                        break
                    task = asyncio.create_task(self.processor.process(job))
                    self._jobs.add(task)
                    task.add_done_callback(self._jobs.discard)
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("etl claim failed")
            await _sleep(self._stop, self.config.poll_interval)


ETL_KEY: web.AppKey[Runtime] = web.AppKey("etl", Runtime)


def processor_config_hash() -> str:
    import hashlib
    import importlib.metadata as metadata

    parts = []
    for package in ("trafilatura", "pymupdf", "python-docx", "feedparser", "lxml", "py3langid"):
        try:
            parts.append(f"{package}=={metadata.version(package)}")
        except metadata.PackageNotFoundError:
            parts.append(f"{package}==missing")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
