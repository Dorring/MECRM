# Runbook: Replay / Policy Failure

## Symptoms

- Replay jobs fail or do not progress.
- Requests are denied due to policy engine failures.
- Alert: `PolicyEngineUnavailable` or replay endpoint errors.

## Checks

- OPA health:
  - `up{job="opa"}` and OPA `/metrics`.
  - Gateway fail-closed signal: `increase(cache_fail_closed_total{reason="opa_unavailable"}[5m])`.
- Replay service health:
  - `up{job="replay-service"}`.
- Postgres health:
  - database connectivity and error logs.

## Mitigation

- If OPA is down/unreachable:
  - Restore OPA connectivity first; system is designed to fail closed.
  - Validate policy bundle mount and container readiness.
- If replay is failing:
  - Restart replay service; verify `event_log` and `aggregate_snapshots` exist.
  - If snapshots are missing, run full replay; expect higher latency but deterministic results.

## Rollback / Safety

- Do not disable OPA checks for convenience.
- Do not modify event history tables manually.
