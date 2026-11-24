from __future__ import annotations

from datetime import UTC, datetime

from wre_graph_builder.kg.builder import _apply_cancellations, _supplier_and_status_edges
from wre_graph_builder.kg.model import Relationship


def _relationship(
    rel_type: str,
    src_id: str,
    dst_id: str,
    valid_from: datetime,
    *,
    valid_to: datetime | None = None,
) -> Relationship:
    return Relationship(
        relationship_type=rel_type,
        src_kind="entity",
        src_id=src_id,
        dst_kind="entity",
        dst_id=dst_id,
        valid_from=valid_from,
        valid_to=valid_to,
        observed_at=valid_from,
        source_document_id="doc",
        confidence=0.9,
        extraction_version="test",
    )


def test_relationship_shapes() -> None:
    valid = _relationship("INVESTED_IN", "a", "b", datetime(2026, 1, 1, tzinfo=UTC))
    assert valid.shape_valid()
    invalid = Relationship(
        relationship_type="INVESTED_IN",
        src_kind="source",
        src_id="s",
        dst_kind="entity",
        dst_id="e",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_document_id=None,
        confidence=0.5,
        extraction_version="test",
    )
    assert not invalid.shape_valid()


def test_cancellation_closes_prior_relationships() -> None:
    invested = _relationship("INVESTED_IN", "company", "project", datetime(2025, 1, 1, tzinfo=UTC))
    announced = _relationship("ANNOUNCED", "company", "event", datetime(2025, 1, 1, tzinfo=UTC))
    cancelled = Relationship(
        relationship_type="CANCELLED",
        src_kind="entity",
        src_id="company",
        dst_kind="document",
        dst_id="doc",
        valid_from=datetime(2026, 6, 1, tzinfo=UTC),
        observed_at=datetime(2026, 11, 1, tzinfo=UTC),
        source_document_id="doc",
        confidence=0.55,
        extraction_version="test",
    )
    result = _apply_cancellations([invested, announced, cancelled])
    assert result[0].valid_to == datetime(2026, 6, 1, tzinfo=UTC)
    assert result[1].valid_to == datetime(2026, 6, 1, tzinfo=UTC)
    assert result[2].valid_to is None


def test_supplier_and_status_edges_from_sentences() -> None:
    body = (
        "Tata Electronics and Bharat Electronics announced a supplier partnership. "
        "Gujarat project was delayed in 2025 and later cancelled."
    )
    document = {
        "document_id": "doc-1",
        "body": body,
        "published_at": datetime(2026, 1, 1, tzinfo=UTC),
        "first_seen_at": datetime(2026, 1, 2, tzinfo=UTC),
    }
    starts = {
        "tata": body.index("Tata Electronics"),
        "bharat": body.index("Bharat Electronics"),
        "project": body.index("Gujarat project"),
    }
    mentions = [
        {
            "entity_id": "org-tata",
            "entity_type": "organization",
            "char_start": starts["tata"],
            "char_end": starts["tata"] + len("Tata Electronics"),
        },
        {
            "entity_id": "org-bharat",
            "entity_type": "organization",
            "char_start": starts["bharat"],
            "char_end": starts["bharat"] + len("Bharat Electronics"),
        },
        {
            "entity_id": "project-1",
            "entity_type": "project",
            "char_start": starts["project"],
            "char_end": starts["project"] + len("Gujarat project"),
        },
    ]
    edges = _supplier_and_status_edges([document], {"doc-1": mentions})
    types = [edge.relationship_type for edge in edges]
    assert "SUPPLIES" in types
    assert "DELAYED" in types
    assert "CANCELLED" in types
    assert {edge.src_id for edge in edges} >= {"org-tata", "project-1"}
