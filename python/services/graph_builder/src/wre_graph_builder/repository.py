"""Postgres queries for dedup clusters and entity resolution."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ulid import ULID
from wre_pipeline.db import Database

from . import dedup, resolver

DEDUP_VERSION = "dedup-1.0.0"
ENTITY_VERSION = "entity-resolver-1.0.0"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


async def find_entities(
    db: Database, tenant_id: str, data: resolver.EntityInput
) -> list[resolver.MatchCandidate]:
    normalized = resolver.normalize_name(data.name, data.entity_type)
    official_values = [str(value) for value in (data.official_ids or {}).values() if value] or None
    rows = await db.fetch_all(
        """
        select e.*, greatest(
            similarity(e.normalized_name, %s),
            coalesce((select max(similarity(a, %s)) from unnest(e.alias_keys) a), 0)
        ) as name_similarity
        from graph.entities e
        where e.tenant_id = %s and e.merged_into is null
          and (%s::text is null or e.entity_type = %s)
          and (
            e.normalized_name = %s
            or %s = any(e.alias_keys)
            or (%s::text is not null and e.external_ids->>'domain' = %s)
            or (%s::text[] is not null and exists (
                 select 1 from jsonb_each_text(e.external_ids) x where x.value = any(%s::text[])))
            or similarity(e.normalized_name, %s) > 0.25
            or exists (select 1 from unnest(e.alias_keys) a where similarity(a, %s) > 0.25)
          )
        order by name_similarity desc
        limit 12
        """,
        (
            normalized,
            normalized,
            tenant_id,
            data.entity_type,
            data.entity_type,
            normalized,
            normalized,
            data.domain,
            data.domain,
            official_values,
            official_values,
            normalized,
            normalized,
        ),
    )
    return [
        resolver.MatchCandidate(
            entity_id=row["entity_id"],
            canonical_name=row["canonical_name"],
            normalized_name=row["normalized_name"],
            alias_keys=list(row["alias_keys"] or []),
            external_ids=dict(row["external_ids"] or {}),
            country=row["country"],
            entity_type=row["entity_type"],
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]


async def create_entity(
    db: Database, tenant_id: str, data: resolver.EntityInput, confidence: float
) -> dict[str, Any]:
    entity_type = data.entity_type or "other"
    normalized = resolver.normalize_name(data.name, entity_type)
    display = resolver.display_name(data.name)
    key = resolver.alias_key(data.name, entity_type)
    external_ids = dict(data.official_ids or {})
    if data.domain:
        external_ids.setdefault("domain", data.domain)
    entity_id = str(ULID())
    row = await db.fetch_one(
        """
        insert into graph.entities
            (entity_id, tenant_id, entity_type, canonical_name, normalized_name,
             aliases, alias_keys, external_ids, country, confidence)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (tenant_id, entity_type, normalized_name) where merged_into is null do nothing
        returning *
        """,
        (
            entity_id,
            tenant_id,
            entity_type,
            display,
            normalized,
            [display],
            [key],
            _json(external_ids),
            data.country,
            confidence,
        ),
    )
    if row:
        return row
    existing = await db.fetch_one(
        """
        select * from graph.entities
        where tenant_id = %s and entity_type = %s and normalized_name = %s
          and merged_into is null
        """,
        (tenant_id, entity_type, normalized),
    )
    return existing or {}


async def add_alias(
    db: Database, entity_id: str, surface: str, entity_type: str | None = None
) -> None:
    display = resolver.display_name(surface)
    key = resolver.alias_key(surface, entity_type)
    await db.execute(
        """
        update graph.entities
        set aliases = case
              when canonical_name = %s or %s = any(aliases) then aliases
              else array_append(aliases, %s)
            end,
            alias_keys = case
              when normalized_name = %s or %s = any(alias_keys) then alias_keys
              else array_append(alias_keys, %s)
            end,
            updated_at = now()
        where entity_id = %s
        """,
        (display, display, display, key, key, key, entity_id),
    )


async def record_candidate(
    db: Database,
    *,
    tenant_id: str,
    data: resolver.EntityInput,
    candidate_entity_id: str | None,
    score: float,
    features: dict[str, Any],
    decision: str,
    review_status: str,
    resolved_entity_id: str | None,
) -> str:
    candidate_id = str(ULID())
    await db.execute(
        """
        insert into graph.entity_match_candidates
            (candidate_id, tenant_id, input_name, normalized_name, entity_type,
             domain, country, candidate_entity_id, match_score, match_features,
             decision_status, model_version, review_status, resolved_entity_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            candidate_id,
            tenant_id,
            data.name,
            resolver.normalize_name(data.name, data.entity_type),
            data.entity_type,
            data.domain,
            data.country,
            candidate_entity_id,
            score,
            _json(features),
            decision,
            ENTITY_VERSION,
            review_status,
            resolved_entity_id,
        ),
    )
    return candidate_id


