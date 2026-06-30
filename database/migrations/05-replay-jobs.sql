CREATE TABLE IF NOT EXISTS "replay_jobs" (
  "job_id" UUID NOT NULL,
  "tenant_id" UUID NOT NULL,
  "aggregate_type" TEXT NOT NULL,
  "aggregate_id" UUID NOT NULL,
  "mode" TEXT NOT NULL,
  "topic" TEXT NOT NULL,
  "partition" INTEGER NOT NULL DEFAULT 0,
  "start_offset" BIGINT NOT NULL,
  "end_offset" BIGINT,
  "target_time" TIMESTAMPTZ,
  "status" TEXT NOT NULL,
  "started_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "finished_at" TIMESTAMPTZ,
  "events_processed" INTEGER NOT NULL DEFAULT 0,
  "snapshot_used" BOOLEAN NOT NULL DEFAULT false,
  "error" TEXT,
  CONSTRAINT "replay_jobs_pkey" PRIMARY KEY ("job_id")
);

CREATE INDEX IF NOT EXISTS "replay_jobs_tenant_status_idx"
  ON "replay_jobs" ("tenant_id", "status", "started_at" DESC);

CREATE INDEX IF NOT EXISTS "replay_jobs_tenant_aggregate_idx"
  ON "replay_jobs" ("tenant_id", "aggregate_type", "aggregate_id", "started_at" DESC);

ALTER TABLE "replay_jobs" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "replay_jobs" FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "replay_jobs_tenant_isolation" ON "replay_jobs";
CREATE POLICY "replay_jobs_tenant_isolation" ON "replay_jobs"
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

