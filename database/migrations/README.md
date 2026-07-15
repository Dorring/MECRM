# Database Migrations — M-Agent-ECRM

This directory holds the **raw SQL track** of database migrations. It is one of
two migration tracks and is consumed by the single runner
[`scripts/migrate.sh`](../../scripts/migrate.sh) (bash) /
[`scripts/migrate.ps1`](../../scripts/migrate.ps1) (PowerShell).

## Two-track strategy (authoritative relationship)

The platform deliberately uses **two complementary tracks**, each owning a
non-overlapping concern:

| Track | Source of truth | Owns | Tool |
|---|---|---|---|
| **Prisma migrations** | [`gateway/prisma/schema.prisma`](../../gateway/prisma/schema.prisma) + [`gateway/prisma/migrations/`](../../gateway/prisma/migrations/) | Application tables, indexes, foreign keys, unique constraints (CRM entities, governance entities, CQRS tables that the app reads/writes) | `npx prisma migrate deploy` |
| **Raw SQL (this dir)** | `database/migrations/*.sql` | Database-level concerns Prisma cannot express: RLS policies (`ENABLE`+`FORCE`), the `crm_app` low-privilege role + grants, event-sourcing/event-log tables, aggregate snapshots, replay jobs, customer twins / DevX insights, soft-delete columns + CHECK constraints | `psql` |

### Rule of thumb
- **Prisma** manages anything the application model touches (`gateway/prisma/schema.prisma`).
- **Raw SQL** manages RLS, roles, functions, and the handful of event-sourcing /
  analytics tables that are intentionally **not** in the Prisma schema
  (`event_log`, `aggregate_snapshots`, `replay_jobs`, `customer_twins`,
  `twin_simulation_log`, `devx_insights`).
- When a table exists in **both** tracks (e.g. `leads`, `customers`,
  `outbox_events`, `agent_decisions`, `data_retention_policies`), the raw SQL
  uses `CREATE TABLE IF NOT EXISTS` so it never clobbers the Prisma-created
  table; its purpose there is only to add RLS / columns / indexes Prisma does
  not own. **Prisma wins on column shape** for shared tables.

### The duplicate schema.prisma problem
There are two `schema.prisma` files in the repo:

| File | Size | Status |
|---|---|---|
| `gateway/prisma/schema.prisma` | ~30 KB | **Authoritative.** Full model set (Tenant, User+soft-delete, Automation*, Journey/Predictions, Knowledge, Productivity, AgentDecision, DataRetentionPolicy, EventStream/Event/OutboxEvent/ProcessedEvent, read models). Has the matching `migrations/` directory. Declares `binaryTargets`. |
| `database/prisma/schema.prisma` | ~15 KB | **Stale partial copy.** Only 16 core models. Missing soft-delete columns on `User`/`Customer`. Missing Automation, Journey, Knowledge, Productivity, AgentDecision, DataRetentionPolicy, EventStore/Outbox/Read-model models. No `binaryTargets`. No `migrations/` directory. |

**Recommendation:** `database/prisma/schema.prisma` should be retired (deleted
or replaced with a pointer) and `gateway/prisma/schema.prisma` treated as the
single source of truth for the application schema. Until it is removed, the
runner and this README treat `gateway/prisma/schema.prisma` as authoritative
and do **not** read `database/prisma/schema.prisma`.

## Fixed execution order

The runner applies, in this exact order, on every invocation:

```
 0.  Session-level PostgreSQL advisory lock (held for entire runner lifetime)
 1.  npx prisma migrate deploy                      (gateway/prisma)
 2.  00-advisory-lock.sql      lock semantics documentation
 3.  01-core-tables.sql          leads, customers (idempotent backstop)
 4.  02-rls-policies.sql         ENABLE+FORCE RLS loop, crm_app role + grants
 5.  03-event-log.sql            event_log + RLS
 6.  04-aggregate-snapshots.sql  aggregate_snapshots + RLS
 7.  05-replay-jobs.sql          replay_jobs + RLS
 8.  06-event-store.sql          event_streams, events + RLS
 9.  07-outbox.sql               outbox_events + RLS
10.  08-read-models.sql          processed_events, lead_read_model,
                                deal_pipeline_view, customer_timeline_view + RLS
11.  09-agent-decisions.sql      agent_decisions + RLS (inline, see note below)
12.  10-data-governance.sql      soft-delete cols on customers/users,
                                data_retention_policies + RLS
13.  11-intelligence-twins.sql   customer_twins, twin_simulation_log (+ RLS),
                                devx_insights (NO RLS — system-wide)
14.  12-type-convergence.sql     timestamp-type authority guard
15.  drift + RLS audit           (fails on missing tenant RLS, see below)
```

