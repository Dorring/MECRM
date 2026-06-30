CREATE TABLE IF NOT EXISTS processed_events (
  tenant_id uuid NOT NULL,
  event_id uuid NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, event_id)
);

CREATE TABLE IF NOT EXISTS lead_read_model (
  tenant_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  name text NOT NULL,
  email text NULL,
  phone text NULL,
  company text NULL,
  status text NOT NULL,
  score integer NULL,
  assigned_to uuid NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  version integer NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, lead_id)
);

CREATE INDEX IF NOT EXISTS lead_read_model_status_idx
  ON lead_read_model (tenant_id, status);

CREATE TABLE IF NOT EXISTS deal_pipeline_view (
  tenant_id uuid NOT NULL,
  stage text NOT NULL,
  deal_count integer NOT NULL DEFAULT 0,
  total_amount numeric(15,2) NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, stage)
);

CREATE TABLE IF NOT EXISTS customer_timeline_view (
  tenant_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  last_event_at timestamptz NULL,
  open_tickets integer NOT NULL DEFAULT 0,
  active_deals integer NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, customer_id)
);

ALTER TABLE processed_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS processed_events_tenant_isolation ON processed_events;
CREATE POLICY processed_events_tenant_isolation ON processed_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE lead_read_model ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_read_model FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lead_read_model_tenant_isolation ON lead_read_model;
CREATE POLICY lead_read_model_tenant_isolation ON lead_read_model
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE deal_pipeline_view ENABLE ROW LEVEL SECURITY;
ALTER TABLE deal_pipeline_view FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deal_pipeline_view_tenant_isolation ON deal_pipeline_view;
CREATE POLICY deal_pipeline_view_tenant_isolation ON deal_pipeline_view
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE customer_timeline_view ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_timeline_view FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS customer_timeline_view_tenant_isolation ON customer_timeline_view;
CREATE POLICY customer_timeline_view_tenant_isolation ON customer_timeline_view
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