async def list_candidates(
    db: Database, review_status: str | None, limit: int
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select candidate_id, input_name, normalized_name, entity_type, domain, country,
               candidate_entity_id, match_score, match_features, decision_status,
               model_version, review_status, resolved_entity_id, created_at, reviewed_at
        from graph.entity_match_candidates
        where (%s::text is null or review_status = %s)
        order by created_at desc limit %s
        """,
        (review_status, review_status, limit),
    )


async def review_candidate(db: Database, candidate_id: str, review: str) -> dict[str, Any] | None:
    candidate = await db.fetch_one(
        "select * from graph.entity_match_candidates where candidate_id = %s",
        (candidate_id,),
    )
    if candidate is None:
        return None
    if candidate["review_status"] != "pending":
        return candidate
    if review == "accepted" and candidate["candidate_entity_id"]:
        await add_alias(
            db,
            candidate["candidate_entity_id"],
            candidate["input_name"],
            candidate["entity_type"],
        )
    resolved = candidate["candidate_entity_id"] if review == "accepted" else None
    await db.execute(
        """
        update graph.entity_match_candidates
        set review_status = %s, resolved_entity_id = %s, reviewed_at = now()
        where candidate_id = %s
        """,
        (review, resolved, candidate_id),
    )
    return await db.fetch_one(
        "select * from graph.entity_match_candidates where candidate_id = %s", (candidate_id,)
    )


async def merge_entities(
    db: Database, from_id: str, into_id: str, method: str, confidence: float, performed_by: str
) -> dict[str, Any] | None:
    async with db.transaction() as conn:
        source = await (
            await conn.execute("select * from graph.entities where entity_id = %s", (from_id,))
        ).fetchone()
        target = await (
            await conn.execute("select * from graph.entities where entity_id = %s", (into_id,))
        ).fetchone()
        if source is None or target is None or from_id == into_id:
            return None
        await conn.execute(
            """
            insert into graph.entity_merges
                (from_entity_id, into_entity_id, method, confidence, performed_by)
            values (%s, %s, %s, %s, %s)
            """,
            (from_id, into_id, method, confidence, performed_by),
        )
        await conn.execute(
            """
            update graph.entities set merged_into = %s, updated_at = now()
            where entity_id = %s
            """,
            (into_id, from_id),
        )
        aliases = list(
            dict.fromkeys(
                (target["aliases"] or []) + (source["aliases"] or []) + [source["canonical_name"]]
            )
        )
        keys = list(dict.fromkeys((target["alias_keys"] or []) + (source["alias_keys"] or [])))
        await conn.execute(
            """
            update graph.entities set aliases = %s, alias_keys = %s, updated_at = now()
            where entity_id = %s
            """,
            (aliases, keys, into_id),
        )
        await conn.execute(
            "update graph.entity_mentions set entity_id = %s where entity_id = %s",
            (into_id, from_id),
        )
    return await get_entity(db, into_id)


async def get_entity(db: Database, entity_id: str) -> dict[str, Any] | None:
    entity = await db.fetch_one("select * from graph.entities where entity_id = %s", (entity_id,))
    if entity is None:
        return None
    entity["mentions"] = await db.fetch_one(
        "select count(*)::int as count from graph.entity_mentions where entity_id = %s",
        (entity_id,),
    )
    entity["merges"] = await db.fetch_all(
        """
        select * from graph.entity_merges
        where from_entity_id = %s or into_entity_id = %s
        order by created_at
        """,
        (entity_id, entity_id),
    )
    return entity


async def list_entities(
    db: Database, query: str | None, entity_type: str | None, limit: int
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select entity_id, entity_type, canonical_name, aliases, external_ids, country,
               confidence, created_at, updated_at
        from graph.entities
        where merged_into is null
          and (%s::text is null or entity_type = %s)
          and (%s::text is null or canonical_name ilike %s or %s = any(alias_keys))
        order by updated_at desc limit %s
        """,
        (
            entity_type,
            entity_type,
            query,
            f"%{query}%" if query else None,
            resolver.normalize_name(query) if query else None,
            limit,
        ),
    )


