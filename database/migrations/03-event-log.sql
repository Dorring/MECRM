CREATE TABLE IF NOT EXISTS "event_log" (
  "id" UUID NOT NULL,
  "event_id" UUID NOT NULL,
  "tenant_id" UUID NOT NULL,
  "aggregate_type" TEXT NOT NULL,
  "aggregate_id" UUID NOT NULL,
  "event_type" TEXT NOT NULL,
  "version" INTEGER NOT NULL,
  "ts" TIMESTAMPTZ NOT NULL,
  "payload" JSONB NOT NULL,
  "kafka_topic" TEXT,
  "kafka_partition" INTEGER,
  "kafka_offset" BIGINT,
  CONSTRAINT "event_log_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "event_log_event_id_key" UNIQUE ("event_id"),
  CONSTRAINT "event_log_aggregate_version_key" UNIQUE ("tenant_id", "aggregate_type", "aggregate_id", "version")
);

CREATE INDEX IF NOT EXISTS "event_log_tenant_aggregate_version_idx"
  ON "event_log" ("tenant_id", "aggregate_type", "aggregate_id", "version");

CREATE INDEX IF NOT EXISTS "event_log_tenant_aggregate_ts_idx"
  ON "event_log" ("tenant_id", "aggregate_type", "aggregate_id", "ts");

CREATE INDEX IF NOT EXISTS "event_log_tenant_ts_idx"
  ON "event_log" ("tenant_id", "ts");

ALTER TABLE "event_log" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "event_log" FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "event_log_tenant_isolation" ON "event_log";
CREATE POLICY "event_log_tenant_isolation" ON "event_log"
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

