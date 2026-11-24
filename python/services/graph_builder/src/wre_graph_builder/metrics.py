"""Dedup and entity-resolution metrics."""

from prometheus_client import Counter, Gauge, Histogram

DEDUP_RESULTS = Counter(
    "wre_dedup_jobs_total",
    "Dedup jobs finished, by outcome.",
    ["status"],
)
DEDUP_INFLIGHT = Gauge(
    "wre_dedup_inflight",
    "Documents currently being clustered.",
)
DEDUP_CLUSTERS = Gauge(
    "wre_dedup_clusters",
    "Story clusters currently stored.",
)
DEDUP_QUEUE_DEPTH = Gauge(
    "wre_dedup_queue_depth",
    "Pending dedup jobs.",
)
ENTITY_RESOLUTIONS = Counter(
    "wre_entity_resolutions_total",
    "Entity resolution decisions.",
    ["decision"],
)
ENTITY_REVIEWS = Counter(
    "wre_entity_candidate_reviews_total",
    "Entity candidate reviews.",
    ["status"],
)
DEDUP_DURATION = Histogram(
    "wre_dedup_duration_seconds",
    "Dedup job duration.",
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
)

EXTRACTION_JOBS = Counter(
    "wre_extraction_jobs_total",
    "Extraction jobs finished, by outcome.",
    ["status"],
)
EXTRACTION_INFLIGHT = Gauge(
    "wre_extraction_inflight",
    "Documents currently being extracted.",
)
EXTRACTION_QUARANTINED = Counter(
    "wre_extraction_quarantined_total",
    "Extraction objects quarantined.",
    ["object_type"],
)
EXTRACTION_DURATION = Histogram(
    "wre_extraction_duration_seconds",
    "Document extraction duration.",
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
)

KG_RELATIONSHIPS = Gauge(
    "wre_kg_relationships",
    "Relationships in the active graph projection.",
)
KG_REBUILDS = Counter(
    "wre_kg_rebuilds_total",
    "Graph projection rebuilds, by outcome.",
    ["status"],
)
KG_BUILD_SECONDS = Histogram(
    "wre_kg_build_seconds",
    "Graph projection build duration.",
    buckets=[0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
)
