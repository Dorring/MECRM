# CQRS + Transactional Outbox (Phase 3)

## Goal

Implement true CQRS with enterprise-grade reliability:

- **Write-side** uses command handlers that append to an **event store** with optimistic concurrency.
- **Transactional outbox** guarantees reliable publishing to Kafka (no lost events on partial failure).
- **Read-side** is a set of **projections** that asynchronously build query-optimized read models.
- **Consumers are idempotent** and safe under retries/duplicates.
- **Compatibility is enforced** via repo-stored schemas and contract tests in CI.
- **Consistency lag is measurable** and meets dev target (median ≤ 3s, p95 ≤ 10s under load).

Phase 3 introduces breaking changes by separating **command/write** and **query/read** paths with eventual consistency.

## Why CQRS Here

- **Read scaling**: projections produce denormalized tables optimized for UI queries without impacting write throughput.
- **Auditability**: the event store is an append-only history for governance and debugging.
- **Resilience**: outbox ensures events are not lost when DB commits succeed but Kafka publish fails.
- **Rebuildability**: projections can be rebuilt from the event store when needed.

## Event Model (Canonical)

Write-side events are stored with:

- `tenant_id` (uuid)
- `stream_id` (text; `${aggregate_type}:${aggregate_id}`)
- `version` (int; monotonically increasing per stream)
- `event_id` (uuid)
- `event_type` (text; e.g. `lead.created`)
- `schema_version` (int)
- `payload` (jsonb)
- `created_at` (timestamptz)
- `idempotency_key` (text; optional unique)

Kafka messages use a CloudEvents-like envelope consistent with existing gateway conventions and include `tenantid`.

## Command vs Query Separation Rules

Non-negotiable invariants for “true CQRS”:

- Write handlers **never** write to read model tables.
- Read endpoints **never** read from the write-side event store tables.
- Read models are updated **only** by projection workers consuming Kafka events.

Legacy endpoints may exist during rollout for fallback, but the CQRS path is demonstrated and verified via dedicated command/query endpoints.

## Outbox Flow (Diagram)

```
Command Handler
  |
  v
DB Transaction (single commit)
  - event_streams optimistic version check/update
  - events append (event store)
  - outbox_events append (pending publishes)
  |
  v
Outbox Publisher (poll loop)
  - lock rows (FOR UPDATE SKIP LOCKED)
  - publish to Kafka (at-least-once)
  - mark published_at OR retry with backoff OR dead-letter
  |
  v
Kafka Topics (consolidated streams)
  |
  v
Projector (consumer)
  - validate schema version
  - dedupe by (tenant_id, event_id)
  - apply projection updates
  |
  v
Read Models (denormalized tables)
```

## Exactly-once Strategy (Practical)

Phase 3 approaches exactly-once semantics via:

- **At-least-once delivery** from outbox publisher.
- **Idempotent consumption** on the read side using a `processed_events` dedupe table keyed by `(tenant_id, event_id)`.

This handles duplicates, retries, and consumer restarts safely without relying on “magic” exactly-once guarantees.

## Migration Strategy (Safe Rollout)

### Phase A — Shadow Projections

1. Create event store/outbox/read model tables and projection workers.
2. Start projection workers consuming Kafka and populating read models.
3. Keep reads on existing tables.

Outcome: projections are proven without changing the user experience.

### Phase B — Switch Reads to Read Models

1. Introduce query endpoints that read only from read models.
2. Switch frontend to query endpoints behind a feature flag/config.

Outcome: production reads move to read models with low risk (roll back by switching back).

### Phase C — Switch Writes to Commands + Outbox

1. Introduce command endpoints that write only to event store/outbox.
2. Switch frontend write operations to command endpoints.
3. Deprecate legacy direct writes.

Outcome: write-side reliability is guaranteed by the outbox pattern.

## Rollback Plan

If issues arise during rollout:

- Disable projection workers and outbox publisher.
- Switch frontend and gateway routing back to legacy endpoints.
- Keep event store/outbox tables intact for postmortem and later retry.

Rollback does not require destructive schema changes.

## Measurable Consistency Lag

Projection lag is measured as:

- `read_model.updated_at - event.created_at`

Targets (dev):

- median ≤ 3s
- p95 ≤ 10s

Reports are generated under `reports/cqrs/`.

