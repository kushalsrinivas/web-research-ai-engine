"""Durable extraction job queue."""

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


async def dispatch(db: Database, version: str, limit: int) -> int:
    return await db.execute(
        """
        insert into graph.extraction_jobs (job_id, document_id, revision)
        select gen_random_uuid()::text, d.document_id, d.current_revision
        from core.documents d
        where d.state in ('deduplicated', 'duplicate')
          and exists (
            select 1 from core.document_text t
            where t.document_id = d.document_id and t.revision = d.current_revision
          )
          and not exists (
            select 1 from graph.extraction_runs r
            where r.document_id = d.document_id and r.revision = d.current_revision
              and r.extraction_version = %s
              and r.status in ('done', 'partial')
          )
          and not exists (
            select 1 from graph.extraction_jobs j
            where j.document_id = d.document_id and j.revision = d.current_revision
              and j.status in ('pending', 'running', 'failed', 'dead_letter')
          )
        order by d.updated_at
        limit %s
        on conflict do nothing
        """,
        (version, limit),
    )


async def recover_leases(db: Database) -> int:
    return await db.execute(
        """
        update graph.extraction_jobs
        set status = 'pending', lease_owner = null, lease_expires_at = null,
            next_attempt_at = now(), updated_at = now()
        where status = 'running' and lease_expires_at < now()
        """
    )


async def claim(db: Database, owner: str, lease_seconds: int) -> Job | None:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            """
            update graph.extraction_jobs j
            set status = 'running', lease_owner = %s,
                lease_expires_at = now() + (%s * interval '1 second'),
                attempts = attempts + 1, started_at = coalesce(started_at, now()),
                updated_at = now()
            where j.job_id = (
                select job_id from graph.extraction_jobs
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
        update graph.extraction_jobs
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
        update graph.extraction_jobs
        set status = %s, last_error = %s, next_attempt_at = %s,
            lease_owner = null, lease_expires_at = null,
            finished_at = case when %s = 'dead_letter' then now() else null end,
            updated_at = now()
        where job_id = %s
        """,
        (status, error[:2000], next_attempt, status, job.job_id),
    )
    return status


async def fail(db: Database, job: Job, error: str) -> None:
    await db.execute(
        """
        update graph.extraction_jobs
        set status = 'failed', last_error = %s, finished_at = now(),
            lease_owner = null, lease_expires_at = null, updated_at = now()
        where job_id = %s
        """,
        (error[:2000], job.job_id),
    )


async def release_owner(db: Database, owner: str) -> int:
    return await db.execute(
        """
        update graph.extraction_jobs
        set status = 'pending', lease_owner = null, lease_expires_at = null,
            next_attempt_at = now(), updated_at = now()
        where status = 'running' and lease_owner = %s
        """,
        (owner,),
    )


async def enqueue_document(db: Database, document_id: str) -> int:
    return await db.execute(
        """
        insert into graph.extraction_jobs (job_id, document_id, revision)
        select gen_random_uuid()::text, d.document_id, d.current_revision
        from core.documents d
        where d.document_id = %s
          and exists (
            select 1 from core.document_text t
            where t.document_id = d.document_id and t.revision = d.current_revision
          )
        on conflict do nothing
        """,
        (document_id,),
    )


async def stats(db: Database) -> dict[str, Any]:
    rows = await db.fetch_all(
        "select status, count(*)::int as count from graph.extraction_jobs group by status"
    )
    return {row["status"]: row["count"] for row in rows}


def _backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** max(attempt - 1, 0)), 3600))
