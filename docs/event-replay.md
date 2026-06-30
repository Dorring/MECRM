# Event Replay & Time-Travel Debugging (Phase 2)

## Goal

Enable deterministic replay of tenant-scoped aggregates from Kafka offsets/timestamps, accelerate rebuilds with snapshots, and provide an interactive UI timeline that proves replay works (scrub + diffs).

Phase 2 constraints:

- Kafka remains the source of truth for replay.
- No event store / CQRS / outbox.
- Replay never publishes new Kafka events.
- Given the same ordered event sequence, the projector produces the same state.

## Domain Event (Canonical Schema)

In Phase 2, a “Domain Event” is a normalized record describing a single aggregate change:

- event_id (uuid)
- tenant_id (uuid)
- aggregate_type (text, e.g. `lead`, `ticket`)
- aggregate_id (uuid)
- event_type (text, e.g. `lead.created`, `ticket.updated`)
- payload (json)
- version (int; monotonically increasing per aggregate)
- ts (timestamptz; event time)

Kafka messages in this repo use a CloudEvents-like envelope (`specversion`, `type`, `id`, `time`, `tenantid`, `data`). Phase 2 introduces a mapping that produces the canonical schema for persistence, replay, and UI.

## Kafka Topic Conventions

Phase 2 uses consolidated event streams per aggregate type:

- `crm.leads.events`
- `crm.tickets.events`

Existing per-action topics (e.g. `crm.leads.created`) remain for compatibility, but replay/timeline use the consolidated topics.

### Ordering Guarantees

Kafka keying rule:

- Key messages by `aggregate_id` (or `${tenant_id}:${aggregate_id}`).

This ensures total ordering for a given aggregate within a partition. Replay logic assumes dev topics use a single partition; if multiple partitions are used, the aggregate’s partition must be computed consistently from the key.

## Replay Semantics

### Replay From Offset

“Replay from offset X” means:

1. Consume topic partition starting at offset X.
2. Filter events by `(tenant_id, aggregate_type, aggregate_id)`.
3. Apply events in Kafka offset order using the projector.
4. Stop at end offset or at a supplied time/version boundary.

### Replay To Timestamp

“Replay to timestamp T” means:

1. Consume from the chosen start offset.
2. Apply only events where `event.ts <= T` in Kafka offset order.
3. Return the reconstructed state at time T.

### Determinism Rules

The projector must be a pure function:

- `new_state = apply_event(event, prev_state)`

Determinism requirements:

- Event processing order is stable (Kafka offset order).
- Unknown fields are ignored safely.
- Duplicate `event_id` is ignored (idempotent re-run).
- No calls to external services, time, randomness, or non-deterministic ordering during projection.

## Snapshot Strategy

Snapshots accelerate rebuilds by avoiding replaying the full history.

Snapshot record:

- tenant_id, aggregate_type, aggregate_id
- version, ts
- state (jsonb)
- created_at
- kafka_topic, kafka_partition, kafka_offset (for acceleration)

Snapshot creation policy:

- Periodic: every N events (default 100), or
- On demand: explicit “create snapshot” request.

Snapshot-based rebuild:

1. Fetch latest snapshot <= target (time or version).
2. Start replay from snapshot’s Kafka offset.
3. Apply remaining events to reach target.

## Persistence for Timeline (Read-Only Copy)

To support UI timelines and diffs, Phase 2 persists a read-only copy of event metadata and payload into Postgres:

- `event_log` table stores canonical event fields and indexes by tenant/aggregate/time/version.

This is not an event store; it is a replay log for visibility and debugging. Kafka remains the authoritative source for replay.

## Multi-Tenant Safety

Tenant safety invariants:

- Replay job is always tenant-scoped.
- Any DB access to `event_log`, `aggregate_snapshots`, and replay job tables is filtered by tenant and protected by Postgres RLS using `app.tenant_id`.
- API layer verifies request tenant matches authenticated tenant context (with existing super-admin rule if present).

## Replay Jobs (Auditability)

Replay runs are persisted as jobs:

- job_id, tenant_id
- status: running|done|failed
- start_offset, end_offset
- started_at, finished_at
- events_processed
- snapshot_used
- error

Each job is traceable and produces artifacts (diff examples and benchmarks).

## Proof Strategy

Phase 2 is considered proven only if:

- Tests show deterministic replay: same events => same state hash.
- Snapshot rebuild equals full rebuild.
- Tenant isolation is enforced in replay/timeline APIs.
- UI reads real persisted events and uses real replay/diff endpoints.
- Artifacts exist under `/reports/replay/` including benchmarks with rebuild <5s for the dev target.
