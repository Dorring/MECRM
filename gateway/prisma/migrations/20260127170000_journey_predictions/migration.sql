CREATE TABLE "customer_timelines" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "customer_id" uuid NOT NULL,
  "event_type" varchar(100) NOT NULL,
  "event_payload" jsonb NOT NULL DEFAULT '{}'::jsonb,
  "timestamp" timestamp NOT NULL,
  "created_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "customer_timelines"
  ADD CONSTRAINT "customer_timelines_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "customer_timelines"
  ADD CONSTRAINT "customer_timelines_customer_id_fkey" FOREIGN KEY ("customer_id") REFERENCES "customers" ("id") ON DELETE CASCADE;

CREATE INDEX "customer_timelines_tenant_customer_ts_idx" ON "customer_timelines" ("tenant_id", "customer_id", "timestamp" DESC);

CREATE TABLE "customer_profiles" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "customer_id" uuid NOT NULL,
  "stage" varchar(50) NOT NULL DEFAULT 'awareness',
  "stage_confidence" double precision NOT NULL DEFAULT 0,
  "stage_updated_at" timestamp NOT NULL DEFAULT now(),
  "features" jsonb NOT NULL DEFAULT '{}'::jsonb,
  "updated_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "customer_profiles"
  ADD CONSTRAINT "customer_profiles_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

ALTER TABLE "customer_profiles"
  ADD CONSTRAINT "customer_profiles_customer_id_fkey" FOREIGN KEY ("customer_id") REFERENCES "customers" ("id") ON DELETE CASCADE;

CREATE UNIQUE INDEX "customer_profiles_customer_uniq" ON "customer_profiles" ("customer_id");
CREATE INDEX "customer_profiles_tenant_customer_idx" ON "customer_profiles" ("tenant_id", "customer_id");
CREATE INDEX "customer_profiles_tenant_stage_idx" ON "customer_profiles" ("tenant_id", "stage");

CREATE TABLE "predictions" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "tenant_id" uuid NOT NULL,
  "entity_type" varchar(50) NOT NULL,
  "entity_id" text NOT NULL,
  "prediction_type" varchar(50) NOT NULL,
  "probability" double precision NOT NULL,
  "risk_level" varchar(20) NOT NULL,
  "explanation" text NOT NULL,
  "features" jsonb NOT NULL DEFAULT '{}'::jsonb,
  "model_version" varchar(50) NOT NULL,
  "created_at" timestamp NOT NULL DEFAULT now()
);

ALTER TABLE "predictions"
  ADD CONSTRAINT "predictions_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants" ("id") ON DELETE CASCADE;

CREATE INDEX "predictions_tenant_entity_created_idx" ON "predictions" ("tenant_id", "entity_type", "entity_id", "created_at" DESC);
