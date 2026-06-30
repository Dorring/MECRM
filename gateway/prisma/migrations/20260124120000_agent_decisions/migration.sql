CREATE TABLE IF NOT EXISTS "agent_decisions" (
  "id" UUID NOT NULL,
  "tenant_id" UUID NOT NULL,
  "agent_id" TEXT NOT NULL,
  "action_type" TEXT NOT NULL,
  "risk_level" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "confidence" DECIMAL(4,3),
  "input_context" JSONB NOT NULL DEFAULT '{}'::jsonb,
  "reasoning" JSONB NOT NULL DEFAULT '{}'::jsonb,
  "evidence" JSONB NOT NULL DEFAULT '[]'::jsonb,
  "tool_calls" JSONB NOT NULL DEFAULT '[]'::jsonb,
  "approval_id" UUID,
  "correlation_id" UUID,
  "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "agent_decisions_pkey" PRIMARY KEY ("id")
);

CREATE INDEX IF NOT EXISTS "idx_agent_decisions_tenant_time" ON "agent_decisions"("tenant_id","created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_agent_decisions_tenant_agent_time" ON "agent_decisions"("tenant_id","agent_id","created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_agent_decisions_tenant_approval" ON "agent_decisions"("tenant_id","approval_id");

