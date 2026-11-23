"""ETL Prometheus metrics (served on the shared /metrics endpoint)."""

from prometheus_client import Counter, Gauge, Histogram

ETL_RESULTS = Counter(
    "wre_etl_jobs_total",
    "ETL jobs finished, by outcome.",
    ["status"],
)
ETL_QUALITY = Histogram(
    "wre_etl_quality_score",
    "Canonical document quality score.",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
ETL_DURATION = Histogram(
    "wre_etl_duration_seconds",
    "Document processing duration.",
    ["kind"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
)
ETL_INFLIGHT = Gauge(
    "wre_etl_inflight",
    "Documents currently being processed.",
)
ETL_QUEUE_DEPTH = Gauge(
    "wre_etl_queue_depth",
    "Pending ETL jobs.",
)
