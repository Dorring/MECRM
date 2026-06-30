CREATE TABLE "productivity_proposals" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "user_id" uuid NOT NULL,
  "action_type" varchar(50) NOT NULL,
  "target_entity" varchar(50) NOT NULL,
  "target_id" text NOT NULL,
  "priority" varchar(20) NOT NULL,
  "justification" text NOT NULL,
  "drafts" jsonb NOT NULL DEFAULT '{}'::jsonb,
  "status" varchar(20) NOT NULL DEFAULT 'pending',
  "decided_by" uuid,
  "decided_at" timestamp,
  "decision_reason" text,
  "created_at" timestamp NOT NULL DEFAULT now(),
  "updated_at" timestamp NOT NULL DEFAULT now(),
  "dedupe_key" varchar(128) NOT NULL,
  "signal_type" varchar(50) NOT NULL,
  "signal" jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE "productivity_proposals"
  ADD CONSTRAINT "productivity_proposals_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "productivity_proposals"
  ADD CONSTRAINT "productivity_proposals_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE RESTRICT;

ALTER TABLE "productivity_proposals"
  ADD CONSTRAINT "productivity_proposals_decided_by_fkey" FOREIGN KEY ("decided_by") REFERENCES "users" ("id") ON DELETE SET NULL;

CREATE INDEX "productivity_proposals_tenant_status_priority_idx" ON "productivity_proposals" ("tenant_id", "status", "priority");
CREATE INDEX "productivity_proposals_tenant_user_status_idx" ON "productivity_proposals" ("tenant_id", "user_id", "status");
CREATE INDEX "productivity_proposals_tenant_dedupe_idx" ON "productivity_proposals" ("tenant_id", "dedupe_key");
CREATE UNIQUE INDEX "productivity_proposals_tenant_dedupe_pending_uniq" ON "productivity_proposals" ("tenant_id", "dedupe_key") WHERE status = 'pending';

