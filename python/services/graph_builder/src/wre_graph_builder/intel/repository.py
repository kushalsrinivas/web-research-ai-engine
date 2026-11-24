"""Persistence and queries for extraction runs, mentions, events, and claims."""

from __future__ import annotations

import json
from typing import Any

from ulid import ULID
from wre_pipeline.db import Database

from .claims import ClaimDraft
from .entities import Mention
from .events import EventDraft

EXTRACTION_VERSION = "extract-1.0.0"


async def register_version(db: Database, version: str, config_hash: str) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            """
            insert into graph.extraction_versions
                (version, processor, description, config_hash, active)
            values (%s, 'extractor', %s, %s, true)
            on conflict (version) do update set
                config_hash = excluded.config_hash, active = true
            """,
            (version, f"Entity/event/claim extraction {version}", config_hash),
        )
        await conn.execute(
            """
            update graph.extraction_versions set active = (version = %s)
            where active or version = %s
            """,
            (version, version),
        )


async def load_document(db: Database, document_id: str, revision: int) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        select d.document_id, d.tenant_id, d.current_revision, d.state, d.duplicate_of,
               r.revision, r.published_at, r.language as revision_language,
               t.title, t.body, t.language as text_language
        from core.documents d
        join core.document_revisions r
          on r.document_id = d.document_id and r.revision = %s
        join core.document_text t
          on t.document_id = d.document_id and t.revision = r.revision
        where d.document_id = %s
        """,
        (revision, document_id),
    )


async def create_run(
    db: Database, document_id: str, revision: int, tenant_id: str, version: str
) -> str:
    run_id = str(ULID())
    await db.execute(
        """
        insert into graph.extraction_runs
            (run_id, document_id, revision, tenant_id, extraction_version, status)
        values (%s, %s, %s, %s, %s, 'running')
        """,
        (run_id, document_id, revision, tenant_id, version),
    )
    return run_id


async def finish_run(
    db: Database,
    run_id: str,
    *,
    status: str,
    entities: int,
    events: int,
    claims: int,
    quarantined: int,
    duration_ms: int,
    error: str | None = None,
) -> None:
    await db.execute(
        """
        update graph.extraction_runs
        set status = %s, entity_count = %s, event_count = %s, claim_count = %s,
            quarantined_count = %s, duration_ms = %s, error = %s, finished_at = now()
        where run_id = %s
        """,
        (status, entities, events, claims, quarantined, duration_ms, error, run_id),
    )


async def quarantine(
    db: Database,
    *,
    run_id: str,
    document_id: str,
    revision: int,
    stage: str,
    object_type: str,
    reason: str,
    payload: dict[str, Any],
    version: str,
) -> None:
    await db.execute(
        """
        insert into graph.extraction_quarantine
            (quarantine_id, run_id, document_id, revision, stage, object_type,
             reason, payload, extraction_version)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(ULID()),
            run_id,
            document_id,
            revision,
            stage,
            object_type,
            reason[:2000],
            json.dumps(payload, default=str, ensure_ascii=False),
            version,
        ),
    )


