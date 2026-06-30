# Phase 8: Disaster Recovery Simulation

## Definition of “disaster”

This project treats “disaster” as a hard loss of infrastructure state that requires rebuild from durable assets:

- **DB data loss**: Postgres database dropped or schema wiped.
- **Kafka data loss**: Kafka cluster reset (topics deleted, offsets lost).
- **Region-wide outage (simulated)**: all services down; recovery is executed in an isolated environment.

## Recovery assets

Durable recovery inputs:

- **DB backups**: logical Postgres backups covering write-side tables and read models.
- **Snapshots**: `aggregate_snapshots` (Phase 2) to accelerate replay when present.
- **Event history**: `events` (Phase 3 event store) and `event_log` (Phase 2 replay log) are the source of truth for deterministic rebuild.

## Recovery guarantees

- **Tenant safety**: restore and rebuild are tenant-scoped; validations detect cross-tenant mixing. RLS remains enabled after restore.
- **Deterministic rebuild**: read models are rebuilt from events in a stable order; repeated rebuild produces identical checksums.
- **No manual fixes**: recovery is executable by scripts + documented steps. No hand-edited SQL.

## RPO / RTO targets

- **RPO target**: ≤ 5 minutes.\n Measured as the time gap between the last durable event timestamp and the start of backup creation.\n- **RTO target**: ≤ 30 minutes.\n Measured from the start of the failure simulation to completion of restore + rebuild + integrity validation.

## Step-by-step recovery procedure

### Human steps (operator runbook)

1. Confirm recovery environment is isolated (separate compose project or dedicated restore database).
2. Identify the backup id to restore.
3. Run restore automation (database restore → snapshots restore → rebuild → validate).
4. Inspect generated reports and confirm RPO/RTO targets are met.

### Automated steps (scripts/services)

1. Create backup artifacts:\n - database backup\n - snapshot export + manifest\n2. Simulate failure safely (wipe target database / stop services).\n3. Restore:\n - restore database\n - restore snapshots\n - rebuild read models from events\n4. Validate integrity:\n - tenant-scoped counts and checksums\n - determinism re-run for read model rebuild\n5. Persist proof artifacts under `reports/dr/`.

## Operational notes

- Dev backups are produced via docker-exec against the Postgres container and stored in local object storage.\n- Prod-ready mode supports S3-compatible object storage.\n- Kafka loss recovery relies on Postgres `event_log` and/or write-side `events`; Kafka is not required to be available during rebuild.
