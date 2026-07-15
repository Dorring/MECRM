# Timestamp Type Convergence — Hardening 1.1

> Status: part of Group A (`hardening/db-migration`)  
> Authority: `timestamptz` is the single timestamp type for the platform schema.

## Why

The Batch 1 Prisma init migration (`20260131062707_init`) changed many columns
to `TIMESTAMP(3)` (timestamp without time zone, millisecond precision), while the
raw-SQL track and newer tables used `timestamptz`. This split caused:

- Confusion about which type is canonical.
- Risk that future raw-SQL files or Prisma migrations drift further.
- Potential bugs when application code assumes time-zone-aware storage.

Hardening 1.1 converges everything to `timestamptz(6)`.

## Scope

| Track | Mechanism | File |
|---|---|---|
| Prisma-managed tables | Forward Prisma migration | `gateway/prisma/migrations/20260702000000_timestamptz_convergence/migration.sql` |
| Raw-SQL-track tables | Guard / assertion | `database/migrations/12-type-convergence.sql` |
| Application schema authority | `DateTime @db.Timestamptz(6)` | `gateway/prisma/schema.prisma` |

Tables converted in the Prisma migration include `tenants`, `users`, `roles`,
`leads`, `deals`, `tickets`, `customers`, `agent_tasks`, `agent_events`,
`agent_decisions`, `approvals`, `audit_logs`, `data_retention_policies`,
`event_streams`, `events`, `outbox_events`, `processed_events`, read models,
automation/prediction/knowledge/productivity tables, etc.

Raw-SQL-track tables (`event_log`, `aggregate_snapshots`, `replay_jobs`,
`customer_twins`, `twin_simulation_log`, `devx_insights`) already used
`timestamptz`; `12-type-convergence.sql` asserts this and fails if any plain
`timestamp` column is found in an MECRM-owned table. Keycloak's Liquibase
metadata tables are excluded because their schema is controlled by Keycloak.

## Live-schema drift assessment

Before applying in production, generate a drift report from a copy of the
production database:

```bash
cd gateway
npx prisma migrate diff \
  --from-url "$DATABASE_URL" \
  --to-schema-datamodel prisma/schema.prisma \
  --script > /tmp/timestamptz-drift.sql
```

Expected output: only `ALTER TABLE ... ALTER COLUMN ... SET DATA TYPE TIMESTAMPTZ(6)`
statements. Any additional DDL must be reviewed separately — it indicates drift
not caused by the timestamp convergence.

## Lock and downtime risk

`ALTER TABLE ... ALTER COLUMN ... SET DATA TYPE` acquires an `ACCESS EXCLUSIVE`
lock on the table for the duration of the rewrite. For small tables this is
sub-second. For large tables (e.g. `events`, `outbox_events`, `event_log`) it
can be noticeable.

Mitigation:

1. Apply during a low-traffic window.
2. For very large tables, consider:
   - Adding a new `timestamptz` column.
   - Backfilling with triggers or batch updates.
   - Swapping columns and dropping the old one.
   - This complex path is out of scope for the standard migration; document it
     in your runbook if your event tables exceed tens of millions of rows.
3. After the migration, run `ANALYZE` on rewritten tables so the planner has
   fresh statistics.

## Rollback

The forward migration is reversible by re-altering the columns back to
`TIMESTAMP(3)`:

```sql
-- Example rollback snippet (generate full list from the forward migration)
ALTER TABLE "leads" ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3);
ALTER TABLE "leads" ALTER COLUMN "updated_at" SET DATA TYPE TIMESTAMP(3);
-- ... repeat for all converted columns
```

Generate the full rollback script by replacing `TIMESTAMPTZ(6)` with
`TIMESTAMP(3)` in `migration.sql`. Because `timestamptz` values are stored as
UTC, converting back to `timestamp(3)` does **not** lose data, but it drops
time-zone awareness.

> Rollback should be tested against a restored backup, not on production.

## Validation

After migration:

```bash
# Local / CI
./scripts/migrate.sh

# Drift-only run (must exit 0 and show all tenant tables RLS OK)
./scripts/migrate.sh --drift-only

# Verify no plain timestamp columns remain
psql "$DATABASE_URL" -c "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND data_type IN ('timestamp without time zone', 'timestamp');"
```

Expected: zero rows.

## Vacuum / analyze strategy

Add to your post-migration runbook:

```sql
ANALYZE users;
ANALYZE leads;
ANALYZE deals;
ANALYZE tickets;
ANALYZE customers;
ANALYZE events;
ANALYZE outbox_events;
ANALYZE event_log;
-- Add any other large tables rewritten by the migration.
```

`VACUUM` is usually not required because `ALTER TYPE` rewrites the heap in place,
but a `VACUUM ANALYZE` can be run if autovacuum is disabled or if bloat is a
concern.

## Compatibility notes

- Prisma Client reads `timestamptz` into JavaScript `Date` objects exactly as it
  did `timestamp(3)`. No application code changes are required.
- Existing indexes on the converted columns are preserved.
- Default expressions (`now()`, `@default(now())`) continue to work.
- `@updatedAt` fields continue to work.
