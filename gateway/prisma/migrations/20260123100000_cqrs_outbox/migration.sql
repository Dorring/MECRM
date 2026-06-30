CREATE TABLE IF NOT EXISTS "event_streams" (
  "tenant_id" UUID NOT NULL,
  "stream_id" TEXT NOT NULL,
  "current_version" INTEGER NOT NULL DEFAULT 0,
  "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "event_streams_pkey" PRIMARY KEY ("tenant_id","stream_id")
);

CREATE TABLE IF NOT EXISTS "events" (
  "id" UUID NOT NULL,
  "tenant_id" UUID NOT NULL,
  "stream_id" TEXT NOT NULL,
  "version" INTEGER NOT NULL,
  "event_id" UUID NOT NULL,
  "event_type" TEXT NOT NULL,
  "schema_version" INTEGER NOT NULL DEFAULT 1,
  "payload" JSONB NOT NULL,
  "idempotency_key" TEXT,
  "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "events_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "events_tenant_stream_version_key" ON "events"("tenant_id","stream_id","version");
CREATE UNIQUE INDEX IF NOT EXISTS "events_tenant_event_id_key" ON "events"("tenant_id","event_id");
CREATE INDEX IF NOT EXISTS "events_stream_lookup_idx" ON "events"("tenant_id","stream_id","version");

CREATE TABLE IF NOT EXISTS "outbox_events" (
  "id" UUID NOT NULL,
  "tenant_id" UUID NOT NULL,
  "event_id" UUID NOT NULL,
  "event_type" TEXT NOT NULL,
  "topic" TEXT NOT NULL,
  "payload" JSONB NOT NULL,
  "schema_version" INTEGER NOT NULL DEFAULT 1,
  "published_at" TIMESTAMP(3),
  "retry_count" INTEGER NOT NULL DEFAULT 0,
  "last_error" TEXT,
  "idempotency_key" TEXT,
  "next_attempt_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "dead_lettered_at" TIMESTAMP(3),
  "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "outbox_events_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "outbox_events_tenant_event_id_key" ON "outbox_events"("tenant_id","event_id");
CREATE INDEX IF NOT EXISTS "outbox_pending_idx" ON "outbox_events"("tenant_id","created_at") WHERE "published_at" IS NULL AND "dead_lettered_at" IS NULL;

CREATE TABLE IF NOT EXISTS "processed_events" (
  "tenant_id" UUID NOT NULL,
  "event_id" UUID NOT NULL,
  "processed_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "processed_events_pkey" PRIMARY KEY ("tenant_id","event_id")
);

CREATE TABLE IF NOT EXISTS "lead_read_model" (
  "tenant_id" UUID NOT NULL,
  "lead_id" UUID NOT NULL,
  "name" TEXT NOT NULL,
  "email" TEXT,
  "phone" TEXT,
  "company" TEXT,
  "status" TEXT NOT NULL,
  "score" INTEGER,
  "assigned_to" UUID,
  "metadata" JSONB NOT NULL DEFAULT '{}'::jsonb,
  "version" INTEGER NOT NULL DEFAULT 0,
  "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "lead_read_model_pkey" PRIMARY KEY ("tenant_id","lead_id")
);

CREATE INDEX IF NOT EXISTS "lead_read_model_status_idx" ON "lead_read_model"("tenant_id","status");

CREATE TABLE IF NOT EXISTS "deal_pipeline_view" (
  "tenant_id" UUID NOT NULL,
  "stage" TEXT NOT NULL,
  "deal_count" INTEGER NOT NULL DEFAULT 0,
  "total_amount" DECIMAL(15,2) NOT NULL DEFAULT 0,
  "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "deal_pipeline_view_pkey" PRIMARY KEY ("tenant_id","stage")
);

CREATE TABLE IF NOT EXISTS "customer_timeline_view" (
  "tenant_id" UUID NOT NULL,
  "customer_id" UUID NOT NULL,
  "last_event_at" TIMESTAMP(3),
  "open_tickets" INTEGER NOT NULL DEFAULT 0,
  "active_deals" INTEGER NOT NULL DEFAULT 0,
  "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "customer_timeline_view_pkey" PRIMARY KEY ("tenant_id","customer_id")
);

