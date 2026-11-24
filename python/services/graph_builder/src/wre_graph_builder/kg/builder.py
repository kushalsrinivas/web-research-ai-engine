"""Build the temporal graph projection from authoritative Postgres tables."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from ulid import ULID
from wre_pipeline.db import Database

from .. import resolver
from ..intel.text import sentences
from .model import Relationship

EXTRACTION_VERSION = "extract-1.0.0"
DERIVED_VERSION = "kg-1.0.0"

_CLAIM_RELATIONS = {
    "located_in": "LOCATED_IN",
    "approved_policy": "APPROVED",
    "completed_acquisition": "OWNS",
    "launched_product": "PRODUCES",
    "signed_contract": "PARTNERED_WITH",
    "announced_investment": "ANNOUNCED",
    "started_construction": "RELATED_TO",
    "started_investigation": "REGULATES",
}


async def active_version(db: Database) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        select * from graph.projection_versions
        where status = 'active' order by created_at desc limit 1
        """
    )


async def needs_rebuild(db: Database) -> bool:
    active = await active_version(db)
    if active is None:
        return True
    changed = await db.fetch_one(
        """
        select greatest(
          (select max(created_at) from graph.claims),
          (select max(created_at) from graph.events),
          (select max(created_at) from graph.entity_mentions),
          (select max(updated_at) from graph.entities),
          (select max(created_at) from dedup.lineage_edges),
          (select max(updated_at) from core.documents)
        ) as changed
        """
    )
    return bool(changed and changed["changed"] and changed["changed"] > active["created_at"])


