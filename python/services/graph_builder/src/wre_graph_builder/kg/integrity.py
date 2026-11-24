"""Graph integrity checks over the active projection."""

from __future__ import annotations

from typing import Any

from wre_pipeline.db import Database

from .model import SHAPES


async def issues(db: Database) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    active = await db.fetch_one("select * from graph.active_projection")
    if active is None:
        findings.append({"kind": "missing_projection", "severity": "error", "details": {}})
        return findings

    duplicates = await db.fetch_all(
        """
        select tenant_id, entity_type, normalized_name, count(*)::int as count
        from graph.entities
        where merged_into is null
        group by tenant_id, entity_type, normalized_name
        having count(*) > 1
        order by count desc
        limit 50
        """
    )
    for row in duplicates:
        findings.append(
            {
                "kind": "duplicate_entities",
                "severity": "warning",
                "details": row,
            }
        )

    orphans = await db.fetch_all(
        """
        select e.entity_id, e.canonical_name, e.entity_type
        from graph.entities e
        where e.merged_into is null
          and not exists (select 1 from graph.entity_mentions m where m.entity_id = e.entity_id)
          and not exists (
            select 1 from graph.active_relationships r
            where (r.src_kind = 'entity' and r.src_id = e.entity_id)
               or (r.dst_kind = 'entity' and r.dst_id = e.entity_id)
          )
        limit 50
        """
    )
    for row in orphans:
        findings.append({"kind": "orphan_entity", "severity": "info", "details": row})

    dangling = await db.fetch_all(
        """
        select r.relationship_id, r.relationship_type, r.src_kind, r.src_id,
               r.dst_kind, r.dst_id
        from graph.active_relationships r
        where not exists (
                select 1 from graph.nodes n where n.kind = r.src_kind and n.id = r.src_id)
           or not exists (
                select 1 from graph.nodes n where n.kind = r.dst_kind and n.id = r.dst_id)
        limit 50
        """
    )
    for row in dangling:
        findings.append({"kind": "dangling_relationship", "severity": "error", "details": row})

    shapes = await db.fetch_all(
        "select distinct src_kind, dst_kind from graph.active_relationships"
    )
    for row in shapes:
        if row["dst_kind"] not in SHAPES.get(row["src_kind"], set()):
            findings.append(
                {
                    "kind": "unsupported_shape",
                    "severity": "error",
                    "details": row,
                }
            )

    unverified = await db.fetch_one(
        """
        select count(*)::int as count from graph.claims
        where status = 'active' and not span_verified
        """
    )
    if unverified and unverified["count"]:
        findings.append(
            {
                "kind": "unverified_claims",
                "severity": "warning",
                "details": {"count": unverified["count"]},
            }
        )
    return findings
