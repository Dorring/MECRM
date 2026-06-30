from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

cache_hit_total = Counter("cache_hit_total", "Total cache hits", labelnames=("tenant", "cache"))
cache_miss_total = Counter("cache_miss_total", "Total cache misses", labelnames=("tenant", "cache"))
cache_invalidation_total = Counter("cache_invalidation_total", "Total cache invalidations", labelnames=("tenant", "reason"))
cache_fail_closed_total = Counter("cache_fail_closed_total", "Total fail-closed security events", labelnames=("component", "reason"))
auth_recheck_latency_ms = Histogram(
    "auth_recheck_latency_ms",
    "Authorization recheck latency on cache miss in milliseconds",
    labelnames=("component",),
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500),
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
