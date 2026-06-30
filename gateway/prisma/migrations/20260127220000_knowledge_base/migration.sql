CREATE TABLE "knowledge_drafts" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "source_ticket_id" uuid,
  "source_conversation_id" text,
  "title" varchar(500) NOT NULL,
  "problem_summary" text NOT NULL,
  "solution_steps" jsonb NOT NULL DEFAULT '[]'::jsonb,
  "preconditions" jsonb NOT NULL DEFAULT '[]'::jsonb,
  "tags" jsonb NOT NULL DEFAULT '[]'::jsonb,
  "topic" varchar(50),
  "confidence" numeric(4,3),
  "status" varchar(20) NOT NULL DEFAULT 'draft',
  "created_by" text,
  "approved_by" uuid,
  "approved_at" timestamp,
  "rejected_by" uuid,
  "rejected_at" timestamp,
  "rejection_reason" text,
  "created_at" timestamp NOT NULL DEFAULT now(),
  "updated_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "knowledge_drafts"
  ADD CONSTRAINT "knowledge_drafts_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "knowledge_drafts"
  ADD CONSTRAINT "knowledge_drafts_source_ticket_id_fkey" FOREIGN KEY ("source_ticket_id") REFERENCES "tickets" ("id") ON DELETE SET NULL;

ALTER TABLE "knowledge_drafts"
  ADD CONSTRAINT "knowledge_drafts_approved_by_fkey" FOREIGN KEY ("approved_by") REFERENCES "users" ("id") ON DELETE SET NULL;

ALTER TABLE "knowledge_drafts"
  ADD CONSTRAINT "knowledge_drafts_rejected_by_fkey" FOREIGN KEY ("rejected_by") REFERENCES "users" ("id") ON DELETE SET NULL;

CREATE INDEX "knowledge_drafts_tenant_status_created_idx" ON "knowledge_drafts" ("tenant_id", "status", "created_at" DESC);
CREATE INDEX "knowledge_drafts_tenant_source_ticket_idx" ON "knowledge_drafts" ("tenant_id", "source_ticket_id");

CREATE TABLE "knowledge_articles" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "source_draft_id" uuid,
  "title" varchar(500) NOT NULL,
  "content" text NOT NULL,
  "tags" jsonb NOT NULL DEFAULT '[]'::jsonb,
  "reuse_count" integer NOT NULL DEFAULT 0,
  "last_accessed_at" timestamp,
  "created_at" timestamp NOT NULL DEFAULT now(),
  "updated_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "knowledge_articles"
  ADD CONSTRAINT "knowledge_articles_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "knowledge_articles"
  ADD CONSTRAINT "knowledge_articles_source_draft_id_fkey" FOREIGN KEY ("source_draft_id") REFERENCES "knowledge_drafts" ("id") ON DELETE SET NULL;

CREATE INDEX "knowledge_articles_tenant_created_idx" ON "knowledge_articles" ("tenant_id", "created_at" DESC);
CREATE INDEX "knowledge_articles_tenant_source_draft_idx" ON "knowledge_articles" ("tenant_id", "source_draft_id");
CREATE UNIQUE INDEX "knowledge_articles_source_draft_unique" ON "knowledge_articles" ("source_draft_id");
