"""Named graph queries over the active temporal projection."""

from __future__ import annotations

from typing import Any

from wre_pipeline.db import Database


async def investments_by_location_industry(
    db: Database, location: str, industry: str
) -> list[dict[str, Any]]:
    location_like = f"%{location}%" if location else ""
    industry_like = f"%{industry}%" if industry else ""
    return await db.fetch_all(
        """
        select distinct company.label as company, project.label as project,
               loc.label as location, ind.label as industry
        from graph.active_relationships inv
        join graph.nodes company on company.kind = 'entity' and company.id = inv.src_id
        join graph.nodes project on project.kind = 'entity' and project.id = inv.dst_id
        left join graph.active_relationships locrel
          on locrel.src_kind = 'entity' and locrel.src_id = project.id
         and locrel.relationship_type = 'LOCATED_IN'
        left join graph.nodes loc on loc.kind = 'entity' and loc.id = locrel.dst_id
        left join graph.active_relationships indrel
          on indrel.src_kind = 'entity' and indrel.src_id = project.id
         and indrel.relationship_type IN ('USES_TECHNOLOGY', 'PRODUCES')
        left join graph.nodes ind on ind.kind = 'entity' and ind.id = indrel.dst_id
        where inv.relationship_type = 'INVESTED_IN' and inv.dst_kind = 'entity'
          and (%s = '' or loc.label ilike %s)
          and (%s = '' or ind.label ilike %s)
        order by company
        """,
        (location_like, location_like, industry_like, industry_like),
    )


async def policies_for_investments(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select distinct gov.label as government, policy.label as policy,
               ind.label as industry
        from graph.active_relationships app
        join graph.nodes gov on gov.kind = 'entity' and gov.id = app.src_id
        join graph.nodes policy on policy.kind = 'entity' and policy.id = app.dst_id
        left join graph.active_relationships reg
          on reg.src_kind = 'entity' and reg.src_id = policy.id
         and reg.relationship_type = 'REGULATES'
        left join graph.nodes ind on ind.kind = 'entity' and ind.id = reg.dst_id
        where app.relationship_type = 'APPROVED' and policy.subtype = 'policy'
        order by policy
        limit %s
        """,
        (limit,),
    )


async def shared_suppliers(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select supplier.label as supplier, array_agg(distinct company.label) as companies
        from graph.active_relationships r
        join graph.nodes supplier on supplier.kind = 'entity' and supplier.id = r.src_id
        join graph.nodes company on company.kind = 'entity' and company.id = r.dst_id
        where r.relationship_type = 'SUPPLIES'
        group by supplier.label
        order by count(distinct company.label) desc
        limit %s
        """,
        (limit,),
    )


async def delayed_projects(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select distinct host.label as project, r.relationship_type as status,
               r.valid_from, r.source_document_id, r.evidence->>'quote' as quote
        from graph.active_relationships r
        join graph.nodes host on host.kind = 'entity' and host.id = r.src_id
        where r.relationship_type in ('DELAYED', 'CANCELLED')
        order by r.valid_from desc
        limit %s
        """,
        (limit,),
    )


async def developments_after_policy(db: Database, policy: str, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        with chosen_policy as (
          select r.valid_from, r.dst_id
          from graph.active_relationships r
          join graph.nodes n on n.kind = 'entity' and n.id = r.dst_id
          where r.relationship_type = 'APPROVED'
            and n.label ilike %s
          order by r.valid_from desc
          limit 1
        )
        select distinct company.label as company, project.label as project,
               inv.valid_from as invested_at, p.valid_from as policy_at
        from chosen_policy p
        join graph.active_relationships inv
          on inv.relationship_type = 'INVESTED_IN' and inv.valid_from > p.valid_from
        join graph.nodes company on company.kind = 'entity' and company.id = inv.src_id
        join graph.nodes project on project.kind = 'entity' and project.id = inv.dst_id
        order by invested_at desc
        limit %s
        """,
        (f"%{policy}%", limit),
    )