### Why `02-rls-policies.sql` runs early but RLS still works
`02-rls-policies.sql` iterates a hard-coded list of tenant tables and applies
`ENABLE`+`FORCE` RLS, but it `CONTINUE`s past any table that does not yet exist
(`to_regclass` guard). Because `02` runs before `06`–`11`, tables created by
those later files would **not** receive RLS from the loop on a fresh database.

To guarantee coverage on first run and on idempotent re-runs, every later file
that creates a tenant-scoped table (03, 04, 05, 06, 07, 08, 10, 11 for twins,
and now **09**) applies its own inline `ENABLE`+`FORCE` RLS + `USING` +
`WITH CHECK` policy. This belt-and-suspenders pattern means RLS is correct
regardless of whether `02` already saw the table.

### Tables intentionally without RLS
- `tenants` — the tenant directory itself (non-tenant-scoped).
- `ai_agents` — global agent registry (no `tenant_id` column).
- `devx_insights` — system-wide operational/SRE data, gated by OPA at the
  application layer (documented inline in `11-intelligence-twins.sql`).

## RLS contract (Phase 1 / Phase 3 requirement)

Every tenant-scoped table MUST have, verified by the runner's RLS audit:

1. `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;`
2. `ALTER TABLE <t> FORCE ROW LEVEL SECURITY;`  ← forces the **table owner**
   and the `crm_app` role to also obey the policy (no owner bypass).
3. A policy covering **both** `USING` (reads/updates/deletes) **and**
   `WITH CHECK` (inserts/updates) — so a session cannot write a row for a
   different tenant than `current_setting('app.tenant_id')`.

The RLS audit checks an explicit tenant-table allowlist (declared in
`scripts/migrate.sh` and `scripts/migrate.ps1`). Missing `ENABLE`, `FORCE`, or
`FOR ALL` policy on any allowlisted table causes the runner to exit non-zero.
Tables that are intentionally not tenant-scoped are allowlisted separately and
are not required to have RLS.

Standard policy (applied to all tenant tables):

```sql
CREATE POLICY <t>_tenant_isolation ON <t>
  FOR ALL
  USING    (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
```

## Privilege model — do NOT migrate as `crm_app`

Migrations run as the **high-privilege owner account** (`POSTGRES_USER`, default
`crm_user`). This is mandatory because:

- DDL (`CREATE TABLE`, `ALTER TABLE`, `CREATE POLICY`) requires the owner.
- `ALTER TABLE ... FORCE ROW LEVEL SECURITY` can only be issued by the table
  owner / a superuser. The low-privilege `crm_app` role **cannot** FORCE RLS.

If you ran migrations as `crm_app`, the `FORCE` statements would silently fail
(or, without `ON_ERROR_STOP`, be skipped), the table owner would still bypass
RLS, and the security control would be invisibly broken. The runner therefore
connects as `POSTGRES_USER` and only **grants** `crm_app` its limited privileges
at the end of `02-rls-policies.sql`. At runtime the gateway/agents connect as
`crm_app`, which is fully subject to `FORCE` RLS.

## Idempotency

- Prisma `migrate deploy` is idempotent (re-applies nothing already recorded).
- Every `CREATE TABLE` uses `IF NOT EXISTS`; every `DROP POLICY` uses
  `IF EXISTS`; indexes use `IF NOT EXISTS`; constraints use `ADD ... IF NOT
  EXISTS` or `EXCEPTION WHEN duplicate_object`.
- Re-running the runner on an already-migrated database is safe and produces
  no schema change.

## Initializing an empty database

