CREATE TABLE IF NOT EXISTS "aggregate_snapshots" (
  "tenant_id" UUID NOT NULL,
  "aggregate_type" TEXT NOT NULL,
  "aggregate_id" UUID NOT NULL,
  "version" INTEGER NOT NULL,
  "ts" TIMESTAMPTZ NOT NULL,
  "state" JSONB NOT NULL,
  "kafka_topic" TEXT,
  "kafka_partition" INTEGER,
  "kafka_offset" BIGINT,
  "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "aggregate_snapshots_pkey" PRIMARY KEY ("tenant_id", "aggregate_type", "aggregate_id", "version")
);

CREATE INDEX IF NOT EXISTS "aggregate_snapshots_latest_idx"
  ON "aggregate_snapshots" ("tenant_id", "aggregate_type", "aggregate_id", "version" DESC);

CREATE INDEX IF NOT EXISTS "aggregate_snapshots_ts_idx"
  ON "aggregate_snapshots" ("tenant_id", "aggregate_type", "aggregate_id", "ts" DESC);

ALTER TABLE "aggregate_snapshots" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "aggregate_snapshots" FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "aggregate_snapshots_tenant_isolation" ON "aggregate_snapshots";
CREATE POLICY "aggregate_snapshots_tenant_isolation" ON "aggregate_snapshots"
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

