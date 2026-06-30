-- DropForeignKey
ALTER TABLE "automation_executions" DROP CONSTRAINT "automation_executions_policy_id_fkey";

-- DropForeignKey
ALTER TABLE "automation_executions" DROP CONSTRAINT "automation_executions_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "automation_policies" DROP CONSTRAINT "automation_policies_created_by_fkey";

-- DropForeignKey
ALTER TABLE "automation_policies" DROP CONSTRAINT "automation_policies_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "automation_simulations" DROP CONSTRAINT "automation_simulations_policy_id_fkey";

-- DropForeignKey
ALTER TABLE "automation_simulations" DROP CONSTRAINT "automation_simulations_requested_by_fkey";

-- DropForeignKey
ALTER TABLE "automation_simulations" DROP CONSTRAINT "automation_simulations_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "customer_profiles" DROP CONSTRAINT "customer_profiles_customer_id_fkey";

-- DropForeignKey
ALTER TABLE "customer_profiles" DROP CONSTRAINT "customer_profiles_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "customer_timelines" DROP CONSTRAINT "customer_timelines_customer_id_fkey";

-- DropForeignKey
ALTER TABLE "customer_timelines" DROP CONSTRAINT "customer_timelines_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "knowledge_articles" DROP CONSTRAINT "knowledge_articles_source_draft_id_fkey";

-- DropForeignKey
ALTER TABLE "knowledge_articles" DROP CONSTRAINT "knowledge_articles_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "knowledge_drafts" DROP CONSTRAINT "knowledge_drafts_approved_by_fkey";

-- DropForeignKey
ALTER TABLE "knowledge_drafts" DROP CONSTRAINT "knowledge_drafts_rejected_by_fkey";

-- DropForeignKey
ALTER TABLE "knowledge_drafts" DROP CONSTRAINT "knowledge_drafts_source_ticket_id_fkey";

-- DropForeignKey
ALTER TABLE "knowledge_drafts" DROP CONSTRAINT "knowledge_drafts_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "predictions" DROP CONSTRAINT "predictions_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "productivity_proposals" DROP CONSTRAINT "productivity_proposals_decided_by_fkey";

-- DropForeignKey
ALTER TABLE "productivity_proposals" DROP CONSTRAINT "productivity_proposals_tenant_id_fkey";

-- DropForeignKey
ALTER TABLE "productivity_proposals" DROP CONSTRAINT "productivity_proposals_user_id_fkey";

-- DropIndex
DROP INDEX "idx_agent_decisions_tenant_agent_time";

-- DropIndex
DROP INDEX "idx_agent_decisions_tenant_time";

-- DropIndex
DROP INDEX "automation_executions_tenant_policy_created_idx";

-- DropIndex
DROP INDEX "automation_simulations_tenant_policy_created_idx";

-- DropIndex
DROP INDEX "customer_profiles_tenant_customer_idx";

-- DropIndex
DROP INDEX "knowledge_articles_tenant_created_idx";

-- DropIndex
DROP INDEX "knowledge_drafts_tenant_status_created_idx";

-- AlterTable
ALTER TABLE "automation_executions" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "automation_policies" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "updated_at" DROP DEFAULT,
ALTER COLUMN "updated_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "automation_simulations" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "from_ts" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "to_ts" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "customer_profiles" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "stage_updated_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "updated_at" DROP DEFAULT,
ALTER COLUMN "updated_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "customer_timelines" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "timestamp" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "knowledge_articles" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "last_accessed_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "updated_at" DROP DEFAULT,
ALTER COLUMN "updated_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "knowledge_drafts" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "approved_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "rejected_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "updated_at" DROP DEFAULT,
ALTER COLUMN "updated_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "predictions" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "productivity_proposals" ALTER COLUMN "id" DROP DEFAULT,
ALTER COLUMN "decided_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "created_at" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "updated_at" DROP DEFAULT,
ALTER COLUMN "updated_at" SET DATA TYPE TIMESTAMP(3);

-- CreateIndex
CREATE INDEX "agent_decisions_tenant_id_created_at_idx" ON "agent_decisions"("tenant_id", "created_at");

-- CreateIndex
CREATE INDEX "agent_decisions_tenant_id_agent_id_created_at_idx" ON "agent_decisions"("tenant_id", "agent_id", "created_at");

-- CreateIndex
CREATE INDEX "automation_executions_tenant_id_policy_id_created_at_idx" ON "automation_executions"("tenant_id", "policy_id", "created_at");

-- CreateIndex
CREATE INDEX "automation_simulations_tenant_id_policy_id_created_at_idx" ON "automation_simulations"("tenant_id", "policy_id", "created_at");

-- CreateIndex
CREATE INDEX "knowledge_articles_tenant_id_created_at_idx" ON "knowledge_articles"("tenant_id", "created_at");

-- CreateIndex
CREATE INDEX "knowledge_drafts_tenant_id_status_created_at_idx" ON "knowledge_drafts"("tenant_id", "status", "created_at");

-- CreateIndex
CREATE INDEX "outbox_events_tenant_id_created_at_idx" ON "outbox_events"("tenant_id", "created_at");

