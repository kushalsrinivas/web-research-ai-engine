"""Durable dedup job queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from wre_pipeline.db import Database


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    document_id: str
    revision: int
    attempts: int
    max_attempts: int


async def dispatch(db: Database, limit: int) -> int:
    return await db.execute(
        """
        insert into dedup.jobs (job_id, document_id, revision)
        select gen_random_uuid()::text, d.document_id, d.current_revision
        from core.documents d
        where d.state in ('deduplicated', 'duplicate')
          and exists (
            select 1 from processing.document_outputs o
            where o.document_id = d.document_id and o.revision = d.current_revision
          )
          and not exists (
            select 1 from dedup.story_members m
            where m.document_id = d.document_id and m.revision = d.current_revision
          )
          and not exists (
            select 1 from dedup.jobs j
            where j.document_id = d.document_id and j.revision = d.current_revision
              and j.status in ('pending', 'running', 'dead_letter', 'failed')
          )
        order by d.updated_at
        limit %s
        on conflict do nothing
        """,
        (limit,),
    )


async def recover_leases(db: Database) -> int:
    return await db.execute(
        """
        update dedup.jobs
        set status = 'pending', lease_owner = null, lease_expires_at = null,
            next_attempt_at = now(), updated_at = now()
        where status = 'running' and lease_expires_at < now()
        """
    )


async def claim(db: Database, owner: str, lease_seconds: int) -> Job | None:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            """
            update dedup.jobs j
            set status = 'running', lease_owner = %s,
                lease_expires_at = now() + (%s * interval '1 second'),
                attempts = attempts + 1, started_at = coalesce(started_at, now()),
                updated_at = now()
            where j.job_id = (
                select job_id from dedup.jobs
                where status = 'pending' and next_attempt_at <= now()
                order by created_at
                for update skip locked
                limit 1
            )
            returning job_id, document_id, revision, attempts, max_attempts
            """,
            (owner, lease_seconds),
        )
        row = await cursor.fetchone()
    return Job(**row) if row else None


async def finish(db: Database, job: Job) -> None:
    await db.execute(
        """
        update dedup.jobs
        set status = 'done', finished_at = now(), last_error = null,
            lease_owner = null, lease_expires_at = null, updated_at = now()
        where job_id = %s
        """,
        (job.job_id,),
    )


async def retry_or_dead_letter(db: Database, job: Job, error: str) -> str:
    if job.attempts >= job.max_attempts:
        status = "dead_letter"
        next_attempt = datetime.now(UTC)
    else:
        status = "pending"
        next_attempt = datetime.now(UTC) + _backoff(job.attempts)
    await db.execute(
        """
        update dedup.jobs
        set status = %s, last_error = %s, next_attempt_at = %s,
            lease_owner = null, lease_expires_at = null,
            finished_at = case when %s = 'dead_letter' then now() else null end,
            updated_at = now()
        where job_id = %s
        """,
        (status, error[:2000], next_attempt, status, job.job_id),
    )
    return status


async def release_owner(db: Database, owner: str) -> int:
    return await db.execute(
        """
        update dedup.jobs
        set status = 'pending', lease_owner = null, lease_expires_at = null,
            next_attempt_at = now(), updated_at = now()
        where status = 'running' and lease_owner = %s
        """,
        (owner,),
    )


async def stats(db: Database) -> dict[str, Any]:
    jobs = await db.fetch_all(
        "select status, count(*)::int as count from dedup.jobs group by status"
    )
    clusters = await db.fetch_one("select count(*)::int as count from dedup.story_clusters")
    members = await db.fetch_one("select count(*)::int as count from dedup.story_members")
    return {
        "jobs": {row["status"]: row["count"] for row in jobs},
        "clusters": clusters["count"] if clusters else 0,
        "members": members["count"] if members else 0,
    }


def _backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** max(attempt - 1, 0)), 3600))