async def build(
    db: Database, logger: logging.Logger, description: str = "projection"
) -> dict[str, Any]:
    version = f"kg-{ULID()}"
    run_id = str(ULID())
    started = time.monotonic()
    await db.execute(
        """
        insert into graph.projection_versions (version, status, description)
        values (%s, 'building', %s)
        """,
        (version, description),
    )
    await db.execute(
        """
        insert into graph.projection_runs (run_id, projection_version, status)
        values (%s, %s, 'running')
        """,
        (run_id, version),
    )
    try:
        relationships = await _collect(db)
        async with db.transaction() as conn:
            for relationship in relationships:
                await conn.execute(
                    """
                    insert into graph.relationships
                        (relationship_id, projection_version, tenant_id, relationship_type,
                         src_kind, src_id, dst_kind, dst_id, valid_from, valid_to,
                         observed_at, recorded_at, source_document_id, source_claim_id,
                         source_event_id, confidence, extraction_version, evidence)
                    values (%s, %s, 'public', %s, %s, %s, %s, %s, %s, %s, %s, now(),
                            %s, %s, %s, %s, %s, %s)
                    on conflict do nothing
                    """,
                    (
                        str(ULID()),
                        version,
                        relationship.relationship_type,
                        relationship.src_kind,
                        relationship.src_id,
                        relationship.dst_kind,
                        relationship.dst_id,
                        relationship.valid_from,
                        relationship.valid_to,
                        relationship.observed_at,
                        relationship.source_document_id,
                        relationship.source_claim_id,
                        relationship.source_event_id,
                        relationship.confidence,
                        relationship.extraction_version,
                        json.dumps(relationship.evidence, default=str),
                    ),
                )
            await conn.execute(
                "update graph.projection_versions set status = 'inactive' where status = 'active'"
            )
            await conn.execute(
                """
                update graph.projection_versions
                set status = 'active', activated_at = now()
                where version = %s
                """,
                (version,),
            )
            await conn.execute(
                """
                update graph.projection_runs
                set status = 'done', relationship_count = %s, node_count = %s,
                    duration_ms = %s, finished_at = now()
                where run_id = %s
                """,
                (
                    len(relationships),
                    await _node_count(conn),
                    int((time.monotonic() - started) * 1000),
                    run_id,
                ),
            )
        logger.info(
            "graph projection built",
            extra={"version": version, "relationships": len(relationships)},
        )
        return {
            "version": version,
            "run_id": run_id,
            "relationships": len(relationships),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        await db.execute(
            """
            update graph.projection_runs
            set status = 'failed', error = %s, finished_at = now()
            where run_id = %s
            """,
            (str(exc)[:2000], run_id),
        )
        await db.execute(
            "update graph.projection_versions set status = 'failed' where version = %s",
            (version,),
        )
        raise


async def _node_count(conn: Any) -> int:
    cursor = await conn.execute("select count(*)::int as count from graph.nodes")
    row = await cursor.fetchone()
    return int(row["count"]) if row else 0


async def _collect(db: Database) -> list[Relationship]:
    documents = await db.fetch_all(
        """
        select d.document_id, d.tenant_id, d.host, d.state, d.current_revision,
               d.first_seen_at, t.body, t.title, r.published_at,
               (select f.source_id::text from fetcher.jobs f
                where f.document_id = d.document_id and f.source_id is not null
                order by f.created_at desc limit 1) as source_id
        from core.documents d
        join core.document_text t
          on t.document_id = d.document_id and t.revision = d.current_revision
        left join core.document_revisions r
          on r.document_id = d.document_id and r.revision = d.current_revision
        """
    )
    mentions = await db.fetch_all(
        """
        select m.document_id, m.entity_id, m.surface_form, m.char_start, m.char_end,
               m.confidence, m.revision, e.entity_type, e.canonical_name
        from graph.entity_mentions m
        join core.documents d
          on d.document_id = m.document_id and d.current_revision = m.revision
        join graph.entities e on e.entity_id = m.entity_id
        """
    )
    claims = await db.fetch_all(
        "select * from graph.live_claims where status = 'active' and span_verified"
    )
    events = await db.fetch_all("select * from graph.live_events")
    lineage = await db.fetch_all("select * from dedup.lineage_edges")
    entity_rows = await db.fetch_all(
        """
        select entity_id, entity_type, normalized_name, canonical_name
        from graph.entities where merged_into is null
        """
    )
    entities = {row["entity_id"]: row for row in entity_rows}
    entity_by_key = {
        (row["normalized_name"], row["entity_type"]): row["entity_id"] for row in entity_rows
    }
    documents_by_id = {row["document_id"]: row for row in documents}
    mentions_by_doc: dict[str, list[dict[str, Any]]] = {}
    for mention in mentions:
        mentions_by_doc.setdefault(mention["document_id"], []).append(mention)

    relationships: list[Relationship] = []
    for document in documents:
        published = _aware(document["published_at"] or document["first_seen_at"])
        observed = _aware(document["first_seen_at"])
        if document["source_id"]:
            relationships.append(
                Relationship(
                    "DERIVED_FROM",
                    "document",
                    document["document_id"],
                    "source",
                    document["source_id"],
                    published,
                    observed,
                    document["document_id"],
                    0.9,
                    EXTRACTION_VERSION,
                    evidence={"source_id": document["source_id"]},
                )
            )
        for mention in mentions_by_doc.get(document["document_id"], []):
            relationships.append(
                Relationship(
                    "MENTIONED_IN",
                    "entity",
                    mention["entity_id"],
                    "document",
                    document["document_id"],
                    published,
                    observed,
                    document["document_id"],
                    float(mention["confidence"]),
                    EXTRACTION_VERSION,
                    evidence={
                        "surface": mention["surface_form"],
                        "char_start": mention["char_start"],
                    },
                )
            )

    for claim in claims:
        doc = documents_by_id.get(claim["document_id"])
        if doc is None:
            continue
        valid = _aware(claim["published_at"] or doc["first_seen_at"])
        observed = _aware(doc["first_seen_at"])
        relationships.append(
            Relationship(
                "DERIVED_FROM",
                "claim",
                claim["claim_id"],
                "document",
                claim["document_id"],
                valid,
                observed,
                claim["document_id"],
                float(claim["extraction_confidence"]),
                claim["extractor_version"],
                source_claim_id=claim["claim_id"],
                evidence={"predicate": claim["predicate"], "quote": claim["quote"]},
            )
        )
        relationships.append(
            Relationship(
                "SUPPORTS",
                "document",
                claim["document_id"],
                "claim",
                claim["claim_id"],
                valid,
                observed,
                claim["document_id"],
                1.0,
                claim["extractor_version"],
                source_claim_id=claim["claim_id"],
                evidence={"quote": claim["quote"]},
            )
        )
        relation = _CLAIM_RELATIONS.get(claim["predicate"])
        if (
            relation
            and claim["subject_entity_id"]
            and claim["object_entity_id"]
            and claim["subject_entity_id"] != claim["object_entity_id"]
        ):
            relationships.append(
                Relationship(
                    relation,
                    "entity",
                    claim["subject_entity_id"],
                    "entity",
                    claim["object_entity_id"],
                    valid,
                    observed,
                    claim["document_id"],
                    float(claim["extraction_confidence"]),
                    claim["extractor_version"],
                    source_claim_id=claim["claim_id"],
                    evidence={"claim": claim["claim_text"], "quote": claim["quote"]},
                )
            )

    for event in events:
        doc = documents_by_id.get(event["document_id"])
        if doc is None:
            continue
        valid = _aware(event["event_time"] or doc["published_at"] or doc["first_seen_at"])
        observed = _aware(doc["first_seen_at"])
        subject = event.get("subject_entity_id")
        obj = event.get("object_entity_id")
        event_type = event["event_type"]
        location = _find_entity(entity_by_key, event.get("location_text"), "location")
        relationships.append(
            Relationship(
                "DERIVED_FROM",
                "event",
                event["event_id"],
                "document",
                event["document_id"],
                valid,
                observed,
                event["document_id"],
                float(event["confidence"]),
                event["extractor_version"],
                source_event_id=event["event_id"],
                evidence={"quote": event["quote"]},
            )
        )
        relationships.append(
            Relationship(
                "SUPPORTS",
                "document",
                event["document_id"],
                "event",
                event["event_id"],
                valid,
                observed,
                event["document_id"],
                1.0,
                event["extractor_version"],
                source_event_id=event["event_id"],
                evidence={"quote": event["quote"]},
            )
        )
        if subject:
            relationships.append(
                Relationship(
                    "RELATED_TO",
                    "entity",
                    subject,
                    "event",
                    event["event_id"],
                    valid,
                    observed,
                    event["document_id"],
                    float(event["confidence"]),
                    event["extractor_version"],
                    source_event_id=event["event_id"],
                    evidence={"event_type": event["event_type"], "quote": event["quote"]},
                )
            )
            if event_type == "investment_announcement":
                target_kind, target_id = _event_target(obj, entities, event)
                relationships.append(
                    Relationship(
                        "INVESTED_IN",
                        "entity",
                        subject,
                        target_kind,
                        target_id,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={
                            "amount": float(event["amount"])
                            if event["amount"] is not None
                            else None,
                            "currency": event["currency"],
                            "original": event["amount_original"],
                        },
                    )
                )
                relationships.append(
                    Relationship(
                        "ANNOUNCED",
                        "entity",
                        subject,
                        "event",
                        event["event_id"],
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"quote": event["quote"]},
                    )
                )
            elif event_type == "policy_approval" and obj:
                relationships.append(
                    Relationship(
                        "APPROVED",
                        "entity",
                        subject,
                        "entity",
                        obj,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"quote": event["quote"]},
                    )
                )
            elif event_type == "acquisition_completed" and obj:
                relationships.append(
                    Relationship(
                        "OWNS",
                        "entity",
                        subject,
                        "entity",
                        obj,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"quote": event["quote"]},
                    )
                )
            elif event_type == "product_launch" and obj:
                relationships.append(
                    Relationship(
                        "PRODUCES",
                        "entity",
                        subject,
                        "entity",
                        obj,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"quote": event["quote"]},
                    )
                )
            elif event_type == "contract_signed" and obj:
                relationships.append(
                    Relationship(
                        "PARTNERED_WITH",
                        "entity",
                        subject,
                        "entity",
                        obj,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"quote": event["quote"]},
                    )
                )
            elif event_type == "investigation_started" and obj:
                relationships.append(
                    Relationship(
                        "REGULATES",
                        "entity",
                        subject,
                        "entity",
                        obj,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"quote": event["quote"]},
                    )
                )
        if location:
            relationships.append(
                Relationship(
                    "LOCATED_IN",
                    "event",
                    event["event_id"],
                    "entity",
                    location,
                    valid,
                    observed,
                    event["document_id"],
                    float(event["confidence"]),
                    event["extractor_version"],
                    source_event_id=event["event_id"],
                    evidence={"location": event["location_text"]},
                )
            )
            if event["event_type"] == "investment_announcement" and obj:
                relationships.append(
                    Relationship(
                        "LOCATED_IN",
                        "entity",
                        obj,
                        "entity",
                        location,
                        valid,
                        observed,
                        event["document_id"],
                        float(event["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"location": event["location_text"]},
                    )
                )
        span_mentions = [
            mention
            for mention in mentions_by_doc.get(event["document_id"], [])
            if mention["char_start"] >= event["char_start"]
            and mention["char_end"] <= event["char_end"]
        ]
        if event_type == "policy_approval" and subject:
            for mention in span_mentions:
                if mention["entity_type"] == "policy":
                    relationships.append(
                        Relationship(
                            "APPROVED",
                            "entity",
                            subject,
                            "entity",
                            mention["entity_id"],
                            valid,
                            observed,
                            event["document_id"],
                            float(event["confidence"]),
                            event["extractor_version"],
                            source_event_id=event["event_id"],
                            evidence={"quote": event["quote"]},
                        )
                    )
        for mention in span_mentions:
            if mention["entity_type"] not in {"industry", "technology", "product"}:
                continue
            host = obj or subject
            if host:
                if event_type == "policy_approval" and mention["entity_type"] != "product":
                    rel_type = "REGULATES"
                elif mention["entity_type"] == "product":
                    rel_type = "PRODUCES"
                else:
                    rel_type = "USES_TECHNOLOGY"
                relationships.append(
                    Relationship(
                        rel_type,
                        "entity",
                        host,
                        "entity",
                        mention["entity_id"],
                        valid,
                        observed,
                        event["document_id"],
                        float(mention["confidence"]),
                        event["extractor_version"],
                        source_event_id=event["event_id"],
                        evidence={"term": mention["surface_form"], "quote": event["quote"]},
                    )
                )

    for edge in lineage:
        relation = str(edge["relation"])
        rel_type = (
            "DERIVED_FROM"
            if relation in {"syndicated_copy", "repeated_claim", "cites"}
            else "RELATED_TO"
        )
        valid = _aware(edge["created_at"])
        relationships.append(
            Relationship(
                rel_type,
                "document",
                edge["document_id"],
                "document",
                edge["related_document_id"],
                valid,
                valid,
                edge["document_id"],
                float(edge["confidence"]),
                DERIVED_VERSION,
                evidence={"lineage": relation, "detail": edge["evidence"]},
            )
        )

    relationships.extend(_supplier_and_status_edges(documents, mentions_by_doc))
    relationships = _apply_cancellations(relationships)
    return _dedupe(relationships)


def _supplier_and_status_edges(
    documents: list[dict[str, Any]], mentions_by_doc: dict[str, list[dict[str, Any]]]
) -> list[Relationship]:
    edges: list[Relationship] = []
    for document in documents:
        body = document.get("body") or ""
        mentions = mentions_by_doc.get(document["document_id"], [])
        published = _aware(document["published_at"] or document["first_seen_at"])
        observed = _aware(document["first_seen_at"])
        if not body:
            continue
        for sentence in sentences(body):
            lower = sentence.text.casefold()
            inside = [
                mention
                for mention in mentions
                if mention["char_start"] >= sentence.start and mention["char_end"] <= sentence.end
            ]
            organizations = [
                mention
                for mention in inside
                if mention["entity_type"] in {"organization", "government"}
            ]
            host = next(
                (
                    mention
                    for mention in inside
                    if mention["entity_type"] in {"project", "organization", "government"}
                ),
                None,
            )
            if (
                host
                and any(word in lower for word in ("supplier", "supply chain", "supplies"))
                and len(organizations) >= 2
            ):
                first, second = organizations[0], organizations[1]
                if first["entity_id"] != second["entity_id"]:
                    edges.append(
                        Relationship(
                            "SUPPLIES",
                            "entity",
                            first["entity_id"],
                            "entity",
                            second["entity_id"],
                            published,
                            observed,
                            document["document_id"],
                            0.5,
                            DERIVED_VERSION,
                            evidence={"quote": sentence.text},
                        )
                    )
            if host and any(
                word in lower for word in ("delay", "postpone", "postponed", "slipped")
            ):
                edges.append(
                    Relationship(
                        "DELAYED",
                        "entity",
                        host["entity_id"],
                        "document",
                        document["document_id"],
                        published,
                        observed,
                        document["document_id"],
                        0.55,
                        DERIVED_VERSION,
                        evidence={"quote": sentence.text},
                    )
                )
            if host and any(
                word in lower for word in ("cancel", "cancelled", "canceled", "scrapped")
            ):
                edges.append(
                    Relationship(
                        "CANCELLED",
                        "entity",
                        host["entity_id"],
                        "document",
                        document["document_id"],
                        published,
                        observed,
                        document["document_id"],
                        0.55,
                        DERIVED_VERSION,
                        evidence={"quote": sentence.text},
                    )
                )
    return edges


def _apply_cancellations(relationships: list[Relationship]) -> list[Relationship]:
    cancelled: dict[str, datetime] = {}
    for relationship in relationships:
        if relationship.relationship_type == "CANCELLED":
            cancelled[relationship.src_id] = relationship.valid_from
    if not cancelled:
        return relationships
    for relationship in relationships:
        if relationship.relationship_type not in {"INVESTED_IN", "ANNOUNCED"}:
            continue
        if relationship.src_kind != "entity":
            continue
        stop = cancelled.get(relationship.src_id)
        if stop and relationship.valid_from <= stop and relationship.valid_to is None:
            relationship.valid_to = stop
    return relationships


def _event_target(
    object_id: str | None, entities: dict[str, dict[str, Any]], event: dict[str, Any]
) -> tuple[str, str]:
    if object_id:
        entity = entities.get(object_id)
        if entity and entity["entity_type"] in {
            "project",
            "organization",
            "government",
            "location",
            "policy",
        }:
            return "entity", object_id
    return "event", event["event_id"]


def _find_entity(
    entity_by_key: dict[tuple[str, str], str], surface: str | None, entity_type: str
) -> str | None:
    if not surface:
        return None
    key = resolver.normalize_name(surface, entity_type)
    return entity_by_key.get((key, entity_type)) or entity_by_key.get((key, "location"))


def _dedupe(relationships: list[Relationship]) -> list[Relationship]:
    best: dict[tuple[Any, ...], Relationship] = {}
    for relationship in relationships:
        if not relationship.shape_valid():
            continue
        key = (
            relationship.relationship_type,
            relationship.src_kind,
            relationship.src_id,
            relationship.dst_kind,
            relationship.dst_id,
            relationship.source_document_id,
            relationship.valid_from,
        )
        existing = best.get(key)
        if existing is None or relationship.confidence > existing.confidence:
            best[key] = relationship
    return list(best.values())


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
