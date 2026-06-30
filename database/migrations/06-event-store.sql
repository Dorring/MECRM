CREATE TABLE IF NOT EXISTS event_streams (
  tenant_id uuid NOT NULL,
  stream_id text NOT NULL,
  current_version integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, stream_id)
);

CREATE TABLE IF NOT EXISTS events (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  stream_id text NOT NULL,
  version integer NOT NULL,
  event_id uuid NOT NULL,
  event_type text NOT NULL,
  schema_version integer NOT NULL DEFAULT 1,
  payload jsonb NOT NULL,
  idempotency_key text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, stream_id, version),
  UNIQUE (tenant_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS events_tenant_idempotency_key_uniq
  ON events (tenant_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS events_stream_lookup_idx
  ON events (tenant_id, stream_id, version);

ALTER TABLE event_streams ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_streams FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS event_streams_tenant_isolation ON event_streams;
CREATE POLICY event_streams_tenant_isolation ON event_streams
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS events_tenant_isolation ON events;
CREATE POLICY events_tenant_isolation ON events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

