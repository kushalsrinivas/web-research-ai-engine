"""Async Postgres access for the ETL service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self, dsn: str) -> None:
        self._pool: Any = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=4,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("select 1")

    async def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, params)
            return cast(dict[str, Any] | None, await cursor.fetchone())

    async def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, params)
            return [cast(dict[str, Any], row) for row in await cursor.fetchall()]

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, params)
            return cursor.rowcount or 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as conn, conn.transaction():
            yield conn
