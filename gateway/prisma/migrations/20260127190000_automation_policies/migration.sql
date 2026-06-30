CREATE TABLE "automation_policies" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "created_by" uuid NOT NULL,
  "status" varchar(20) NOT NULL DEFAULT 'draft',
  "nl_rule_text" text NOT NULL,
  "trigger_type" varchar(100) NOT NULL,
  "workflow_json" jsonb NOT NULL,
  "compiled_json" jsonb NOT NULL,
  "version" integer NOT NULL DEFAULT 1,
  "last_simulation_id" uuid,
  "created_at" timestamp NOT NULL DEFAULT now(),
  "updated_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "automation_policies"
  ADD CONSTRAINT "automation_policies_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "automation_policies"
  ADD CONSTRAINT "automation_policies_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "users" ("id") ON DELETE RESTRICT;

CREATE INDEX "automation_policies_tenant_status_idx" ON "automation_policies" ("tenant_id", "status");

CREATE TABLE "automation_simulations" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "policy_id" uuid NOT NULL,
  "requested_by" uuid,
  "from_ts" timestamp,
  "to_ts" timestamp,
  "result" jsonb NOT NULL,
  "created_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "automation_simulations"
  ADD CONSTRAINT "automation_simulations_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "automation_simulations"
  ADD CONSTRAINT "automation_simulations_policy_id_fkey" FOREIGN KEY ("policy_id") REFERENCES "automation_policies" ("id") ON DELETE CASCADE;

ALTER TABLE "automation_simulations"
  ADD CONSTRAINT "automation_simulations_requested_by_fkey" FOREIGN KEY ("requested_by") REFERENCES "users" ("id") ON DELETE SET NULL;

CREATE INDEX "automation_simulations_tenant_policy_created_idx" ON "automation_simulations" ("tenant_id", "policy_id", "created_at" DESC);

CREATE TABLE "automation_executions" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "policy_id" uuid NOT NULL,
  "trigger_event_id" uuid,
  "trigger_type" varchar(100) NOT NULL,
  "actions_json" jsonb NOT NULL,
  "status" varchar(20) NOT NULL DEFAULT 'completed',
  "dry_run" boolean NOT NULL DEFAULT false,
  "created_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "automation_executions"
  ADD CONSTRAINT "automation_executions_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "automation_executions"
  ADD CONSTRAINT "automation_executions_policy_id_fkey" FOREIGN KEY ("policy_id") REFERENCES "automation_policies" ("id") ON DELETE CASCADE;

CREATE INDEX "automation_executions_tenant_policy_created_idx" ON "automation_executions" ("tenant_id", "policy_id", "created_at" DESC);