```bash
# 1. Postgres is up with POSTGRES_DB=enterprise_crm created by the image
cp .env.example .env   # edit DATABASE_URL / POSTGRES_* for your host

# 2. One command
./scripts/migrate.sh
#   (Windows PowerShell: ./scripts/migrate.ps1)
```

The runner will: create all Prisma tables via `migrate deploy`, apply the raw
SQL track, grant `crm_app`, and print a drift/RLS audit. On a truly empty DB
the `_prisma_migrations` table is created by Prisma.

## Upgrading an existing database

```bash
# After pulling changes that added a Prisma migration and/or a new SQL file:
./scripts/migrate.sh
```

- Prisma only applies migrations not yet recorded in `_prisma_migrations`.
- Raw SQL files are re-applied in full but are idempotent, so only new
  `IF NOT EXISTS` objects appear.

To add a **new** raw SQL migration: name it `NN-<topic>.sql` (next number,
e.g. `12-...`), add it to `SQL_FILES` in both `migrate.sh` and `migrate.ps1`,
and follow the RLS contract above for any tenant-scoped table.

## Advisory lock

The runner holds a single PostgreSQL session-level advisory lock for the entire
duration of `Prisma -> SQL -> audit`. The lock key is `405011`. This prevents
two CI jobs or operators from running migrations concurrently on the same
database. The lock is released by a `trap`/`finally` even if the runner fails.

## Schema drift and RLS audit

`scripts/migrate.sh --drift-only` (or `-DriftOnly` on PowerShell) reports:

1. **Prisma-declared tables missing from the DB** — parsed from `@@map(...)`
   in `gateway/prisma/schema.prisma` vs `information_schema.tables`.
2. **DB tables not in the Prisma schema** — filtered against an allowlist of
   tables known to be owned by the raw SQL track (`event_log`,
   `aggregate_snapshots`, `replay_jobs`, `customer_twins`,
   `twin_simulation_log`, `devx_insights`) plus `_prisma_migrations`.
3. **RLS enforcement audit** — for every table in the explicit tenant-table
   allowlist, verifies `ENABLE`, `FORCE`, and a `FOR ALL` policy. Any failure
   causes the runner to exit non-zero. Use `--audit-warn` in development to
   downgrade to a warning.

This is a **coarse, table-presence** check (it does not compare column types).
For full-fidelity drift use:

```bash
cd gateway && npx prisma migrate diff \
  --from-schema-datasource prisma/schema.prisma \
  --to-schema-datamodel prisma/schema.prisma
```

## Timestamp type authority

All timestamp columns in MECRM-owned tables use `timestamptz`:

- Prisma-managed tables are converted by the forward migration
  `gateway/prisma/migrations/20260702000000_timestamptz_convergence`.
- Raw-SQL-track tables are guarded by `12-type-convergence.sql`, which raises
  an error if any plain `timestamp` column remains. Keycloak-owned Liquibase
  metadata tables are excluded from this application-schema guard.

See `docs/migration-type-convergence.md` for upgrade, rollback, vacuum/analyze,
and lock-risk notes.

## File inventory

| File | Tables / concern |
|---|---|
| `00-advisory-lock.sql` | Advisory lock semantics (documentation) |
| `01-core-tables.sql` | `tenants`, `leads`, `customers` (backstop for Prisma) |
| `02-rls-policies.sql` | RLS loop over tenant tables; `crm_app` role + grants |
| `03-event-log.sql` | `event_log` + RLS |
| `04-aggregate-snapshots.sql` | `aggregate_snapshots` + RLS |
| `05-replay-jobs.sql` | `replay_jobs` + RLS |
| `06-event-store.sql` | `event_streams`, `events` + RLS |
| `07-outbox.sql` | `outbox_events` + RLS |
| `08-read-models.sql` | `processed_events`, `lead_read_model`, `deal_pipeline_view`, `customer_timeline_view` + RLS |
| `09-agent-decisions.sql` | `agent_decisions` + inline RLS |
| `10-data-governance.sql` | soft-delete cols on `customers`/`users`, `data_retention_policies` + RLS |
| `11-intelligence-twins.sql` | `customer_twins`, `twin_simulation_log` (+ RLS), `devx_insights` (no RLS) |
| `12-type-convergence.sql` | Timestamp-type authority guard |