async def persist(
    db: Database,
    *,
    run_id: str,
    document: dict[str, Any],
    version: str,
    mentions: list[tuple[Mention, str, float]],
    events: list[EventDraft],
    claims: list[ClaimDraft],
) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "delete from graph.entity_mentions where document_id = %s and revision = %s",
            (document["document_id"], document["revision"]),
        )
        for mention, entity_id, confidence in mentions:
            await conn.execute(
                """
                insert into graph.entity_mentions
                    (entity_id, document_id, revision, surface_form, char_start, char_end,
                     confidence, extractor_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict do nothing
                """,
                (
                    entity_id,
                    document["document_id"],
                    document["revision"],
                    mention.surface,
                    mention.char_start,
                    mention.char_end,
                    confidence,
                    version,
                ),
            )
        for event in events:
            await conn.execute(
                """
                insert into graph.events
                    (event_id, run_id, tenant_id, document_id, revision, event_type,
                     subject_entity_id, object_entity_id, location_text, event_time,
                     event_time_precision, amount, currency, amount_original,
                     commitment_status, modality, description, quote, char_start, char_end,
                     paragraph_index, sentence_index, confidence, extraction_confidence,
                     event_classification_confidence, entity_resolution_confidence,
                     verification_confidence, evidence_document_ids, extractor_version, model_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(ULID()),
                    run_id,
                    document["tenant_id"],
                    document["document_id"],
                    document["revision"],
                    event.event_type,
                    event.subject_entity_id,
                    event.object_entity_id,
                    event.location_text,
                    event.event_time,
                    event.event_time_precision,
                    event.amount,
                    event.currency,
                    event.amount_original,
                    event.commitment_status,
                    event.modality,
                    event.description,
                    event.quote,
                    event.char_start,
                    event.char_end,
                    event.paragraph_index,
                    event.sentence_index,
                    event.confidence,
                    event.extraction_confidence,
                    event.event_classification_confidence,
                    event.entity_resolution_confidence,
                    None,
                    [document["document_id"]],
                    version,
                    "rules-1.0.0",
                ),
            )
        content_hash = await _content_hash(conn, document)
        claim_ids: list[str] = []
        for claim in claims:
            claim_id = str(ULID())
            claim_ids.append(claim_id)
            await conn.execute(
                """
                insert into graph.claims
                    (claim_id, run_id, tenant_id, document_id, revision, content_hash,
                     subject_entity_id, subject_text, predicate, object_entity_id,
                     object_literal, object_datatype, polarity, claim_type, claim_text,
                     quote, char_start, char_end, span_verified, paragraph_index,
                     sentence_index, published_at, modality, commitment_status,
                     temporal_qualifier, amount, currency, amount_original, location_text,
                     claim_key, normalized_subject, normalized_predicate, normalized_object,
                     extraction_confidence, entity_resolution_confidence,
                     claim_support_confidence, verification_confidence,
                     extractor_version, model_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    claim_id,
                    run_id,
                    document["tenant_id"],
                    document["document_id"],
                    document["revision"],
                    content_hash,
                    claim.subject_entity_id,
                    claim.subject_text,
                    claim.predicate,
                    claim.object_entity_id,
                    claim.object_literal,
                    claim.object_datatype,
                    claim.polarity,
                    claim.claim_type,
                    claim.claim_text,
                    claim.quote,
                    claim.char_start,
                    claim.char_end,
                    claim.span_verified,
                    claim.paragraph_index,
                    claim.sentence_index,
                    document.get("published_at"),
                    claim.modality,
                    claim.commitment_status,
                    claim.temporal_qualifier,
                    claim.amount,
                    claim.currency,
                    claim.amount_original,
                    claim.location_text,
                    claim.claim_key,
                    claim.normalized_subject,
                    claim.normalized_predicate,
                    claim.normalized_object,
                    claim.extraction_confidence,
                    claim.entity_resolution_confidence,
                    claim.claim_support_confidence,
                    claim.verification_confidence,
                    version,
                    "rules-1.0.0",
                ),
            )
        await conn.execute(
            "delete from graph.edges where document_id = %s and revision = %s",
            (document["document_id"], document["revision"]),
        )
        for _mention, entity_id, confidence in mentions:
            await conn.execute(
                """
                insert into graph.edges
                    (tenant_id, edge_type, src_kind, src_id, dst_kind, dst_id,
                     document_id, revision, weight, extractor_version)
                values (%s, 'mentions', 'document', %s, 'entity', %s, %s, %s, %s, %s)
                on conflict do nothing
                """,
                (
                    document["tenant_id"],
                    document["document_id"],
                    entity_id,
                    document["document_id"],
                    document["revision"],
                    confidence,
                    version,
                ),
            )
        for claim, claim_id in zip(claims, claim_ids, strict=False):
            await conn.execute(
                """
                insert into graph.edges
                    (tenant_id, edge_type, src_kind, src_id, dst_kind, dst_id,
                     document_id, revision, weight, extractor_version)
                values (%s, 'asserts', 'document', %s, 'claim', %s, %s, %s, %s, %s)
                on conflict do nothing
                """,
                (
                    document["tenant_id"],
                    document["document_id"],
                    claim_id,
                    document["document_id"],
                    document["revision"],
                    claim.extraction_confidence,
                    version,
                ),
            )
            for linked_entity_id in (claim.subject_entity_id, claim.object_entity_id):
                if linked_entity_id:
                    await conn.execute(
                        """
                        insert into graph.edges
                            (tenant_id, edge_type, src_kind, src_id, dst_kind, dst_id,
                             document_id, revision, weight, extractor_version)
                        values (%s, 'about', 'claim', %s, 'entity', %s, %s, %s, %s, %s)
                        on conflict do nothing
                        """,
                        (
                            document["tenant_id"],
                            claim_id,
                            linked_entity_id,
                            document["document_id"],
                            document["revision"],
                            claim.entity_resolution_confidence or 0.6,
                            version,
                        ),
                    )


async def _content_hash(conn: Any, document: dict[str, Any]) -> bytes:
    cursor = await conn.execute(
        """
        select content_hash_norm from core.document_revisions
        where document_id = %s and revision = %s
        """,
        (document["document_id"], document["revision"]),
    )
    row = await cursor.fetchone()
    value = row["content_hash_norm"] if row else None
    return value if value is not None else b""


