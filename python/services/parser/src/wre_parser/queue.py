"""Durable ETL queue, version registry, and reprocessing queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from .db import Database

ACTIVE_STATES = (
    "raw_archived",
    "parsed",
    "normalized",
    "deduplicated",
    "duplicate",
    "failed",
    "quarantined",
)


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    document_id: str
    revision: int
    mode: str
    target_version: str
    attempts: int
    max_attempts: int
    priority: int


async def register_version(db: Database, version: str, processor: str, config_hash: str) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            """
            insert into processing.versions (version, processor, description, config_hash, active)
            values (%s, %s, %s, %s, true)
            on conflict (version) do update set
                processor = excluded.processor,
                config_hash = excluded.config_hash,
                active = true
            """,
            (version, processor, f"ETL pipeline {version}", config_hash),
        )
        await conn.execute(
            "update processing.versions set active = (version = %s) where active or version = %s",
            (version, version),
        )


async def dispatch(db: Database, target_version: str, limit: int) -> int:
    return await db.execute(
        """
        insert into processing.jobs
            (job_id, document_id, revision, mode, target_version, priority, reason, requested_by)
        select gen_random_uuid()::text, d.document_id, d.current_revision,
               'extract', %s, 5, 'new raw artifact', 'dispatcher'
        from core.documents d
        join core.document_revisions r
          on r.document_id = d.document_id and r.revision = d.current_revision
        where d.state = 'raw_archived'
          and r.raw_artifact_uri is not null
          and not exists (
            select 1 from processing.document_outputs o
            where o.document_id = d.document_id
              and o.revision = d.current_revision
              and o.processing_version = %s
          )
        order by d.updated_at
        limit %s
        on conflict do nothing
        """,
        (target_version, target_version, limit),
    )


async def recover_leases(db: Database) -> int:
    return await db.execute(
        """
        update processing.jobs
        set status = 'pending', lease_owner = null, lease_expires_at = null,
            next_attempt_at = now(), updated_at = now()
        where status = 'running' and lease_expires_at < now()
        """
    )


async def release_owner(db: Database, owner: str) -> int:
    return await db.execute(
        """
        update processing.jobs
        set status = 'pending', lease_owner = null, lease_expires_at = null,
            next_attempt_at = now(), updated_at = now()
        where status = 'running' and lease_owner = %s
        """,
        (owner,),
    )


async def claim(db: Database, owner: str, lease_seconds: int) -> Job | None:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            """
            update processing.jobs j
            set status = 'running', lease_owner = %s,
                lease_expires_at = now() + (%s * interval '1 second'),
                attempts = attempts + 1, started_at = coalesce(started_at, now()),
                updated_at = now()
            where j.job_id = (
                select job_id from processing.jobs
                where status = 'pending' and next_attempt_at <= now()
                order by priority desc, created_at
                for update skip locked
                limit 1
            )
            returning job_id, document_id, revision, mode, target_version,
                      attempts, max_attempts, priority
            """,
            (owner, lease_seconds),
        )
        row = await cursor.fetchone()
    return Job(**row) if row else None


async def retry_or_dead_letter(db: Database, job: Job, error: str) -> str:
    if job.attempts >= job.max_attempts:
        status = "dead_letter"
        next_attempt = datetime.now(UTC)
    else:
        status = "pending"
        next_attempt = datetime.now(UTC) + _backoff(job.attempts)
    await db.execute(
        """
        update processing.jobs
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
        update processing.jobs
        set status = 'failed', last_error = %s, finished_at = now(),
            lease_owner = null, lease_expires_at = null, updated_at = now()
        where job_id = %s
        """,
        (error[:2000], job.job_id),
    )


async def load_document(db: Database, document_id: str, revision: int) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        select d.document_id, d.tenant_id, d.canonical_url, d.state,
               d.current_revision, d.duplicate_of,
               r.revision, r.fetched_url, r.http_status, r.content_type,
               r.raw_artifact_uri, r.created_at,
               (select f.source_id::text from fetcher.jobs f
                where f.document_id = d.document_id and f.source_id is not null
                order by f.created_at desc limit 1) as source_id
        from core.documents d
        join core.document_revisions r on r.document_id = d.document_id
        where d.document_id = %s and r.revision = %s
        """,
        (document_id, revision),
    )


async def find_exact_duplicate(
    db: Database, content_hash_norm: bytes, document_id: str, tenant_id: str
) -> str | None:
    row = await db.fetch_one(
        """
        select o.document_id
        from processing.document_outputs o
        join core.documents d on d.document_id = o.document_id
        where o.content_hash_norm = %s
          and o.document_id <> %s
          and d.tenant_id = %s
          and not o.is_duplicate
        order by o.created_at
        limit 1
        """,
        (content_hash_norm, document_id, tenant_id),
    )
    return row["document_id"] if row else None


