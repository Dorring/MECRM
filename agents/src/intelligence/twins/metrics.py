"""
Prometheus metrics for Digital Twins module.

Tracks twin building, simulation requests, and performance.
"""
from prometheus_client import Counter, Histogram, Gauge

# Twin Build Metrics
twin_build_total = Counter(
    "crm_twin_build_total",
    "Total number of twin profile builds",
    ["tenant_id", "status"],
)

twin_build_latency = Histogram(
    "crm_twin_build_latency_seconds",
    "Time to build a twin profile",
    ["tenant_id"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

twin_cache_hits = Counter(
    "crm_twin_cache_hits_total",
    "Number of twin profile cache hits",
    ["tenant_id"],
)

twin_cache_misses = Counter(
    "crm_twin_cache_misses_total",
    "Number of twin profile cache misses",
    ["tenant_id"],
)

# Simulation Metrics
simulation_requests_total = Counter(
    "crm_simulation_requests_total",
    "Total number of simulation requests",
    ["tenant_id", "scenario", "status"],
)

simulation_latency = Histogram(
    "crm_simulation_latency_seconds",
    "Time to run a simulation",
    ["tenant_id", "scenario"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

simulation_confidence = Histogram(
    "crm_simulation_confidence",
    "Distribution of simulation confidence scores",
    ["scenario"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# Twin Profile Metrics
twin_profiles_active = Gauge(
    "crm_twin_profiles_active",
    "Number of active twin profiles per tenant",
    ["tenant_id"],
)

twin_profile_age_seconds = Histogram(
    "crm_twin_profile_age_seconds",
    "Age of twin profiles when used for simulation",
    ["tenant_id"],
    buckets=(60, 300, 900, 3600, 86400, 604800),  # 1m, 5m, 15m, 1h, 1d, 1w
)
