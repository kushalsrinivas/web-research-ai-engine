"""Temporal graph relationship model and shape rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

RELATIONSHIP_TYPES = {
    "WORKS_FOR",
    "OWNS",
    "INVESTED_IN",
    "LOCATED_IN",
    "ANNOUNCED",
    "APPROVED",
    "PARTNERED_WITH",
    "COMPETES_WITH",
    "REGULATES",
    "PRODUCES",
    "USES_TECHNOLOGY",
    "MENTIONED_IN",
    "SUPPORTS",
    "CONTRADICTS",
    "DERIVED_FROM",
    "RELATED_TO",
    "SUPPLIES",
    "DELAYED",
    "CANCELLED",
}

# src_kind -> allowed dst kinds. A relationship type may further constrain this.
SHAPES: dict[str, set[str]] = {
    "entity": {"entity", "document", "claim", "event"},
    "document": {"entity", "document", "claim", "event", "source"},
    "claim": {"entity", "document"},
    "event": {"entity", "document", "claim"},
    "source": {"document"},
}


@dataclass(slots=True)
class Relationship:
    relationship_type: str
    src_kind: str
    src_id: str
    dst_kind: str
    dst_id: str
    valid_from: datetime
    observed_at: datetime
    source_document_id: str | None
    confidence: float
    extraction_version: str
    valid_to: datetime | None = None
    source_claim_id: str | None = None
    source_event_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def shape_valid(self) -> bool:
        return (
            self.relationship_type in RELATIONSHIP_TYPES
            and self.dst_kind in SHAPES.get(self.src_kind, set())
            and not (self.src_kind == self.dst_kind and self.src_id == self.dst_id)
        )
