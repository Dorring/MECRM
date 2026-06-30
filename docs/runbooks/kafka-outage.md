# Runbook: Kafka Outage

## Symptoms

- Agents and gateway stop processing events.
- Kafka publish errors in logs.
- Increasing consumer lag (if exporter enabled).

## Checks

- Service health:
  - Kafka container / pod status.
  - `up{job="kafka"}` in Prometheus.
- App signals:
  - `rate(kafka_messages_published_total[5m])` drops to ~0 while traffic exists.
  - `circuit_breaker_state{dependency=~".*kafka.*"} == 1` (OPEN).

## Mitigation

- If local/dev compose:
  - Restart Kafka service only (do not restart Postgres unless necessary).
- If Kubernetes:
  - Verify broker quorum, disk pressure, and controller health.
  - Restart unhealthy brokers one at a time.

## Rollback / Safety

- Do not delete topics to “fix” lag.
- Do not purge Postgres event tables.
- If Kafka cannot be recovered, keep serving read paths and rely on Postgres durable event history for rebuild once Kafka is back.
