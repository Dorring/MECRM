-- M-Agent-ECRM migration advisory lock
--
-- This file is documentation-only. The actual advisory lock is acquired and held
-- by the migration runner (scripts/migrate.sh or scripts/migrate.ps1) for the
-- entire duration of:
--
--   Prisma migrate deploy -> raw SQL 01-11 -> RLS audit
--
-- Lock key: 405011 (arbitrary 64-bit constant chosen for this project).
--
-- Holding a session-level advisory lock prevents two concurrent deployment
-- pipelines or `docker compose --profile migrate run --rm migrate` instances
-- from racing on DDL/RLS operations. The lock is released when the runner's
-- PostgreSQL session ends (or via explicit pg_advisory_unlock in cleanup).
--
-- Do NOT run migrations without this lock in production.

SELECT 'advisory lock documented; lock key = 405011' AS note;
