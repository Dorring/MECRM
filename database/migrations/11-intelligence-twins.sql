-- Digital Customer Twins - Phase 9
-- Behavioral modeling for customer simulation

-- Customer Twins table storing behavioral profiles
CREATE TABLE IF NOT EXISTS customer_twins (
  tenant_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  embedding_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
  behavior_features jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_updated timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, customer_id)
);

-- Index for efficient lookups by last_updated for refresh jobs
CREATE INDEX IF NOT EXISTS customer_twins_last_updated_idx
  ON customer_twins (tenant_id, last_updated);

-- Twin simulation log for audit purposes
CREATE TABLE IF NOT EXISTS twin_simulation_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  user_id uuid NOT NULL,
  scenario text NOT NULL,
  input_params jsonb NOT NULL DEFAULT '{}'::jsonb,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  confidence numeric(5,4) NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS twin_simulation_log_customer_idx
  ON twin_simulation_log (tenant_id, customer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS twin_simulation_log_user_idx
  ON twin_simulation_log (tenant_id, user_id, created_at DESC);

-- DevX Insights table for AI SRE insights
CREATE TABLE IF NOT EXISTS devx_insights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_type text NOT NULL,
  severity text NOT NULL DEFAULT 'medium',
  confidence numeric(5,4) NOT NULL DEFAULT 0,
  suspected_services jsonb NOT NULL DEFAULT '[]'::jsonb,
  suggested_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
  signals jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz NULL,
  acknowledged_by uuid NULL
);

CREATE INDEX IF NOT EXISTS devx_insights_status_idx
  ON devx_insights (status, created_at DESC);

CREATE INDEX IF NOT EXISTS devx_insights_type_idx
  ON devx_insights (incident_type, created_at DESC);

-- Row Level Security for customer_twins
ALTER TABLE customer_twins ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_twins FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS customer_twins_tenant_isolation ON customer_twins;
CREATE POLICY customer_twins_tenant_isolation ON customer_twins
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- Row Level Security for twin_simulation_log
ALTER TABLE twin_simulation_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE twin_simulation_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS twin_simulation_log_tenant_isolation ON twin_simulation_log;
CREATE POLICY twin_simulation_log_tenant_isolation ON twin_simulation_log
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- Note: devx_insights is NOT tenant-scoped (system-wide operational data)
-- Access controlled via OPA policies at application layer