async def latest_run(db: Database, document_id: str, revision: int) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        select * from graph.extraction_runs
        where document_id = %s and revision = %s
        order by (status in ('done', 'partial')) desc, created_at desc limit 1
        """,
        (document_id, revision),
    )


async def document_intel(db: Database, document_id: str) -> dict[str, Any] | None:
    document = await db.fetch_one(
        """
        select d.document_id, d.tenant_id, d.state, d.current_revision, d.duplicate_of,
               t.title, left(t.body, 200000) as body, t.language, r.published_at
        from core.documents d
        left join core.document_text t
          on t.document_id = d.document_id and t.revision = d.current_revision
        left join core.document_revisions r
          on r.document_id = d.document_id and r.revision = d.current_revision
        where d.document_id = %s
        """,
        (document_id,),
    )
    if document is None:
        return None
    run = await latest_run(db, document_id, document["current_revision"])
    mentions = await db.fetch_all(
        """
        select m.entity_id, m.surface_form, m.char_start, m.char_end, m.confidence,
               m.extractor_version, e.canonical_name, e.entity_type, e.aliases
        from graph.entity_mentions m
        join graph.entities e on e.entity_id = m.entity_id
        where m.document_id = %s and m.revision = %s
        order by m.char_start
        """,
        (document_id, document["current_revision"]),
    )
    claims: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    if run:
        claims = await db.fetch_all(
            "select * from graph.claims where run_id = %s order by char_start",
            (run["run_id"],),
        )
        events = await db.fetch_all(
            "select * from graph.events where run_id = %s order by char_start",
            (run["run_id"],),
        )
        quarantine = await db.fetch_all(
            "select * from graph.extraction_quarantine where run_id = %s order by created_at",
            (run["run_id"],),
        )
    return {
        "document": document,
        "run": run,
        "mentions": mentions,
        "events": events,
        "claims": claims,
        "quarantine": quarantine,
    }


async def list_claims(
    db: Database,
    *,
    subject_entity_id: str | None,
    claim_key: str | None,
    claim_type: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select c.*, e.canonical_name as subject_name
        from graph.live_claims c
        left join graph.entities e on e.entity_id = c.subject_entity_id
        where c.status = 'active'
          and (%s::text is null or c.subject_entity_id = %s)
          and (%s::text is null or c.claim_key = %s)
          and (%s::text is null or c.claim_type = %s)
        order by c.created_at desc limit %s
        """,
        (
            subject_entity_id,
            subject_entity_id,
            claim_key,
            claim_key,
            claim_type,
            claim_type,
            limit,
        ),
    )


async def claim_groups(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select claim_key, normalized_subject, normalized_predicate, normalized_object,
               count(distinct document_id)::int as document_count,
               count(*)::int as claim_count,
               array_agg(distinct document_id) as document_ids,
               array_agg(distinct modality) as modalities
        from graph.live_claims
        where status = 'active' and span_verified
        group by claim_key, normalized_subject, normalized_predicate, normalized_object
        order by document_count desc, claim_count desc
        limit %s
        """,
        (limit,),
    )


async def list_events(
    db: Database,
    *,
    event_type: str | None,
    subject_entity_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select e.*, s.canonical_name as subject_name
        from graph.live_events e
        left join graph.entities s on s.entity_id = e.subject_entity_id
        where (%s::text is null or e.event_type = %s)
          and (%s::text is null or e.subject_entity_id = %s)
        order by e.discovered_at desc limit %s
        """,
        (event_type, event_type, subject_entity_id, subject_entity_id, limit),
    )


async def stats(db: Database) -> dict[str, Any]:
    claims = await db.fetch_one("select count(*)::int as count from graph.live_claims")
    events = await db.fetch_one("select count(*)::int as count from graph.live_events")
    mentions = await db.fetch_one("select count(*)::int as count from graph.entity_mentions")
    runs = await db.fetch_all(
        "select status, count(*)::int as count from graph.extraction_runs group by status"
    )
    quarantine = await db.fetch_one(
        "select count(*)::int as count from graph.extraction_quarantine"
    )
    return {
        "claims": claims["count"] if claims else 0,
        "events": events["count"] if events else 0,
        "mentions": mentions["count"] if mentions else 0,
        "runs": {row["status"]: row["count"] for row in runs},
        "quarantined": quarantine["count"] if quarantine else 0,
    }


async def quarantine_rows(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select quarantine_id, document_id, revision, stage, object_type, reason,
               payload, extraction_version, created_at
        from graph.extraction_quarantine order by created_at desc limit %s
        """,
        (limit,),
    )


async def versions(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all("select * from graph.extraction_versions order by created_at desc")
