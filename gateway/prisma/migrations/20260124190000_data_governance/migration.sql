ALTER TABLE "users" ADD COLUMN "deleted_at" TIMESTAMP(3);
ALTER TABLE "users" ADD COLUMN "deletion_type" VARCHAR(50);

ALTER TABLE "customers" ADD COLUMN "deleted_at" TIMESTAMP(3);
ALTER TABLE "customers" ADD COLUMN "deletion_type" VARCHAR(50);

CREATE TABLE "data_retention_policies" (
    "id" UUID NOT NULL,
    "tenant_id" UUID NOT NULL,
    "entity_type" TEXT NOT NULL,
    "retention_days" INTEGER NOT NULL,
    "hard_delete" BOOLEAN NOT NULL DEFAULT false,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "data_retention_policies_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "data_retention_policies_tenant_id_entity_type_key" ON "data_retention_policies"("tenant_id", "entity_type");
CREATE INDEX "data_retention_policies_tenant_id_entity_type_idx" ON "data_retention_policies"("tenant_id", "entity_type");

ALTER TABLE "data_retention_policies" ADD CONSTRAINT "data_retention_policies_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
