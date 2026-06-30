from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest


replay_failures = Counter(
    "replay_failures",
    "Replay service failures",
    labelnames=("component", "error_type"),
)

consumer_lag = Gauge(
    "consumer_lag",
    "Kafka consumer lag (end_offset - current_offset)",
    labelnames=("group_id", "topic", "partition"),
)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
