CREATE TABLE IF NOT EXISTS tenants (
  id uuid PRIMARY KEY,
  name varchar(255) NOT NULL,
  slug varchar(100) NOT NULL,
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  status varchar(50) NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS leads (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  name varchar(255) NOT NULL,
  email varchar(255),
  phone varchar(50),
  company varchar(255),
  source varchar(100),
  status varchar(50) NOT NULL DEFAULT 'new',
  score integer,
  assigned_to uuid,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS leads_tenant_id_idx ON leads (tenant_id);

CREATE TABLE IF NOT EXISTS customers (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  name varchar(255) NOT NULL,
  email varchar(255),
  phone varchar(50),
  company varchar(255),
  segment varchar(100),
  lifetime_value numeric(15,2) NOT NULL DEFAULT 0,
  status varchar(50) NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS customers_tenant_id_idx ON customers (tenant_id);
