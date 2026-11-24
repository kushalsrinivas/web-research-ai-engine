"""Extraction orchestration: NER → resolution → events → claims → validation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata as metadata
import logging
import time
from datetime import UTC, datetime
from typing import Any

from wre_pipeline.db import Database

from .. import repository as graph_repository
from .. import resolver as entity_resolver
from ..metrics import EXTRACTION_DURATION, EXTRACTION_INFLIGHT, EXTRACTION_JOBS
from ..resolver import EntityInput
from ..service import EntityService
from . import claims as claim_extractor
from . import entities as mention_extractor
from . import events as event_extractor
from . import queue, repository, schemas
from .entities import Mention
from .text import sentences


class ExtractionWorker:
    def __init__(
        self,
        db: Database,
        entities: EntityService,
        version: str,
        max_chars: int,
        logger: logging.Logger,
    ) -> None:
        self.db = db
        self.entities = entities
        self.version = version
        self.max_chars = max_chars
        self.logger = logger

    async def process(self, job: queue.Job) -> None:
        started = time.monotonic()
        EXTRACTION_INFLIGHT.inc()
        try:
            await self._process(job, started)
        finally:
            EXTRACTION_INFLIGHT.dec()
            EXTRACTION_DURATION.observe(time.monotonic() - started)

    async def _process(self, job: queue.Job, started: float) -> None:
        document = await repository.load_document(self.db, job.document_id, job.revision)
        if document is None or not document.get("body"):
            if document is not None:
                run_id = await repository.create_run(
                    self.db, job.document_id, job.revision, document["tenant_id"], self.version
                )
                await repository.finish_run(
                    self.db,
                    run_id,
                    status="failed",
                    entities=0,
                    events=0,
                    claims=0,
                    quarantined=0,
                    duration_ms=_millis(started),
                    error="no normalized text",
                )
            await queue.fail(self.db, job, "no normalized text")
            EXTRACTION_JOBS.labels(status="failed").inc()
            return

        run_id = await repository.create_run(
            self.db, job.document_id, job.revision, document["tenant_id"], self.version
        )
        try:
            body = str(document["body"])[: self.max_chars]
            language = document.get("text_language") or document.get("revision_language") or "und"
            draft_mentions = await asyncio.to_thread(
                mention_extractor.extract_mentions, body, language
            )
            resolved, mention_tuples = await self._resolve(
                document["tenant_id"], draft_mentions, body
            )
            events = event_extractor.extract_events(
                sentences(body), resolved, document.get("published_at")
            )
            claims = claim_extractor.extract_claims(sentences(body), resolved, body)
            valid_mentions, valid_events, valid_claims, quarantined = await self._validate(
                run_id=run_id,
                document=document,
                body=body,
                mention_tuples=mention_tuples,
                events=events,
                claims=claims,
            )
            await repository.persist(
                self.db,
                run_id=run_id,
                document=document,
                version=self.version,
                mentions=valid_mentions,
                events=valid_events,
                claims=valid_claims,
            )
            status = "partial" if quarantined else "done"
            await repository.finish_run(
                self.db,
                run_id,
                status=status,
                entities=len(valid_mentions),
                events=len(valid_events),
                claims=len(valid_claims),
                quarantined=quarantined,
                duration_ms=_millis(started),
            )
            await queue.finish(self.db, job)
            EXTRACTION_JOBS.labels(status=status).inc()
            self.logger.info(
                "document extracted",
                extra={
                    "job_id": job.job_id,
                    "document_id": document["document_id"],
                    "mentions": len(valid_mentions),
                    "events": len(valid_events),
                    "claims": len(valid_claims),
                    "quarantined": quarantined,
                },
            )
        except Exception as exc:
            await repository.finish_run(
                self.db,
                run_id,
                status="failed",
                entities=0,
                events=0,
                claims=0,
                quarantined=0,
                duration_ms=_millis(started),
                error=str(exc)[:2000],
            )
            status = await queue.retry_or_dead_letter(self.db, job, str(exc))
            EXTRACTION_JOBS.labels(status=status).inc()
            self.logger.warning(
                "extraction failed",
                extra={"job_id": job.job_id, "status": status, "error": str(exc)},
            )

    async def _resolve(
        self, tenant_id: str, mentions: list[Mention], body: str
    ) -> tuple[list[Mention], list[tuple[Mention, str, float]]]:
        cache: dict[tuple[str, str], tuple[str | None, float]] = {}
        tuples: list[tuple[Mention, str, float]] = []
        for mention in mentions:
            key = (
                entity_resolver.normalize_name(mention.surface, mention.entity_type),
                mention.entity_type,
            )
            if key not in cache:
                try:
                    result = await self.entities.resolve(
                        tenant_id,
                        EntityInput(name=mention.surface, entity_type=mention.entity_type),
                        create_missing=True,
                    )
                    entity = result.get("entity")
                    confidence = {
                        "auto_match": 0.95,
                        "review": 0.8,
                        "keep_separate": 0.6,
                    }.get(str(result.get("decision")), 0.5)
                    if entity is None and result.get("decision") == "review":
                        entity = await graph_repository.create_entity(
                            self.db,
                            tenant_id,
                            EntityInput(name=mention.surface, entity_type=mention.entity_type),
                            0.5,
                        )
                    cache[key] = (
                        str(entity["entity_id"]) if entity else None,
                        confidence,
                    )
                except Exception as exc:  # one bad surface should not lose the document
                    self.logger.warning(
                        "entity resolution failed",
                        extra={"surface": mention.surface, "error": str(exc)},
                    )
                    cache[key] = (None, 0.0)
            entity_id, confidence = cache[key]
            if entity_id is None:
                continue
            if not (0 <= mention.char_start < mention.char_end <= len(body)):
                continue
            if body[mention.char_start : mention.char_end] != mention.surface:
                continue
            mention.entity_id = entity_id
            mention.resolution_confidence = confidence
            tuples.append((mention, entity_id, confidence))
        return mentions, tuples

    async def _validate(
        self,
        *,
        run_id: str,
        document: dict[str, Any],
        body: str,
        mention_tuples: list[tuple[Mention, str, float]],
        events: list[event_extractor.EventDraft],
        claims: list[claim_extractor.ClaimDraft],
    ) -> tuple[
        list[tuple[Mention, str, float]],
        list[event_extractor.EventDraft],
        list[claim_extractor.ClaimDraft],
        int,
    ]:
        quarantined = 0
        valid_mentions: list[tuple[Mention, str, float]] = []
        for mention, entity_id, confidence in mention_tuples:
            payload = {
                "entity_id": entity_id,
                "entity_type": mention.entity_type,
                "surface_form": mention.surface,
                "char_start": mention.char_start,
                "char_end": mention.char_end,
                "confidence": confidence,
                "extractor_version": self.version,
            }
            errors = schemas.errors("mention", payload)
            if errors:
                quarantined += 1
                await repository.quarantine(
                    self.db,
                    run_id=run_id,
                    document_id=document["document_id"],
                    revision=document["revision"],
                    stage="ner",
                    object_type="mention",
                    reason="; ".join(errors),
                    payload=payload,
                    version=self.version,
                )
                continue
            valid_mentions.append((mention, entity_id, confidence))

        valid_events: list[event_extractor.EventDraft] = []
        for event in events:
            if (
                not (0 <= event.char_start < event.char_end <= len(body))
                or body[event.char_start : event.char_end] != event.quote
            ):
                quarantined += 1
                await repository.quarantine(
                    self.db,
                    run_id=run_id,
                    document_id=document["document_id"],
                    revision=document["revision"],
                    stage="event",
                    object_type="event",
                    reason="event span does not match source",
                    payload=_dict(event),
                    version=self.version,
                )
                continue
            payload = _event_payload(document, event, self.version)
            errors = schemas.errors("event", payload)
            if errors:
                quarantined += 1
                await repository.quarantine(
                    self.db,
                    run_id=run_id,
                    document_id=document["document_id"],
                    revision=document["revision"],
                    stage="event",
                    object_type="event",
                    reason="; ".join(errors),
                    payload=payload,
                    version=self.version,
                )
                continue
            valid_events.append(event)

        valid_claims: list[claim_extractor.ClaimDraft] = []
        for claim in claims:
            if (
                not (0 <= claim.char_start < claim.char_end <= len(body))
                or body[claim.char_start : claim.char_end] != claim.quote
            ):
                quarantined += 1
                await repository.quarantine(
                    self.db,
                    run_id=run_id,
                    document_id=document["document_id"],
                    revision=document["revision"],
                    stage="claim",
                    object_type="claim",
                    reason="claim span does not match source",
                    payload=_dict(claim),
                    version=self.version,
                )
                continue
            if (claim.object_entity_id is None) == (claim.object_literal is None):
                quarantined += 1
                await repository.quarantine(
                    self.db,
                    run_id=run_id,
                    document_id=document["document_id"],
                    revision=document["revision"],
                    stage="claim",
                    object_type="claim",
                    reason="claim must have exactly one object",
                    payload=_dict(claim),
                    version=self.version,
                )
                continue
            payload = _claim_payload(document, claim, self.version)
            errors = schemas.errors("claim", payload)
            if errors:
                quarantined += 1
                await repository.quarantine(
                    self.db,
                    run_id=run_id,
                    document_id=document["document_id"],
                    revision=document["revision"],
                    stage="claim",
                    object_type="claim",
                    reason="; ".join(errors),
                    payload=payload,
                    version=self.version,
                )
                continue
            valid_claims.append(claim)

        return valid_mentions, valid_events, valid_claims, quarantined


def extraction_config_hash() -> str:
    try:
        model = metadata.version("en-core-web-sm")
    except metadata.PackageNotFoundError:
        model = "missing"
    return hashlib.sha256(
        f"extract-1.0.0|spacy-en_core_web_sm={model}|rules=1".encode()
    ).hexdigest()


def _event_payload(
    document: dict[str, Any], event: event_extractor.EventDraft, version: str
) -> dict[str, Any]:
    payload = _dict(event)
    payload.update(
        {
            "document_id": document["document_id"],
            "revision": document["revision"],
            "event_time": _iso(event.event_time),
            "evidence_document_ids": [document["document_id"]],
            "extractor_version": version,
        }
    )
    return payload


def _claim_payload(
    document: dict[str, Any], claim: claim_extractor.ClaimDraft, version: str
) -> dict[str, Any]:
    payload = _dict(claim)
    payload.update(
        {
            "document_id": document["document_id"],
            "revision": document["revision"],
            "extractor_version": version,
        }
    )
    return payload


def _dict(value: Any) -> dict[str, Any]:
    return {key: getattr(value, key) for key in value.__dataclass_fields__}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _millis(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
