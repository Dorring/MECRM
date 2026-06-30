from prometheus_client import Counter, Histogram

backup_duration_seconds = Histogram(
    "backup_duration_seconds",
    "Duration of backup operations in seconds",
    labelnames=("type",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 60, 120, 300, 600, 1800),
)
restore_duration_seconds = Histogram(
    "restore_duration_seconds",
    "Duration of restore operations in seconds",
    labelnames=("type",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 60, 120, 300, 600, 1800),
)
rebuild_duration_seconds = Histogram(
    "rebuild_duration_seconds",
    "Duration of rebuild operations in seconds",
    labelnames=("model",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 60, 120, 300, 600, 1800),
)

recovery_success_total = Counter("recovery_success_total", "Total successful recoveries", labelnames=("scenario",))
recovery_failure_total = Counter("recovery_failure_total", "Total failed recoveries", labelnames=("scenario",))
