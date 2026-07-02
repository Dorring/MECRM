ALTER TABLE IF EXISTS customers
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
  ADD COLUMN IF NOT EXISTS deletion_type text;

ALTER TABLE IF EXISTS users
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
  ADD COLUMN IF NOT EXISTS deletion_type text;

DO $$
BEGIN
  IF to_regclass('public.customers') IS NOT NULL THEN
    BEGIN
      ALTER TABLE customers
        ADD CONSTRAINT customers_deletion_type_chk CHECK (deletion_type IS NULL OR deletion_type IN ('soft', 'gdpr_forget'));
    EXCEPTION WHEN duplicate_object THEN
      NULL;
    END;
    CREATE INDEX IF NOT EXISTS idx_customers_tenant_deleted_at ON customers (tenant_id, deleted_at);
  END IF;

  IF to_regclass('public.users') IS NOT NULL THEN
    BEGIN
      ALTER TABLE users
        ADD CONSTRAINT users_deletion_type_chk CHECK (deletion_type IS NULL OR deletion_type IN ('soft', 'gdpr_forget'));
    EXCEPTION WHEN duplicate_object THEN
      NULL;
    END;
    CREATE INDEX IF NOT EXISTS idx_users_tenant_deleted_at ON users (tenant_id, deleted_at);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS data_retention_policies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  entity_type text NOT NULL,
  retention_days integer NOT NULL CHECK (retention_days > 0),
  hard_delete boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_retention_policies_tenant_entity ON data_retention_policies (tenant_id, entity_type);

DO $$
BEGIN
  IF to_regclass('public.data_retention_policies') IS NOT NULL THEN
    ALTER TABLE data_retention_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE data_retention_policies FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS data_retention_policies_tenant_isolation ON data_retention_policies;
    CREATE POLICY data_retention_policies_tenant_isolation
      ON data_retention_policies
      FOR ALL
      USING (tenant_id = current_setting('app.tenant_id')::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
  END IF;
END $$;
