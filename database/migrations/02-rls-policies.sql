DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'users',
    'roles',
    'user_roles',
    'policies',
    'leads',
    'deals',
    'tickets',
    'customers',
    'agent_tasks',
    'agent_events',
    'agent_decisions',
    'ai_memory',
    'approvals',
    'audit_logs',
    'domain_events',
    'event_streams',
    'events',
    'outbox_events',
    'processed_events',
    'lead_read_model',
    'deal_pipeline_view',
    'customer_timeline_view',
    'security_events'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    IF to_regclass(format('public.%I', t)) IS NULL THEN
      CONTINUE;
    END IF;

    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);

    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant_isolation', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL USING (tenant_id = current_setting(''app.tenant_id'')::uuid) WITH CHECK (tenant_id = current_setting(''app.tenant_id'')::uuid)',
      t || '_tenant_isolation',
      t
    );
  END LOOP;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app') THEN
    CREATE ROLE crm_app LOGIN PASSWORD 'crm_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;

  EXECUTE format('GRANT CONNECT ON DATABASE %I TO crm_app', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO crm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO crm_app;