-- AddForeignKey
ALTER TABLE "automation_policies" ADD CONSTRAINT "automation_policies_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "automation_policies" ADD CONSTRAINT "automation_policies_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "users"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "automation_simulations" ADD CONSTRAINT "automation_simulations_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "automation_simulations" ADD CONSTRAINT "automation_simulations_policy_id_fkey" FOREIGN KEY ("policy_id") REFERENCES "automation_policies"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "automation_simulations" ADD CONSTRAINT "automation_simulations_requested_by_fkey" FOREIGN KEY ("requested_by") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "automation_executions" ADD CONSTRAINT "automation_executions_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "automation_executions" ADD CONSTRAINT "automation_executions_policy_id_fkey" FOREIGN KEY ("policy_id") REFERENCES "automation_policies"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "customer_timelines" ADD CONSTRAINT "customer_timelines_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "customer_timelines" ADD CONSTRAINT "customer_timelines_customer_id_fkey" FOREIGN KEY ("customer_id") REFERENCES "customers"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "customer_profiles" ADD CONSTRAINT "customer_profiles_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "customer_profiles" ADD CONSTRAINT "customer_profiles_customer_id_fkey" FOREIGN KEY ("customer_id") REFERENCES "customers"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "predictions" ADD CONSTRAINT "predictions_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "agent_decisions" ADD CONSTRAINT "agent_decisions_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "productivity_proposals" ADD CONSTRAINT "productivity_proposals_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "productivity_proposals" ADD CONSTRAINT "productivity_proposals_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "productivity_proposals" ADD CONSTRAINT "productivity_proposals_decided_by_fkey" FOREIGN KEY ("decided_by") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_drafts" ADD CONSTRAINT "knowledge_drafts_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_drafts" ADD CONSTRAINT "knowledge_drafts_source_ticket_id_fkey" FOREIGN KEY ("source_ticket_id") REFERENCES "tickets"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_drafts" ADD CONSTRAINT "knowledge_drafts_approved_by_fkey" FOREIGN KEY ("approved_by") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_drafts" ADD CONSTRAINT "knowledge_drafts_rejected_by_fkey" FOREIGN KEY ("rejected_by") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_articles" ADD CONSTRAINT "knowledge_articles_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_articles" ADD CONSTRAINT "knowledge_articles_source_draft_id_fkey" FOREIGN KEY ("source_draft_id") REFERENCES "knowledge_drafts"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- RenameIndex
ALTER INDEX "idx_agent_decisions_tenant_approval" RENAME TO "agent_decisions_tenant_id_approval_id_idx";

-- RenameIndex
ALTER INDEX "automation_policies_tenant_status_idx" RENAME TO "automation_policies_tenant_id_status_idx";

-- RenameIndex
ALTER INDEX "customer_profiles_customer_uniq" RENAME TO "customer_profiles_customer_id_key";

-- RenameIndex
ALTER INDEX "customer_profiles_tenant_stage_idx" RENAME TO "customer_profiles_tenant_id_stage_idx";

-- RenameIndex
ALTER INDEX "customer_timelines_tenant_customer_ts_idx" RENAME TO "customer_timelines_tenant_id_customer_id_timestamp_idx";

-- RenameIndex
ALTER INDEX "events_stream_lookup_idx" RENAME TO "events_tenant_id_stream_id_version_idx";

-- RenameIndex
ALTER INDEX "events_tenant_event_id_key" RENAME TO "events_tenant_id_event_id_key";

-- RenameIndex
ALTER INDEX "events_tenant_stream_version_key" RENAME TO "events_tenant_id_stream_id_version_key";

-- RenameIndex
ALTER INDEX "knowledge_articles_source_draft_unique" RENAME TO "knowledge_articles_source_draft_id_key";

-- RenameIndex
ALTER INDEX "knowledge_articles_tenant_source_draft_idx" RENAME TO "knowledge_articles_tenant_id_source_draft_id_idx";

-- RenameIndex
ALTER INDEX "knowledge_drafts_tenant_source_ticket_idx" RENAME TO "knowledge_drafts_tenant_id_source_ticket_id_idx";

-- RenameIndex
ALTER INDEX "lead_read_model_status_idx" RENAME TO "lead_read_model_tenant_id_status_idx";

-- RenameIndex
ALTER INDEX "outbox_events_tenant_event_id_key" RENAME TO "outbox_events_tenant_id_event_id_key";

-- RenameIndex
ALTER INDEX "predictions_tenant_entity_created_idx" RENAME TO "predictions_tenant_id_entity_type_entity_id_created_at_idx";

-- RenameIndex
ALTER INDEX "productivity_proposals_tenant_dedupe_idx" RENAME TO "productivity_proposals_tenant_id_dedupe_key_idx";

-- RenameIndex
ALTER INDEX "productivity_proposals_tenant_status_priority_idx" RENAME TO "productivity_proposals_tenant_id_status_priority_idx";

-- RenameIndex
ALTER INDEX "productivity_proposals_tenant_user_status_idx" RENAME TO "productivity_proposals_tenant_id_user_id_status_idx";
