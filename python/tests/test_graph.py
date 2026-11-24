from __future__ import annotations

from datetime import UTC, datetime

from wre_graph_builder import dedup, resolver


def test_entity_name_normalization_collapses_legal_forms() -> None:
    variants = [
        "Tata Electronics",
        "Tata Electronics Private Limited",
        "Tata Electronics Pvt Ltd",
        "TATA ELECTRONICS PVT. LTD.",
    ]
    keys = {resolver.normalize_name(name, "organization") for name in variants}
    assert keys == {"tata electronics"}
    assert resolver.normalize_name("Tata Electronics Ltd.", "person") == "tata electronics ltd"


def test_exact_name_and_type_auto_matches() -> None:
    candidate = resolver.MatchCandidate(
        entity_id="e1",
        canonical_name="Tata Electronics",
        normalized_name="tata electronics",
        alias_keys=["tata electronics"],
        external_ids={"domain": "tata.com"},
        country="IN",
        entity_type="organization",
        confidence=0.9,
    )
    score, features = resolver.score_candidate(
        resolver.EntityInput(
            name="Tata Electronics Private Limited",
            entity_type="organization",
            domain="tata.com",
        ),
        candidate,
    )
    assert score >= 0.95
    assert features["exact_name"] is True
    assert resolver.decide(score, features) == "auto_match"


def test_fuzzy_name_alone_never_auto_matches() -> None:
    candidate = resolver.MatchCandidate(
        entity_id="e2",
        canonical_name="Tata Motors",
        normalized_name="tata motors",
        alias_keys=["tata motors"],
        external_ids={},
        country=None,
        entity_type="organization",
        confidence=0.7,
    )
    score, features = resolver.score_candidate(
        resolver.EntityInput(name="Tata Motor", entity_type="organization"), candidate
    )
    assert score < 0.95
    assert features["corroborated"] is False
    assert resolver.decide(score, features) in {"review", "keep_separate"}


def test_type_mismatch_blocks_automatic_merge() -> None:
    candidate = resolver.MatchCandidate(
        entity_id="e3",
        canonical_name="Tata Electronics",
        normalized_name="tata electronics",
        alias_keys=["tata electronics"],
        external_ids={},
        country=None,
        entity_type="organization",
        confidence=0.9,
    )
    score, features = resolver.score_candidate(
        resolver.EntityInput(name="Tata Electronics", entity_type="person"), candidate
    )
    assert score >= 0.95
    assert resolver.decide(score, features) == "review"


def test_shingles_and_similarity() -> None:
    text = "The company announced a semiconductor investment in Gujarat this week."
    same = dedup.jaccard(dedup.shingles(text), dedup.shingles(text))
    other = dedup.jaccard(
        dedup.shingles(text), dedup.shingles("Completely different weather report about rain.")
    )
    assert same == 1.0
    assert other == 0.0
    assert dedup.hamming(0b1010, 0b0011) == 2


def test_near_duplicate_relation_and_official_origin() -> None:
    assert dedup.classify(0.9, 0.1, 2, same_host=False, official=False) == (
        "syndicated_copy",
        "media",
        0.9,
    )
    assert dedup.classify(0.9, 0.1, 2, same_host=True, official=False) == (
        "repeated_claim",
        "media",
        0.9,
    )
    assert dedup.classify(0.2, 0.7, 30, same_host=False, official=False) == (
        "independent",
        "media",
        0.2,
    )
    assert dedup.official_origin("pib.gov.in", "", "") is True
    assert dedup.official_origin("example.com", "Press release", "") is True
    assert dedup.official_origin("example.com", "Analysis", "Body") is False


def test_evidence_does_not_count_syndicated_copies() -> None:
    members = [
        dedup.Member("d1", "origin.example", "original", "official"),
        dedup.Member("d2", "copy1.example", "syndicated_copy", "media"),
        dedup.Member("d3", "copy2.example", "syndicated_copy", "media"),
        dedup.Member("d4", "copy3.example", "repeated_claim", "media"),
        dedup.Member("d5", "independent.example", "independent", "media"),
    ]
    result = dedup.evidence_strength(members, {"cites": 2})
    assert result["document_count"] == 5
    assert result["independent_sources"] == 2
    assert result["syndicated_copies"] == 3
    assert result["effective_sources"] == 2
    assert result["inflation_ratio"] == 2.5
    assert result["evidence_score"] < 1.0
    assert result["lineage"] == {"cites": 2}


def test_origin_sort_prefers_publication_time() -> None:
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    later = datetime(2026, 2, 1, tzinfo=UTC)
    assert dedup.origin_sort_key(earlier, later) < dedup.origin_sort_key(later, earlier)
