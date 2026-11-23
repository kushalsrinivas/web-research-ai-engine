"""Single-document ETL: raw bytes in, canonical document and quality out."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC
from decimal import Decimal
from typing import Any

from . import canonical, normalize, quality
from .config import NORMALIZER_VERSION, EtlConfig
from .db import Database
from .extract import Extraction, ExtractionError, detect_kind, extract
from .metrics import ETL_DURATION, ETL_INFLIGHT, ETL_QUALITY, ETL_RESULTS
from .queue import Job, activate_output, fail, load_document, load_output, retry_or_dead_letter
from .storage import ObjectStore

PARSER_VERSION = "1.0.0"


class TransientError(RuntimeError):
    """A retryable infrastructure failure."""


class Processor:
    def __init__(
        self, db: Database, store: ObjectStore, config: EtlConfig, logger: logging.Logger
    ) -> None:
        self.db = db
        self.store = store
        self.config = config
        self.logger = logger
        self.version = config.processing_version

    async def process(self, job: Job) -> None:
        ETL_INFLIGHT.inc()
        started = time.monotonic()
        trace_id = uuid.uuid4().hex
        try:
            document = await load_document(self.db, job.document_id, job.revision)
            if document is None:
                await fail(self.db, job, "document revision not found")
                ETL_RESULTS.labels(status="failed").inc()
                return
            raw_uri = document.get("raw_artifact_uri")
            if not raw_uri:
                await self._quarantine(
                    job, document, "raw artifact missing", "raw_uri", started, trace_id
                )
                return

            try:
                payload = await self.store.read_uri(raw_uri)
            except Exception as exc:
                raise TransientError(f"read raw artifact: {exc}") from exc

            content_type = document.get("content_type") or ""
            kind = detect_kind(content_type, payload)
            try:
                extraction = await asyncio.to_thread(
                    extract, payload, content_type, document["fetched_url"]
                )
            except ExtractionError as exc:
                extraction = Extraction(
                    kind=kind,
                    text="",
                    extractor="none",
                    warnings=[f"extraction_failed: {exc}"],
                )

            await self._finish_extraction(job, document, extraction, started, trace_id, raw_uri)
        except TransientError as exc:
            await self._handle_transient(job, str(exc), trace_id)
        except canonical.CanonicalError as exc:
            assert document is not None
            await self._quarantine(
                job, document, f"etl.schema: {exc}", "etl.schema", started, trace_id
            )
        except Exception as exc:
            self.logger.exception("etl job failed", extra={"job_id": job.job_id})
            await self._handle_transient(job, f"unexpected: {exc}", trace_id)
        finally:
            ETL_INFLIGHT.dec()
            ETL_DURATION.labels(kind="all").observe(time.monotonic() - started)

    async def _finish_extraction(
        self,
        job: Job,
        document: dict[str, Any],
        extraction: Extraction,
        started: float,
        trace_id: str,
        raw_uri: str,
    ) -> None:
        text = normalize.clean_text(extraction.text)
        text, truncated = normalize.truncate_utf8(text, self.config.max_text_bytes)
        words = normalize.word_count(text)
        language, language_confidence = normalize.detect_language(text)
        quality_result = quality.score(extraction, text, language, language_confidence, words)
        warnings = list(extraction.warnings)
        if truncated:
            warnings.append("text_truncated")

        published_at = normalize.parse_timestamp(extraction.published_raw)
        updated_at = normalize.parse_timestamp(extraction.updated_raw)
        authors = normalize.normalize_authors(extraction.authors)
        extraction.authors = authors
        extraction.title = normalize.clean_text(extraction.title)
        digest = normalize.content_hash(text)
        hash_bytes = bytes.fromhex(digest)
        simhash = normalize.simhash64(text)
        retrieved_at = document["created_at"]
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=UTC)

        quarantine_reason = self._quarantine_reason(text, words, quality_result["score"])
        next_state = "quarantined" if quarantine_reason else "deduplicated"
        duplicate_of = None
        if not quarantine_reason:
            duplicate_of = await _find_duplicate(
                self.db, hash_bytes, document["document_id"], document["tenant_id"]
            )
            if duplicate_of:
                next_state = "duplicate"

        parsed_key = _artifact_key("parsed", document, job, self.version)
        canonical_key = _artifact_key("canonical", document, job, self.version)
        parsed_uri = await self.store.put_json(
            parsed_key, canonical.parsed_representation(extraction)
        )
        canonical_doc = canonical.build(
            document=document,
            revision=document,
            source_id=document.get("source_id"),
            extraction=extraction,
            text=text,
            language=language,
            language_confidence=language_confidence,
            quality=quality_result,
            content_hash=digest,
            raw_uri=raw_uri,
            parsed_uri=parsed_uri,
            processing_version=self.version,
            parser_version=f"parser-{PARSER_VERSION}",
            normalizer_version=NORMALIZER_VERSION,
            published_at=published_at,
            updated_at=updated_at,
            retrieved_at=retrieved_at,
            warnings=warnings,
        )
        canonical_uri = await self.store.put_json(canonical_key, canonical_doc)

        async with self.db.transaction() as conn:
            await _save_output(
                conn,
                document,
                job,
                self.version,
                hash_bytes,
                simhash,
                extraction.title,
                text,
                language,
                words,
                quality_result,
                canonical_doc["metadata"],
                warnings,
                parsed_uri,
                canonical_uri,
                duplicate_of,
            )
            if quarantine_reason:
                await conn.execute(
                    """
                    update core.documents
                    set state = 'quarantined', failed_stage = %s,
                        last_error = jsonb_build_object('error', %s::text, 'quality', %s::numeric),
                        updated_at = now()
                    where document_id = %s
                    """,
                    (
                        quarantine_reason,
                        f"quality gate: {quarantine_reason}",
                        quality_result["score"],
                        document["document_id"],
                    ),
                )
                await _event(
                    conn,
                    document,
                    job,
                    "parser.extract",
                    document["state"],
                    "quarantined",
                    trace_id,
                    parsed_uri,
                    parsed_uri,
                    started,
                    "permanent",
                    error=f"quality gate: {quarantine_reason}",
                )
            else:
                await conn.execute(
                    """
                    update core.document_revisions
                    set title = %s, language = %s, word_count = %s, content_hash_norm = %s,
                        parsed_artifact_uri = %s, parser_version = %s, normalizer_version = %s,
                        published_at = %s, author = %s
                    where document_id = %s and revision = %s
                    """,
                    (
                        extraction.title,
                        language,
                        words,
                        hash_bytes,
                        parsed_uri,
                        f"parser-{PARSER_VERSION}",
                        NORMALIZER_VERSION,
                        published_at,
                        ", ".join(authors) if authors else None,
                        document["document_id"],
                        job.revision,
                    ),
                )
                await conn.execute(
                    """
                    insert into core.document_text
                        (document_id, revision, title, body, word_count, language)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (document_id, revision) do update set
                        title = excluded.title, body = excluded.body,
                        word_count = excluded.word_count, language = excluded.language
                    """,
                    (
                        document["document_id"],
                        job.revision,
                        extraction.title,
                        text,
                        words,
                        language,
                    ),
                )
                await conn.execute(
                    """
                    update core.documents
                    set state = %s, duplicate_of = %s, failed_stage = null,
                        last_error = case when %s
                            then jsonb_build_object('code', 'TEXT_TRUNCATED') else null end,
                        updated_at = now()
                    where document_id = %s
                    """,
                    (next_state, duplicate_of, truncated, document["document_id"]),
                )
                await _event(
                    conn,
                    document,
                    job,
                    "parser.extract",
                    document["state"],
                    "parsed",
                    trace_id,
                    parsed_uri,
                    parsed_uri,
                    started,
                    None,
                )
                await _event(
                    conn,
                    document,
                    job,
                    "parser.normalize",
                    "parsed",
                    next_state,
                    trace_id,
                    canonical_uri,
                    parsed_uri,
                    started,
                    None,
                )
                await conn.execute(
                    """
                    update processing.jobs
                    set status = 'done', finished_at = now(), last_error = null,
                        lease_owner = null, lease_expires_at = null, updated_at = now()
                    where job_id = %s
                    """,
                    (job.job_id,),
                )

        if quarantine_reason:
            await fail(self.db, job, f"quarantined: {quarantine_reason}")
            ETL_RESULTS.labels(status="quarantined").inc()
            self.logger.warning(
                "document quarantined",
                extra={"job_id": job.job_id, "reason": quarantine_reason},
            )
            return

        ETL_RESULTS.labels(status=next_state).inc()
        ETL_QUALITY.observe(quality_result["score"])
        self.logger.info(
            "document processed",
            extra={
                "job_id": job.job_id,
                "document_id": document["document_id"],
                "state": next_state,
                "words": words,
                "quality": quality_result["score"],
            },
        )

    async def _quarantine(
        self,
        job: Job,
        document: dict[str, Any],
        reason: str,
        stage: str,
        started: float,
        trace_id: str,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                """
                update core.documents
                set state = 'quarantined', failed_stage = %s,
                    last_error = jsonb_build_object('error', %s::text), updated_at = now()
                where document_id = %s
                """,
                (stage, reason, document["document_id"]),
            )
            await _event(
                conn,
                document,
                job,
                stage,
                document["state"],
                "quarantined",
                trace_id,
                "",
                document.get("raw_artifact_uri"),
                started,
                "permanent",
                error=reason,
            )
        await fail(self.db, job, f"quarantined: {reason}")
        ETL_RESULTS.labels(status="quarantined").inc()

    async def _handle_transient(self, job: Job, error: str, trace_id: str) -> None:
        status = await retry_or_dead_letter(self.db, job, error)
        ETL_RESULTS.labels(status=status).inc()
        self.logger.warning(
            "etl attempt failed",
            extra={"job_id": job.job_id, "status": status, "error": error},
        )
        if status == "dead_letter":
            async with self.db.transaction() as conn:
                await conn.execute(
                    """
                    update core.documents
                    set state = 'failed', failed_stage = 'etl', updated_at = now(),
                        last_error = jsonb_build_object('error', %s::text)
                    where document_id = %s
                      and state not in ('deduplicated', 'duplicate')
                    """,
                    (error[:1000], job.document_id),
                )
                await _event(
                    conn,
                    {
                        "document_id": job.document_id,
                        "revision": job.revision,
                        "raw_artifact_uri": None,
                        "state": None,
                    },
                    job,
                    "parser.extract",
                    None,
                    "failed",
                    trace_id,
                    "",
                    None,
                    time.monotonic(),
                    "transient",
                    error=error,
                )

    def _quarantine_reason(self, text: str, words: int, score: float) -> str | None:
        if not text.strip():
            return "etl.empty_content"
        if words < self.config.min_words:
            return "etl.short_content"
        if score < self.config.min_quality:
            return "etl.low_quality"
        return None


async def _save_output(
    conn: Any,
    document: dict[str, Any],
    job: Job,
    version: str,
    hash_bytes: bytes,
    simhash: int,
    title: str,
    body: str,
    language: str,
    words: int,
    quality_result: dict[str, Any],
    metadata: dict[str, Any],
    warnings: list[str],
    parsed_uri: str,
    canonical_uri: str,
    duplicate_of: str | None,
) -> None:
    await conn.execute(
        """
        insert into processing.document_outputs
            (document_id, revision, processing_version, content_hash_norm, simhash,
             title, body, language, word_count, quality_score, quality, metadata,
             warnings, parsed_uri, canonical_uri, is_duplicate, duplicate_of)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (document_id, revision, processing_version) do update set
            content_hash_norm = excluded.content_hash_norm,
            simhash = excluded.simhash,
            title = excluded.title,
            body = excluded.body,
            language = excluded.language,
            word_count = excluded.word_count,
            quality_score = excluded.quality_score,
            quality = excluded.quality,
            metadata = excluded.metadata,
            warnings = excluded.warnings,
            parsed_uri = excluded.parsed_uri,
            canonical_uri = excluded.canonical_uri,
            is_duplicate = excluded.is_duplicate,
            duplicate_of = excluded.duplicate_of
        """,
        (
            document["document_id"],
            job.revision,
            version,
            hash_bytes,
            normalize.simhash_signed(simhash),
            title,
            body,
            language,
            words,
            quality_result["score"],
            json.dumps(quality_result),
            json.dumps(metadata),
            json.dumps(warnings),
            parsed_uri,
            canonical_uri,
            bool(duplicate_of),
            duplicate_of,
        ),
    )


async def _find_duplicate(
    db: Database, hash_bytes: bytes, document_id: str, tenant_id: str
) -> str | None:
    from . import queue

    return await queue.find_exact_duplicate(db, hash_bytes, document_id, tenant_id)


async def _event(
    conn: Any,
    document: dict[str, Any],
    job: Job,
    stage: str,
    from_state: str | None,
    to_state: str,
    trace_id: str,
    artifact_uri: str,
    raw_uri: str | None,
    started: float,
    error_class: str | None,
    error: str | None = None,
) -> None:
    await conn.execute(
        """
        insert into core.document_events
            (document_id, revision, stage, from_state, to_state, attempt,
             processor, processor_version, trace_id, artifact_uri, raw_artifact_uri,
             duration_ms, error_class, error)
        values (%s, %s, %s, %s, %s, %s, 'parser', %s, %s, nullif(%s, ''),
                %s, %s, %s, case when %s::text is null then null
                     else jsonb_build_object('error', %s::text) end)
        """,
        (
            document["document_id"],
            job.revision,
            stage,
            from_state,
            to_state,
            job.attempts,
            "parser-1.0.0",
            trace_id,
            artifact_uri,
            raw_uri,
            int((time.monotonic() - started) * 1000),
            error_class,
            error,
            error,
        ),
    )


def _artifact_key(prefix: str, document: dict[str, Any], job: Job, version: str) -> str:
    tenant = _safe(document["tenant_id"])
    return (
        f"{prefix}/{tenant}/{document['document_id']}/{job.revision}/{_safe(version)}/{prefix}.json"
    )


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in str(value)) or "unknown"


async def activate_version(
    db: Database,
    document_id: str,
    revision: int,
    version: str,
    min_quality: float = 0.35,
) -> dict[str, Any] | None:
    output = await load_output(db, document_id, revision, version)
    if output is None:
        return None
    if output["word_count"] == 0 or output["quality_score"] < Decimal(str(min_quality)):
        return None
    document = await load_document(db, document_id, revision)
    if document is None:
        return None
    trace_id = uuid.uuid4().hex
    async with db.transaction() as conn:
        await activate_output(
            conn,
            document_id=document_id,
            revision=revision,
            output=output,
            from_state=document["state"],
            trace_id=trace_id,
        )
    return {
        "document_id": document_id,
        "revision": revision,
        "activated_version": version,
        "state": "duplicate" if output["is_duplicate"] else "deduplicated",
    }
