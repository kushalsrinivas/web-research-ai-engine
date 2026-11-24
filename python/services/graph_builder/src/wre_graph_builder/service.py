"""Confidence-aware entity resolution."""

from __future__ import annotations

import logging
from typing import Any

from wre_pipeline.db import Database

from . import repository, resolver
from .metrics import ENTITY_RESOLUTIONS, ENTITY_REVIEWS

ENTITY_TYPES = {
    "person",
    "organization",
    "government",
    "location",
    "product",
    "industry",
    "technology",
    "financial_instrument",
    "project",
    "policy",
    "event",
    "work",
    "concept",
    "other",
}


class EntityService:
    def __init__(self, db: Database, logger: logging.Logger) -> None:
        self.db = db
        self.logger = logger

    async def resolve(
        self,
        tenant_id: str,
        data: resolver.EntityInput,
        *,
        create_missing: bool = True,
    ) -> dict[str, Any]:
        candidates = await repository.find_entities(self.db, tenant_id, data)
        scored: list[tuple[float, dict[str, Any], resolver.MatchCandidate]] = []
        for candidate in candidates:
            score, features = resolver.score_candidate(data, candidate)
            scored.append((score, features, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0] if scored else None
        decision = resolver.decide(best[0], best[1]) if best else "keep_separate"

        top = [
            {
                "entity_id": candidate.entity_id,
                "canonical_name": candidate.canonical_name,
                "entity_type": candidate.entity_type,
                "score": score,
                "features": features,
            }
            for score, features, candidate in scored[:3]
        ]

        if best and decision == "auto_match":
            candidate = best[2]
            await repository.add_alias(
                self.db, candidate.entity_id, data.name, data.entity_type or candidate.entity_type
            )
            candidate_id = await repository.record_candidate(
                self.db,
                tenant_id=tenant_id,
                data=data,
                candidate_entity_id=candidate.entity_id,
                score=best[0],
                features=best[1],
                decision="auto_match",
                review_status="not_required",
                resolved_entity_id=candidate.entity_id,
            )
            ENTITY_RESOLUTIONS.labels(decision="auto_match").inc()
            entity = await repository.get_entity(self.db, candidate.entity_id)
            return {
                "decision": "auto_match",
                "candidate_id": candidate_id,
                "entity": entity,
                "score": best[0],
                "features": best[1],
                "candidates": top,
            }

        if best and decision == "review":
            candidate_id = await repository.record_candidate(
                self.db,
                tenant_id=tenant_id,
                data=data,
                candidate_entity_id=best[2].entity_id,
                score=best[0],
                features=best[1],
                decision="review",
                review_status="pending",
                resolved_entity_id=None,
            )
            ENTITY_RESOLUTIONS.labels(decision="review").inc()
            return {
                "decision": "review",
                "candidate_id": candidate_id,
                "entity": None,
                "score": best[0],
                "features": best[1],
                "candidates": top,
            }

        entity = None
        if create_missing:
            entity = await repository.create_entity(self.db, tenant_id, data, confidence=0.5)
        candidate_id = await repository.record_candidate(
            self.db,
            tenant_id=tenant_id,
            data=data,
            candidate_entity_id=None,
            score=best[0] if best else 0.0,
            features=best[1] if best else {},
            decision="keep_separate",
            review_status="not_required",
            resolved_entity_id=entity["entity_id"] if entity else None,
        )
        ENTITY_RESOLUTIONS.labels(decision="keep_separate").inc()
        return {
            "decision": "keep_separate",
            "candidate_id": candidate_id,
            "entity": entity,
            "score": best[0] if best else 0.0,
            "features": best[1] if best else {},
            "candidates": top,
        }

    async def review(self, candidate_id: str, review: str) -> dict[str, Any] | None:
        result = await repository.review_candidate(self.db, candidate_id, review)
        if result is None:
            return None
        ENTITY_REVIEWS.labels(status=review).inc()
        return result
