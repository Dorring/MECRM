-- Type convergence guard (Hardening 1.1)
--
-- Authority: all timestamp columns in the platform schema use `timestamptz`.
-- Prisma-managed tables are migrated by the forward Prisma migration
-- `20260702000000_timestamptz_convergence`; raw-SQL-track tables are guarded
-- here. This file is idempotent and safe to re-run.

DO $$
DECLARE
  bad_columns text;
BEGIN
  SELECT string_agg(
    table_name || '.' || column_name || ' (' || data_type ||
    COALESCE('(' || datetime_precision::text || ')', '') || ')',
    ', '
  )
  INTO bad_columns
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND data_type IN ('timestamp without time zone', 'timestamp')
    AND table_name NOT IN ('_prisma_migrations');

  IF bad_columns IS NOT NULL THEN
    RAISE EXCEPTION 'Type convergence failed: columns still using plain timestamp: %', bad_columns;
  END IF;
END $$;

-- Ensure raw-SQL-track tables that have timestamptz columns keep their defaults.
-- (No DDL changes required if they already use TIMESTAMPTZ; kept as documentation.)
SELECT 'type-convergence OK: all public timestamp columns are timestamptz' AS status;
