CREATE TABLE IF NOT EXISTS outbox_events (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  event_id uuid NOT NULL,
  event_type text NOT NULL,
  topic text NOT NULL,
  payload jsonb NOT NULL,
  schema_version integer NOT NULL DEFAULT 1,
  published_at timestamptz NULL,
  retry_count integer NOT NULL DEFAULT 0,
  last_error text NULL,
  idempotency_key text NULL,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  dead_lettered_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS outbox_tenant_idempotency_key_uniq
  ON outbox_events (tenant_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS outbox_pending_idx
  ON outbox_events (tenant_id, created_at)
  WHERE published_at IS NULL AND dead_lettered_at IS NULL;

ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS outbox_events_tenant_isolation ON outbox_events;
CREATE POLICY outbox_events_tenant_isolation ON outbox_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

