# Phase 5: Chaos & Reliability Testing

## Goal
Prove—using evidence—that the Multi-Agent Enterprise CRM degrades gracefully, recovers automatically, and preserves correctness under realistic distributed-system failures.

## Non-negotiable Safety Rules
- Chaos runs only in isolated environments: local docker-compose, dedicated CI job, or isolated staging.
- Chaos is disabled by default and must be explicitly enabled via environment flags.
- Blast radius is controlled: one failure at a time; restore to healthy before proceeding.
- No destructive operations on prod-like data; use dedicated tenant ids and test topics/tables.
- If a failure is not visible in metrics/logs, it does not count.

## System Dependency Graph (Simplified)

### Data plane
- Write path:
  - Core Services (FastAPI write API) → PostgreSQL (event_store + outbox) → Outbox Publisher → Kafka
- Read path:
  - Replay Service (Kafka consumer) → PostgreSQL (event_log + read models) → Gateway APIs → Frontend
- Agents path:
  - Agents (Kafka consumers + tools) ↔ Redis (kill switch + approvals pending) ↔ OPA (policy decisions) → Kafka (events)

### Control plane
- Governance UI → Gateway → Redis (kill switch commands) → Agents (pub/sub propagation)
- AuthN/AuthZ:
  - Frontend/Gateway ↔ Keycloak (OIDC) and OPA (ABAC/RBAC policy)

## Failure Modes and Expected Behavior

### Kafka broker down
**Failure injection**
- Stop Kafka container.

**Expected behavior**
- Producers:
  - circuit breaker transitions CLOSED → OPEN after bounded failures
  - retries are bounded (max retries + max time)
  - outbox table retains unsent messages (published_at remains NULL)
- Consumers:
  - no crash loops; consumer backoff is bounded
  - recovery after Kafka restart without message loss

**Correctness assertions**
- No lost messages: outbox row remains until successfully published.
- No incorrect duplicates: consumers must be idempotent by event_id and monotonic version.

### Consumer crash mid-processing
**Failure injection**
- Kill consumer process/container during message processing.

**Expected behavior**
- On restart, consumer reprocesses from last committed offset.
- Idempotency:
  - event_id uniqueness prevents duplicates
  - projection only applies versions strictly greater than current

**Correctness assertions**
- Exactly-once effect at the projection level (idempotent writes).
- No duplicate projections; final state equals the “ideal” ordered, deduplicated event stream.

### Duplicate messages
**Failure injection**
- Produce the same event multiple times (same event_id).

**Expected behavior**
- Deduplication at storage layer (unique constraint on event_id) OR in consumer logic.
- Offsets still advance; duplicates do not modify state.

**Correctness assertions**
- Projection state unchanged compared to single delivery.

### Out-of-order events
**Failure injection**
- Produce events with versions out of order (e.g., v3 then v2).

**Expected behavior**
- Projection applies only if version is newer than current (monotonic guard).
- Older events are ignored (or parked for later, if implemented).

**Correctness assertions**
- Final projected state corresponds to highest version.

### Redis unavailable
**Failure injection**
- Stop Redis container.

**Expected behavior**
- Kill switch / governance must fail closed:
  - if Redis cannot be reached, agents treat state as blocked (no side effects)
- Circuit breaker opens for Redis dependency and prevents cascading failures.

**Correctness assertions**
- No actions executed while Redis is down.
- On Redis recovery, agents resume with bounded recovery.

### DB slow / transient failure
**Failure injection**
- Stop PostgreSQL container briefly (transient outage), then restart.

**Expected behavior**
- Retry policy performs bounded retries with exponential backoff + jitter.
- Circuit breaker opens after threshold, then transitions to HALF_OPEN after recovery timeout.
- Automatic recovery:
  - successful probe closes breaker and resumes processing.

**Correctness assertions**
- No corruption: partial writes are rolled back.
- Recovery within RTO target.

## Circuit Breaker Semantics
- States:
  - CLOSED: normal operation, count failures
  - OPEN: short-circuit, do not attempt dependency calls
  - HALF_OPEN: allow limited probe calls to test recovery
- Configuration:
  - failure_threshold: number of consecutive failures to open
  - recovery_timeout_seconds: time spent open before probing
  - half_open_max_calls: max concurrent probe calls
- Tenant-aware option:
  - separate breaker per tenant_id to limit blast radius

## Retry Policy Semantics
- Exponential backoff with jitter.
- Max attempts and max elapsed time (bounded).
- Retry classification:
  - retryable: transient network, timeout, connection reset, broker unavailable
  - fatal: validation errors, authorization errors, schema errors
- Observability:
  - retry_attempts_total / retry_failures_total counters
  - per-operation labels (dependency + operation)

## SLO/SLA Targets (Phase 5)
- Availability (core workflows): 99.9% in healthy infra; graceful degradation under dependency loss.
- Max retry time (per dependency call): 30s (bounded), no infinite loops.
- Recovery time objectives (RTO):
  - Kafka restart recovery: ≤ 60s
  - Redis restart recovery: ≤ 30s
  - Postgres transient restart recovery: ≤ 60s
- Error budget measurement:
  - recovery_time_seconds histogram and summary in reports/chaos/*.json

## Blast Radius Control
- Chaos tests enforce a single fault at a time.
- Chaos tests use dedicated:
  - Kafka topic prefix: `chaos.*`
  - PostgreSQL tables: `chaos_*`
  - Tenant ids: generated UUIDs not used elsewhere

## Environment Gating (Never Production)
- Chaos test suite requires:
  - `CHAOS_TESTS_ENABLED=true`
  - `CHAOS_ENVIRONMENT in {local,ci,staging}`
- CI workflow runs only on:
  - manual trigger (workflow_dispatch)
  - nightly schedule
- Chaos tests must not run on production branches/environments without explicit manual trigger.