async def graph_stats(db: Database) -> dict[str, Any]:
    entities = await db.fetch_one(
        "select count(*)::int as count from graph.entities where merged_into is null"
    )
    pending = await db.fetch_one(
        """
        select count(*)::int as count from graph.entity_match_candidates
        where review_status = 'pending'
        """
    )
    merges = await db.fetch_one("select count(*)::int as count from graph.entity_merges")
    by_type = await db.fetch_all(
        """
        select entity_type, count(*)::int as count from graph.entities
        where merged_into is null group by entity_type order by count desc
        """
    )
    return {
        "entities": entities["count"] if entities else 0,
        "by_type": {row["entity_type"]: row["count"] for row in by_type},
        "pending_reviews": pending["count"] if pending else 0,
        "merges": merges["count"] if merges else 0,
    }


# ---------------------------------------------------------------------------
# Documents and clusters
# ---------------------------------------------------------------------------


async def load_document(db: Database, document_id: str, revision: int) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        select d.document_id, d.tenant_id, d.host, d.state, d.current_revision,
               d.first_seen_at, d.duplicate_of,
               r.revision, r.published_at, r.created_at as fetched_at,
               o.title, o.body, o.simhash, o.language, o.metadata, o.quality_score,
               encode(o.content_hash_norm, 'hex') as content_hash, o.is_duplicate
        from core.documents d
        join core.document_revisions r
          on r.document_id = d.document_id and r.revision = %s
        join processing.live_outputs o
          on o.document_id = d.document_id and o.revision = r.revision
        where d.document_id = %s
        """,
        (revision, document_id),
    )


async def near_candidates(
    db: Database, document: dict[str, Any], max_distance: int, title_floor: float
) -> list[dedup.Candidate]:
    rows = await db.fetch_all(
        """
        select d.document_id, d.current_revision, d.tenant_id, d.host, d.state,
               d.first_seen_at, r.published_at,
               o.title, left(o.body, 100000) as body, o.simhash, o.language,
               encode(o.content_hash_norm, 'hex') as content_hash,
               m.cluster_id
        from processing.live_outputs o
        join core.documents d on d.document_id = o.document_id
        join core.document_revisions r
          on r.document_id = d.document_id and r.revision = d.current_revision
        left join dedup.story_members m on m.document_id = d.document_id
        where o.document_id <> %s
          and d.tenant_id = %s
          and o.revision = d.current_revision
          and (o.language = 'und' or %s = 'und' or o.language = %s)
          and (
            bit_count((o.simhash # %s::bigint)::bit(64)) <= %s
            or similarity(o.title, %s) > %s
          )
        order by bit_count((o.simhash # %s::bigint)::bit(64)), similarity(o.title, %s) desc
        limit 40
        """,
        (
            document["document_id"],
            document["tenant_id"],
            document["language"],
            document["language"],
            document["simhash"],
            max_distance,
            document["title"],
            title_floor,
            document["simhash"],
            document["title"],
        ),
    )
    return [
        dedup.Candidate(
            document_id=row["document_id"],
            revision=row["current_revision"],
            tenant_id=row["tenant_id"],
            host=row["host"],
            title=row["title"],
            body=row["body"],
            simhash=row["simhash"],
            content_hash=row["content_hash"],
            language=row["language"],
            published_at=row["published_at"],
            first_seen_at=row["first_seen_at"],
            cluster_id=row["cluster_id"],
            state=row["state"],
        )
        for row in rows
    ]


async def create_cluster(
    db: Database, tenant_id: str, title: str, representative: str, confidence: float
) -> str:
    cluster_id = str(ULID())
    await db.execute(
        """
        insert into dedup.story_clusters
            (cluster_id, tenant_id, title, representative_document_id, confidence, model_version)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (cluster_id, tenant_id, title, representative, confidence, DEDUP_VERSION),
    )
    return cluster_id


async def enqueue_document(db: Database, document_id: str) -> int:
    return await db.execute(
        """
        insert into dedup.jobs (job_id, document_id, revision)
        select gen_random_uuid()::text, d.document_id, d.current_revision
        from core.documents d
        where d.document_id = %s
          and exists (
            select 1 from processing.document_outputs o
            where o.document_id = d.document_id and o.revision = d.current_revision
          )
        on conflict do nothing
        """,
        (document_id,),
    )


async def reconcile_clusters(db: Database, cluster_id: str, related: set[str]) -> str:
    candidates = {value for value in related | {cluster_id} if value}
    if len(candidates) <= 1:
        return cluster_id
    rows = await db.fetch_all(
        """
        select cluster_id, created_at from dedup.story_clusters
        where cluster_id = any(%s::text[])
        """,
        (list(candidates),),
    )
    if not rows:
        return cluster_id
    target = str(min(rows, key=lambda row: (row["created_at"], row["cluster_id"]))["cluster_id"])
    for row in rows:
        other = row["cluster_id"]
        if other == target:
            continue
        await db.execute(
            "update dedup.story_members set cluster_id = %s where cluster_id = %s",
            (target, other),
        )
        await db.execute("delete from dedup.story_clusters where cluster_id = %s", (other,))
    return target


async def member_for_document(db: Database, document_id: str) -> dict[str, Any] | None:
    return await db.fetch_one(
        "select * from dedup.story_members where document_id = %s", (document_id,)
    )


async def add_member(
    db: Database,
    *,
    cluster_id: str,
    document_id: str,
    revision: int,
    similarity: float,
    relation: str,
    origin_kind: str,
) -> str | None:
    previous = await member_for_document(db, document_id)
    await db.execute(
        """
        insert into dedup.story_members
            (cluster_id, document_id, revision, similarity, relation, origin_kind)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (document_id) do update set
            cluster_id = excluded.cluster_id, revision = excluded.revision,
            similarity = excluded.similarity, relation = excluded.relation,
            origin_kind = excluded.origin_kind
        """,
        (cluster_id, document_id, revision, similarity, relation, origin_kind),
    )
    return previous["cluster_id"] if previous else None


async def cluster_members(db: Database, cluster_id: str) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select m.document_id, m.revision, m.similarity, m.relation, m.origin_kind, m.added_at,
               d.host, d.state, d.first_seen_at, r.published_at, r.title, r.language
        from dedup.story_members m
        join core.documents d on d.document_id = m.document_id
        left join core.document_revisions r
          on r.document_id = m.document_id and r.revision = m.revision
        where m.cluster_id = %s
        order by m.added_at
        """,
        (cluster_id,),
    )


async def update_cluster(db: Database, cluster_id: str) -> None:
    members = await cluster_members(db, cluster_id)
    if not members:
        await db.execute("delete from dedup.story_clusters where cluster_id = %s", (cluster_id,))
        return
    copies = {"syndicated_copy", "repeated_claim"}
    representative = min(
        members,
        key=lambda member: dedup.origin_sort_key(member["published_at"], member["first_seen_at"]),
    )
    representative_id = representative["document_id"]

    def relation(member: dict[str, Any]) -> str:
        if member["document_id"] == representative_id:
            return "original"
        return str(member["relation"])

    independent_hosts = {
        member["host"] for member in members if relation(member) not in copies and member["host"]
    }
    syndicated = sum(1 for member in members if relation(member) in copies)
    official = sum(1 for member in members if member["origin_kind"] == "official")
    confidence = max(float(member["similarity"]) for member in members)
    title = next(
        (
            member["title"]
            for member in members
            if member["document_id"] == representative["document_id"]
        ),
        "",
    )
    await db.execute(
        """
        update dedup.story_clusters
        set title = %s, representative_document_id = %s, member_count = %s,
            independent_source_count = %s, syndicated_count = %s, official_count = %s,
            confidence = %s, updated_at = now()
        where cluster_id = %s
        """,
        (
            title or "",
            representative["document_id"],
            len(members),
            len(independent_hosts),
            syndicated,
            official,
            confidence,
            cluster_id,
        ),
    )
    await db.execute(
        """
        update dedup.story_members set relation = 'original'
        where cluster_id = %s and document_id = %s and relation <> 'original'
        """,
        (cluster_id, representative_id),
    )


async def insert_lineage(
    db: Database,
    *,
    tenant_id: str,
    document_id: str,
    related_document_id: str,
    relation: str,
    confidence: float,
    evidence: dict[str, Any],
) -> None:
    if document_id == related_document_id:
        return
    await db.execute(
        """
        insert into dedup.lineage_edges
            (tenant_id, document_id, related_document_id, relation, confidence,
             evidence, model_version)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict do nothing
        """,
        (
            tenant_id,
            document_id,
            related_document_id,
            relation,
            confidence,
            _json(evidence),
            DEDUP_VERSION,
        ),
    )


async def insert_citations(db: Database, document: dict[str, Any]) -> int:
    links = (document.get("metadata") or {}).get("links") or []
    urls = []
    for link in links[:200]:
        if isinstance(link, dict) and isinstance(link.get("url"), str):
            urls.append(_normalize_url(link["url"]))
    if not urls:
        return 0
    rows = await db.fetch_all(
        """
        select document_id, canonical_url from core.documents
        where document_id <> %s and canonical_url = any(%s::text[])
        """,
        (document["document_id"], urls),
    )
    count = 0
    for row in rows:
        await insert_lineage(
            db,
            tenant_id=document["tenant_id"],
            document_id=document["document_id"],
            related_document_id=row["document_id"],
            relation="cites",
            confidence=1.0,
            evidence={"url": row["canonical_url"]},
        )
        count += 1
    return count


async def lineage_counts(db: Database, cluster_id: str) -> dict[str, int]:
    rows = await db.fetch_all(
        """
        select l.relation, count(*)::int as count
        from dedup.lineage_edges l
        join dedup.story_members m on m.document_id = l.document_id
        where m.cluster_id = %s
        group by l.relation
        """,
        (cluster_id,),
    )
    return {row["relation"]: row["count"] for row in rows}


async def cluster_detail(db: Database, cluster_id: str) -> dict[str, Any] | None:
    cluster = await db.fetch_one(
        "select * from dedup.story_clusters where cluster_id = %s", (cluster_id,)
    )
    if cluster is None:
        return None
    cluster["members"] = await cluster_members(db, cluster_id)
    cluster["lineage"] = await db.fetch_all(
        """
        select l.document_id, l.related_document_id, l.relation, l.confidence,
               l.evidence, l.created_at
        from dedup.lineage_edges l
        join dedup.story_members m on m.document_id = l.document_id
        where m.cluster_id = %s
        order by l.created_at
        """,
        (cluster_id,),
    )
    cluster["evidence"] = await evidence_for_cluster(db, cluster_id)
    return cluster


async def list_clusters(db: Database, limit: int) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select cluster_id, title, representative_document_id, member_count,
               independent_source_count, syndicated_count, official_count,
               confidence, model_version, updated_at
        from dedup.story_clusters order by updated_at desc limit %s
        """,
        (limit,),
    )


async def evidence_for_cluster(db: Database, cluster_id: str) -> dict[str, Any]:
    members = [
        dedup.Member(
            document_id=row["document_id"],
            host=row["host"],
            relation=row["relation"],
            origin_kind=row["origin_kind"],
        )
        for row in await cluster_members(db, cluster_id)
    ]
    counts = await lineage_counts(db, cluster_id)
    result = dedup.evidence_strength(members, counts)
    result["cluster_id"] = cluster_id
    return result


async def evidence_for_document(db: Database, document_id: str) -> dict[str, Any] | None:
    member = await member_for_document(db, document_id)
    if member is None:
        return None
    return await evidence_for_cluster(db, member["cluster_id"])


async def document_dedup(db: Database, document_id: str) -> dict[str, Any] | None:
    member = await member_for_document(db, document_id)
    if member is None:
        return None
    cluster = await db.fetch_one(
        "select * from dedup.story_clusters where cluster_id = %s", (member["cluster_id"],)
    )
    lineage = await db.fetch_all(
        """
        select document_id, related_document_id, relation, confidence, evidence, created_at
        from dedup.lineage_edges
        where document_id = %s or related_document_id = %s
        order by created_at
        """,
        (document_id, document_id),
    )
    return {"member": member, "cluster": cluster, "lineage": lineage}


async def insert_semantic_candidate(
    db: Database,
    *,
    tenant_id: str,
    document_id: str,
    candidate_document_id: str,
    similarity: float,
    model_version: str,
    features: dict[str, Any],
) -> str:
    candidate_id = str(ULID())
    await db.execute(
        """
        insert into dedup.semantic_candidates
            (candidate_id, tenant_id, document_id, candidate_document_id,
             similarity, model_version, features)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (document_id, candidate_document_id, model_version) do nothing
        """,
        (
            candidate_id,
            tenant_id,
            document_id,
            candidate_document_id,
            similarity,
            model_version,
            _json(features),
        ),
    )
    return candidate_id


async def list_semantic_candidates(
    db: Database, review_status: str | None, limit: int
) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        select candidate_id, document_id, candidate_document_id, similarity,
               model_version, features, decision_status, review_status, created_at
        from dedup.semantic_candidates
        where (%s::text is null or review_status = %s)
        order by created_at desc limit %s
        """,
        (review_status, review_status, limit),
    )


async def review_semantic_candidate(
    db: Database, candidate_id: str, review: str
) -> dict[str, Any] | None:
    await db.execute(
        """
        update dedup.semantic_candidates
        set review_status = %s, reviewed_at = now()
        where candidate_id = %s
        """,
        (review, candidate_id),
    )
    return await db.fetch_one(
        "select * from dedup.semantic_candidates where candidate_id = %s", (candidate_id,)
    )


async def dedup_stats(db: Database) -> dict[str, Any]:
    clusters = await db.fetch_one("select count(*)::int as count from dedup.story_clusters")
    members = await db.fetch_one("select count(*)::int as count from dedup.story_members")
    lineages = await db.fetch_one("select count(*)::int as count from dedup.lineage_edges")
    pending = await db.fetch_one(
        """
        select count(*)::int as count from dedup.semantic_candidates
        where review_status = 'pending'
        """
    )
    return {
        "clusters": clusters["count"] if clusters else 0,
        "members": members["count"] if members else 0,
        "lineage_edges": lineages["count"] if lineages else 0,
        "pending_semantic_reviews": pending["count"] if pending else 0,
    }


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str, ensure_ascii=False)


def _normalize_url(raw: str) -> str:
    parsed = urlsplit(raw)
    return urlunsplit((parsed.scheme, parsed.netloc.casefold(), parsed.path, parsed.query, ""))