async def claims_multiple_sources(
    db: Database, min_sources: int, limit: int
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select c.claim_key, c.normalized_subject, c.normalized_predicate,
               c.normalized_object,
               count(distinct d.host)::int as independent_sources,
               array_agg(distinct d.host) as hosts,
               array_agg(distinct c.document_id) as documents
        from graph.live_claims c
        join core.documents d on d.document_id = c.document_id
        left join dedup.story_members m on m.document_id = c.document_id
        where c.status = 'active' and c.span_verified
          and d.state <> 'duplicate'
          and coalesce(m.relation, 'original') not in ('syndicated_copy', 'repeated_claim')
        group by c.claim_key, c.normalized_subject, c.normalized_predicate, c.normalized_object
        having count(distinct d.host) >= %s
        order by independent_sources desc
        limit %s
        """,
        (min_sources, limit),
    )


async def connected_to_event(db: Database, event_id: str, depth: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        with recursive reach(kind, id, depth, path) as (
          select 'event'::text, %s::text, 0, array[%s::text]
          union all
          select x.kind, x.id, r.depth + 1, r.path || x.id
          from reach r
          join graph.active_relationships e
            on (e.src_kind = r.kind and e.src_id = r.id)
            or (e.dst_kind = r.kind and e.dst_id = r.id)
          cross join lateral (
            select case when e.src_kind = r.kind and e.src_id = r.id
                        then e.dst_kind else e.src_kind end as kind,
                   case when e.src_kind = r.kind and e.src_id = r.id
                        then e.dst_id else e.src_id end as id
          ) x
          where r.depth < %s and not (x.id = any(r.path))
        )
        select n.kind, n.id, n.label, min(r.depth)::int as depth
        from reach r
        join graph.nodes n on n.kind = r.kind and n.id = r.id
        where r.depth > 0
        group by n.kind, n.id, n.label
        order by depth, n.kind, n.label
        limit 200
        """,
        (event_id, event_id, depth),
    )


async def entity_neighborhood(db: Database, entity_id: str, depth: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        with recursive reach(kind, id, depth, path) as (
          select 'entity'::text, %s::text, 0, array[%s::text]
          union all
          select x.kind, x.id, r.depth + 1, r.path || x.id
          from reach r
          join graph.active_relationships e
            on (e.src_kind = r.kind and e.src_id = r.id)
            or (e.dst_kind = r.kind and e.dst_id = r.id)
          cross join lateral (
            select case when e.src_kind = r.kind and e.src_id = r.id
                        then e.dst_kind else e.src_kind end as kind,
                   case when e.src_kind = r.kind and e.src_id = r.id
                        then e.dst_id else e.src_id end as id
          ) x
          where r.depth < %s and not (x.id = any(r.path))
        )
        select n.kind, n.id, n.label, min(r.depth)::int as depth
        from reach r
        join graph.nodes n on n.kind = r.kind and n.id = r.id
        where r.depth > 0
        group by n.kind, n.id, n.label
        order by depth, n.kind, n.label
        limit 200
        """,
        (entity_id, entity_id, depth),
    )


async def event_entities(db: Database, event_id: str) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select distinct n.label, n.subtype, r.relationship_type, r.confidence
        from graph.active_relationships r
        join graph.nodes n
          on (r.src_kind = 'event' and r.src_id = %s and n.kind = r.dst_kind and n.id = r.dst_id)
          or (r.dst_kind = 'event' and r.dst_id = %s and n.kind = r.src_kind and n.id = r.src_id)
        where n.kind = 'entity'
        order by n.label
        """,
        (event_id, event_id),
    )


async def relationships(
    db: Database,
    relationship_type: str,
    entity_id: str,
    as_of: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select r.*, src.label as src_label, dst.label as dst_label
        from graph.active_relationships r
        left join graph.nodes src on src.kind = r.src_kind and src.id = r.src_id
        left join graph.nodes dst on dst.kind = r.dst_kind and dst.id = r.dst_id
        where (%s = '' or r.relationship_type = %s)
          and (%s = '' or r.src_id = %s or r.dst_id = %s)
          and (%s::timestamptz is null
               or (r.valid_from <= %s::timestamptz
                   and (r.valid_to is null or r.valid_to > %s::timestamptz)))
        order by r.valid_from desc
        limit %s
        """,
        (
            relationship_type,
            relationship_type,
            entity_id,
            entity_id,
            entity_id,
            as_of,
            as_of,
            as_of,
            limit,
        ),
    )


async def stats(db: Database) -> dict[str, Any]:
    version = await db.fetch_one("select * from graph.active_projection")
    nodes = await db.fetch_all(
        "select kind, count(*)::int as count from graph.nodes group by kind order by kind"
    )
    rels = await db.fetch_all(
        """
        select relationship_type, count(*)::int as count
        from graph.active_relationships
        group by relationship_type order by count desc
        """
    )
    return {
        "active_version": version,
        "nodes": {row["kind"]: row["count"] for row in nodes},
        "relationships": {row["relationship_type"]: row["count"] for row in rels},
    }


async def versions(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select v.*,
               (select count(*)::int from graph.projection_runs r
                where r.projection_version = v.version) as runs
        from graph.projection_versions v
        order by v.created_at desc limit %s
        """,
        (limit,),
    )