async def enqueue_reprocess(
    db: Database,
    *,
    target_version: str,
    mode: str,
    document_ids: list[str] | None,
    source_id: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
    reason: str,
    requested_by: str,
    limit: int,
) -> int:
    return await db.execute(
        """
        insert into processing.jobs
            (job_id, document_id, revision, mode, target_version, priority, reason, requested_by)
        select gen_random_uuid()::text, d.document_id, d.current_revision,
               %s, %s, 7, %s, %s
        from core.documents d
        join core.document_revisions r
          on r.document_id = d.document_id and r.revision = d.current_revision
        where d.state::text = any(%s)
          and (%s::text[] is null or d.document_id = any(%s::text[]))
          and (%s::uuid is null or exists (
                select 1 from fetcher.jobs f
                where f.document_id = d.document_id and f.source_id = %s::uuid
              ))
          and (%s::timestamptz is null or r.created_at >= %s::timestamptz)
          and (%s::timestamptz is null or r.created_at <= %s::timestamptz)
        order by d.updated_at
        limit %s
        on conflict do nothing
        """,
        (
            mode,
            target_version,
            reason,
            requested_by,
            list(ACTIVE_STATES),
            document_ids,
            document_ids,
            source_id,
            source_id,
            from_time,
            from_time,
            to_time,
            to_time,
            limit,
        ),
    )


async def list_jobs(
    db: Database,
    status: str | None,
    document_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select job_id, document_id, revision, mode, status, attempts, max_attempts,
               target_version, last_error, created_at, updated_at, finished_at
        from processing.jobs
        where (%s::text is null or status = %s)
          and (%s::text is null or document_id = %s)
        order by created_at desc limit %s
        """,
        (status, status, document_id, document_id, limit),
    )


async def stats(db: Database) -> dict[str, Any]:
    jobs = await db.fetch_all(
        "select status, count(*)::int as count from processing.jobs group by status"
    )
    documents = await db.fetch_all(
        """
        select state, count(*)::int as count from core.documents
        where state not in ('discovered', 'scheduled')
        group by state
        """
    )
    outputs = await db.fetch_one("select count(*)::int as count from processing.document_outputs")
    pending = await db.fetch_one(
        "select count(*)::int as count from processing.jobs where status = 'pending'"
    )
    return {
        "jobs": {row["status"]: row["count"] for row in jobs},
        "documents": {row["state"]: row["count"] for row in documents},
        "outputs": outputs["count"] if outputs else 0,
        "queue_depth": pending["count"] if pending else 0,
    }


async def versions(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select version, processor, description, config_hash, active, created_at
        from processing.versions order by created_at desc
        """
    )


async def document_detail(db: Database, document_id: str) -> dict[str, Any] | None:
    document = await db.fetch_one(
        """
        select document_id, tenant_id, canonical_url, state, current_revision,
               available_revision, duplicate_of, failed_stage, last_error, updated_at
        from core.documents where document_id = %s
        """,
        (document_id,),
    )
    if not document:
        return None
    outputs = await db.fetch_all(
        """
        select processing_version, content_hash_norm, title, language, word_count,
               quality_score, quality, warnings, parsed_uri, canonical_uri,
               is_duplicate, duplicate_of, created_at
        from processing.document_outputs
        where document_id = %s order by created_at
        """,
        (document_id,),
    )
    for output in outputs:
        output["content_hash_norm"] = output["content_hash_norm"].hex()
    document["outputs"] = outputs
    return document


async def quality_comparison(db: Database, document_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        select processing_version, revision, quality_score, quality, word_count, language,
               is_duplicate, created_at
        from processing.document_outputs
        where document_id = %s order by created_at
        """,
        (document_id,),
    )
    return rows


async def load_output(
    db: Database, document_id: str, revision: int, version: str
) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        select * from processing.document_outputs
        where document_id = %s and revision = %s and processing_version = %s
        """,
        (document_id, revision, version),
    )


async def activate_output(
    conn: AsyncConnection[dict[str, Any]],
    *,
    document_id: str,
    revision: int,
    output: dict[str, Any],
    from_state: str,
    trace_id: str,
) -> None:
    await conn.execute(
        """
        update core.document_revisions
        set title = %s, language = %s, word_count = %s, content_hash_norm = %s,
            parser_version = %s, normalizer_version = %s
        where document_id = %s and revision = %s
        """,
        (
            output["title"],
            output["language"],
            output["word_count"],
            output["content_hash_norm"],
            output["processing_version"],
            output["processing_version"],
            document_id,
            revision,
        ),
    )
    await conn.execute(
        """
        insert into core.document_text (document_id, revision, title, body, word_count, language)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (document_id, revision) do update set
            title = excluded.title, body = excluded.body,
            word_count = excluded.word_count, language = excluded.language
        """,
        (
            document_id,
            revision,
            output["title"],
            output["body"],
            output["word_count"],
            output["language"],
        ),
    )
    state = "duplicate" if output["is_duplicate"] else "deduplicated"
    await conn.execute(
        """
        update core.documents
        set state = %s, duplicate_of = %s, failed_stage = null, last_error = null,
            updated_at = now()
        where document_id = %s
        """,
        (state, output["duplicate_of"], document_id),
    )
    await conn.execute(
        """
        insert into core.document_events
            (document_id, revision, stage, from_state, to_state, attempt,
             processor, processor_version, trace_id, artifact_uri, raw_artifact_uri, duration_ms)
        values (%s, %s, 'parser.rollback', %s, %s, 1, 'parser', %s, %s, %s, %s, 0)
        """,
        (
            document_id,
            revision,
            from_state,
            state,
            output["processing_version"],
            trace_id,
            output["canonical_uri"],
            output["canonical_uri"],
        ),
    )


def _backoff(attempt: int) -> timedelta:
    seconds = min(30 * (2 ** max(attempt - 1, 0)), 3600)
    return timedelta(seconds=seconds)
