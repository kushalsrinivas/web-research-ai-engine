"""Dedup worker: near-duplicate clustering and lineage derivation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from wre_pipeline.db import Database

from . import dedup, queue, repository
from .config import GraphConfig
from .metrics import DEDUP_INFLIGHT, DEDUP_RESULTS


class DedupWorker:
    def __init__(self, db: Database, config: GraphConfig, logger: logging.Logger) -> None:
        self.db = db
        self.config = config
        self.logger = logger

    async def process(self, job: queue.Job) -> None:
        DEDUP_INFLIGHT.inc()
        started = time.monotonic()
        try:
            document = await repository.load_document(self.db, job.document_id, job.revision)
            if document is None:
                await queue.finish(self.db, job)
                DEDUP_RESULTS.labels(status="skipped").inc()
                return
            existing = await repository.member_for_document(self.db, job.document_id)
            if existing and existing["revision"] == job.revision:
                await queue.finish(self.db, job)
                DEDUP_RESULTS.labels(status="skipped").inc()
                return

            candidates = await repository.near_candidates(
                self.db, document, self.config.simhash_distance, self.config.title_floor
            )
            body_shingles = await asyncio.to_thread(dedup.shingles, document["body"])
            current_official = dedup.official_origin(
                document["host"], document["title"], document["body"]
            )
            best, related_clusters = await self._best_candidate(
                document, candidates, body_shingles, current_official
            )

            moved_from = None
            if best is None:
                cluster_id = await repository.create_cluster(
                    self.db,
                    document["tenant_id"],
                    document["title"],
                    document["document_id"],
                    1.0,
                )
                moved_from = await repository.add_member(
                    self.db,
                    cluster_id=cluster_id,
                    document_id=document["document_id"],
                    revision=job.revision,
                    similarity=1.0,
                    relation="original",
                    origin_kind="official" if current_official else "media",
                )
            else:
                candidate, relation, origin, similarity = best
                cluster_id = candidate.cluster_id or await self._seed_candidate(candidate)
                moved_from = await repository.add_member(
                    self.db,
                    cluster_id=cluster_id,
                    document_id=document["document_id"],
                    revision=job.revision,
                    similarity=similarity,
                    relation=relation,
                    origin_kind=origin,
                )
                evidence = {
                    "body_similarity": similarity,
                    "title_similarity": round(
                        dedup.title_similarity(document["title"], candidate.title), 4
                    ),
                    "simhash_distance": dedup.hamming(document["simhash"], candidate.simhash),
                }
                await repository.insert_lineage(
                    self.db,
                    tenant_id=document["tenant_id"],
                    document_id=document["document_id"],
                    related_document_id=candidate.document_id,
                    relation=relation,
                    confidence=similarity,
                    evidence=evidence,
                )
                if current_official:
                    await repository.insert_lineage(
                        self.db,
                        tenant_id=document["tenant_id"],
                        document_id=document["document_id"],
                        related_document_id=candidate.document_id,
                        relation="official_statement",
                        confidence=1.0,
                        evidence={"origin": "official"},
                    )

            cluster_id = await repository.reconcile_clusters(self.db, cluster_id, related_clusters)
            await repository.insert_citations(self.db, document)
            await repository.update_cluster(self.db, cluster_id)
            if moved_from and moved_from != cluster_id:
                await repository.update_cluster(self.db, moved_from)
            await queue.finish(self.db, job)
            DEDUP_RESULTS.labels(status="clustered").inc()
            self.logger.info(
                "document clustered",
                extra={
                    "job_id": job.job_id,
                    "document_id": document["document_id"],
                    "cluster_id": cluster_id,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                },
            )
        except Exception as exc:
            status = await queue.retry_or_dead_letter(self.db, job, str(exc))
            DEDUP_RESULTS.labels(status=status).inc()
            self.logger.warning(
                "dedup job failed",
                extra={"job_id": job.job_id, "status": status, "error": str(exc)},
            )
        finally:
            DEDUP_INFLIGHT.dec()

    async def _seed_candidate(self, candidate: dedup.Candidate) -> str:
        cluster_id = await repository.create_cluster(
            self.db,
            tenant_id=candidate.tenant_id,
            title=candidate.title,
            representative=candidate.document_id,
            confidence=1.0,
        )
        await repository.add_member(
            self.db,
            cluster_id=cluster_id,
            document_id=candidate.document_id,
            revision=candidate.revision,
            similarity=1.0,
            relation="original",
            origin_kind=(
                "official"
                if dedup.official_origin(candidate.host, candidate.title, candidate.body)
                else "media"
            ),
        )
        return cluster_id

    async def _best_candidate(
        self,
        document: dict[str, Any],
        candidates: list[dedup.Candidate],
        body_shingles: set[int],
        current_official: bool,
    ) -> tuple[tuple[dedup.Candidate, str, str, float] | None, set[str]]:
        best: tuple[tuple[int, float, float], dedup.Candidate, str, str, float] | None = None
        related_clusters: set[str] = set()
        for candidate in candidates:
            if candidate.content_hash == document["content_hash"]:
                body_score = 1.0
            else:
                body_score = dedup.jaccard(
                    body_shingles, await asyncio.to_thread(dedup.shingles, candidate.body)
                )
            title_score = dedup.title_similarity(document["title"], candidate.title)
            distance = dedup.hamming(document["simhash"], candidate.simhash)
            classified = dedup.classify(
                body_score,
                title_score,
                distance,
                candidate.host == document["host"],
                current_official,
            )
            if classified is None:
                continue
            relation, origin, similarity = classified
            if candidate.cluster_id:
                related_clusters.add(candidate.cluster_id)
            near = 1 if relation in {"syndicated_copy", "repeated_claim"} else 0
            rank = (near, similarity, title_score)
            if best is None or rank > best[0]:
                best = (rank, candidate, relation, origin, similarity)
        if best is None:
            return None, related_clusters
        return (best[1], best[2], best[3], best[4]), related_clusters
