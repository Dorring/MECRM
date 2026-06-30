CREATE TABLE IF NOT EXISTS agent_decisions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  agent_id text NOT NULL,
  action_type text NOT NULL,
  risk_level text NOT NULL,
  status text NOT NULL,
  confidence numeric(4, 3),
  input_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  reasoning jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  tool_calls jsonb NOT NULL DEFAULT '[]'::jsonb,
  approval_id uuid,
  correlation_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_tenant_time ON agent_decisions (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_tenant_agent_time ON agent_decisions (tenant_id, agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_tenant_approval ON agent_decisions (tenant_id, approval_id);

-- Row Level Security: tenant isolation for agent decisions.
-- ENABLE + FORCE so the table owner / crm_app role is also subject to the policy.
-- 02-rls-policies.sql also covers agent_decisions via its loop, but that file
-- runs BEFORE this one in the fixed migration order, so it cannot reach a table
-- created here on a fresh database. Applying RLS inline (matching 03-08, 10)
-- guarantees coverage on first run and on idempotent re-runs.
ALTER TABLE agent_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_decisions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_decisions_tenant_isolation ON agent_decisions;
CREATE POLICY agent_decisions_tenant_isolation ON agent_decisions
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

