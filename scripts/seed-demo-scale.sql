\set ON_ERROR_STOP on

-- Idempotent, deterministic, local-only demo-scale data generator.
--
-- Usage from the repository root:
--   docker compose exec -T postgres sh -lc \
--     'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v tenant_id="<tenant UUID>"' \
--     < scripts/seed-demo-scale.sql
--
-- All optional variables with their defaults:
--   customers         200
--   leads            1000
--   deals             600
--   tickets           300
--   knowledge_articles 40
--   workflows          30
--   seed        20260720
--
-- Every record carries source=demo-scale in metadata/tags so the companion
-- clear-demo-scale.sql can safely remove only these rows.
--
-- The generator is idempotent: the same tenant_id + seed + scale values
-- produce the same deterministic UUIDs, so ON CONFLICT DO NOTHING skips
-- already-inserted rows.  Rerun safely after clear, or rerun with a
-- different seed for a fresh dataset.

\if :{?tenant_id}
\else
  \echo 'tenant_id is required'
  \quit
\endif

\if :{?customers}
\else
  \set customers 200
\endif

\if :{?leads}
\else
  \set leads 1000
\endif

\if :{?deals}
\else
  \set deals 600
\endif

\if :{?tickets}
\else
  \set tickets 300
\endif

\if :{?knowledge_articles}
\else
  \set knowledge_articles 40
\endif

\if :{?workflows}
\else
  \set workflows 30
\endif

\if :{?seed}
\else
  \set seed 20260720
\endif

-- ------------------------------------------------------------------
-- Deterministic helper: map an integer index to a pseudo-random
-- value in [0, n) using a multiplicative hash of seed and index.
-- The formula is (seed * A + idx * B) mod n where A and B are
-- large primes.  This keeps output stable across reruns without
-- relying on setseed() global state.
-- ------------------------------------------------------------------

BEGIN;

-- Set the RLS context first so the tenant-existence DO block and all
-- helper functions can read it via current_setting.
SELECT set_config('app.tenant_id', :'tenant_id', true);

-- Verify the tenant row exists (the caller must have created it).
DO $$
DECLARE
  v_tenant_id uuid := current_setting('app.tenant_id')::uuid;
  v_count integer;
BEGIN
  SELECT count(*) INTO v_count FROM tenants WHERE id = v_tenant_id;
  IF v_count = 0 THEN
    RAISE EXCEPTION 'tenant % not found -- create it first or seed the interview-demo fixture', v_tenant_id;
  END IF;
END $$;

-- Parameter table MUST be created before helper functions because the
-- functions reference it via STABLE lookup.  Every psql variable is
-- expanded here (plain SQL, not inside dollar quotes).
CREATE TEMP TABLE dsc ON COMMIT DROP AS
SELECT
  :'tenant_id'::uuid          AS tenant_id,
  :seed::bigint               AS seed,
  :customers::int             AS n_customers,
  :leads::int                 AS n_leads,
  :deals::int                 AS n_deals,
  :tickets::int               AS n_tickets,
  :knowledge_articles::int    AS n_articles,
  :workflows::int             AS n_workflows;

-- ------------------------------------------------------------------
-- Helper functions -- reference the dsc table for tenant_id and seed
-- instead of embedding psql variables inside dollar quotes (which
-- psql does not expand).  Marked STABLE because they read a temp
-- table; this is fine for INSERT ... SELECT usage.
-- ------------------------------------------------------------------

-- Deterministic UUID generator
-- md5(tenant || ':' || namespace || ':' || seed || ':' || idx)::uuid
CREATE OR REPLACE FUNCTION pg_temp.demo_uuid(namespace text, idx bigint)
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT md5(dsc.tenant_id::text || ':' || namespace || ':' || dsc.seed::text || ':' || idx)::uuid
  FROM dsc;
$$;

-- Deterministic pick from array: returns arr[hash(idx,seed) % len + 1]
CREATE OR REPLACE FUNCTION pg_temp.demo_pick(idx bigint, arr text[])
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT arr[1 + ((dsc.seed * 31 + idx * 2654435761) % array_length(arr, 1))]
  FROM dsc;
$$;

-- Deterministic int in [lo, hi]
CREATE OR REPLACE FUNCTION pg_temp.demo_int(idx bigint, lo bigint, hi bigint)
RETURNS bigint LANGUAGE sql STABLE AS $$
  SELECT lo + ((dsc.seed * 31 + idx * 2654435761) % (hi - lo + 1))
  FROM dsc;
$$;

-- Deterministic numeric in [lo, hi) with scale digits
CREATE OR REPLACE FUNCTION pg_temp.demo_numeric(idx bigint, lo numeric, hi numeric, scale_digits int)
RETURNS numeric LANGUAGE sql STABLE AS $$
  SELECT round(
    (lo + ((dsc.seed * 31 + idx * 2654435761) % ((hi - lo) * pow(10, least(scale_digits, 6))::bigint))::numeric
    / pow(10, least(scale_digits, 6))::numeric)::numeric,
    scale_digits
  )
  FROM dsc;
$$;

-- Deterministic timestamp offset: now() - (base + hash(idx) % spread) * interval
CREATE OR REPLACE FUNCTION pg_temp.demo_ago(idx bigint, base_days int, spread_days int)
RETURNS timestamptz LANGUAGE sql STABLE AS $$
  SELECT now() - make_interval(days => base_days + (pg_temp.demo_int(idx, 0, spread_days))::int);
$$;

-- ==================================================================
-- DATA ARRAYS (large enough to avoid obvious repetition at scale)
-- ==================================================================

-- First names (80)
CREATE TEMP TABLE demo_first_names ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'Avery','Riley','Morgan','Jordan','Casey','Taylor','Jamie','Drew','Sam','Alex',
  'Blake','Cameron','Dana','Ellis','Finley','Gray','Harper','Jade','Kai','Lane',
  'Marley','Nico','Oakley','Parker','Quinn','Reese','Sage','Tatum','Uma','Val',
  'Wren','Xavi','Yuki','Zuri','Amari','Brett','Corey','Devon','Emery','Frankie',
  'Gale','Haven','Indigo','Jules','Kerry','Logan','Milan','Noel','Ollie','Peyton',
  'Ray','Skyler','Tory','Uli','Vanya','Winter','Xen','Yael','Zion','Aden',
  'Billie','Charlie','Daryl','Eden','Florin','Geri','Hali','Irie','Jody','Kelly',
  'Lee','Micky','Nicky','Ori','Pat','Remi','Shane','Terry','Vic','Wynn'
]) AS name;

-- Last names (60)
CREATE TEMP TABLE demo_last_names ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'Rivera','Brooks','Park','Nguyen','Wilson','Chen','Morgan','Patel','Lee','Kim',
  'Garcia','Martinez','Anderson','Taylor','Thomas','Jackson','White','Harris',
  'Martin','Thompson','Moore','Allen','Young','Hernandez','King','Wright','Lopez',
  'Hill','Scott','Green','Adams','Baker','Gonzalez','Nelson','Carter','Mitchell',
  'Perez','Roberts','Turner','Phillips','Campbell','Parker','Evans','Edwards',
  'Collins','Stewart','Sanchez','Morris','Rogers','Reed','Cook','Morgan','Bell',
  'Murphy','Bailey','Rivera','Cooper','Richardson','Cox','Howard','Ward'
]) AS name;

-- Company name parts
CREATE TEMP TABLE demo_co_prefix ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'NorthStar','Apex','Quantum','Vertex','Zenith','Nova','Pinnacle','Meridian',
  'Atlas','Horizon','Catalyst','Spectrum','Momentum','Prism','Fusion','Titan',
  'Omega','Nexus','Polaris','Stratus','Crest','Peak','Prime','Core','Edge',
  'Beacon','Harbor','Summit','Frontier','Vanguard','Helix','Matrix','Orbit',
  'Pulse','Ridge','Solar','Lunar','Stellar','Cosmic','Astral','Phoenix','Sage',
  'Iron','Steel','Cedar','Birch','Cypress','Vale','Brook','Stone','River'
]) AS name;

CREATE TEMP TABLE demo_co_suffix ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'Analytics','Health','Freight','Systems','Energy','Retail','Manufacturing',
  'Logistics','Capital','Ventures','Technologies','Solutions','Dynamics',
  'Innovations','Enterprises','Industries','Partners','Holdings','Group',
  'Works','Labs','Consulting','Services','Networks','Digital','Software',
  'Platform','Cloud','Security','Data','AI','Automation','Robotics','Medical',
  'Pharma','Bio','Finance','Insurance','Media','Telecom','Transport','Aerospace',
  'Marine','Mining','Utilities','Agriculture','Education','Hospitality','Legal',
  'RealEstate'
]) AS name;

-- Lead sources
CREATE TEMP TABLE demo_lead_sources ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'webinar','inbound','partner','conference','outbound','referral','social',
  'email_campaign','trade_show','website','cold_call','linkedin','advertisement',
  'community','content_marketing'
]) AS source;

-- Lead statuses
CREATE TEMP TABLE demo_lead_statuses ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'new','contacted','qualified','proposal','unqualified','nurture','converted'
]) AS status;

-- Customer segments
CREATE TEMP TABLE demo_segments ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'enterprise','mid-market','growth','smb','startup'
]) AS segment;

-- Deal stages
CREATE TEMP TABLE demo_deal_stages ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'prospecting','qualification','proposal','negotiation','closed_won',
  'closed_lost','deferred'
]) AS stage;

-- Ticket categories
CREATE TEMP TABLE demo_ticket_categories ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'identity','compliance','automation','billing','support','integration',
  'security','data_export','performance','onboarding','configuration',
  'governance','reporting','access_control','workflow'
]) AS category;

-- Ticket priorities
CREATE TEMP TABLE demo_priorities ON COMMIT DROP AS
SELECT unnest(ARRAY['urgent','high','medium','low']) AS priority;

-- Ticket statuses
CREATE TEMP TABLE demo_ticket_statuses ON COMMIT DROP AS
SELECT unnest(ARRAY['open','in_progress','resolved','closed']) AS status;

-- ==================================================================
-- 1. CUSTOMERS (default 200)
-- ==================================================================

INSERT INTO customers (
  id, tenant_id, name, email, phone, company, segment, lifetime_value,
  status, metadata, created_by, created_at, updated_at
)
SELECT
  pg_temp.demo_uuid('customer', g.i),
  dsc.tenant_id,
  (SELECT name FROM demo_first_names ORDER BY md5(:'tenant_id' || g.i::text) LIMIT 1)
    || ' ' ||
  (SELECT name FROM demo_last_names ORDER BY md5(:'tenant_id' || (g.i * 2)::text) LIMIT 1),
  pg_temp.demo_pick(g.i, ARRAY['alex','riley','jordan','morgan','casey','taylor','sam',
    'jamie','drew','blake','quinn','sage','wren','finley','harper','kai','lane',
    'noor','zane','emery']) || '.' ||
  pg_temp.demo_pick(g.i * 3, ARRAY['chen','rivera','brooks','patel','lee','kim',
    'morgan','garcia','nguyen','wilson']) || '@' ||
  pg_temp.demo_pick(g.i * 5, ARRAY['demo','example','corp','biz','mail']) || '.local',
  '+1-555-' || lpad(pg_temp.demo_int(g.i, 100, 9999)::text, 4, '0'),
  pg_temp.demo_pick(g.i, ARRAY(SELECT name FROM demo_co_prefix))
    || ' ' ||
  pg_temp.demo_pick(g.i * 7, ARRAY(SELECT name FROM demo_co_suffix)),
  CASE
    WHEN g.i <= (dsc.n_customers * 0.60) THEN 'enterprise'
    WHEN g.i <= (dsc.n_customers * 0.82) THEN 'mid-market'
    WHEN g.i <= (dsc.n_customers * 0.94) THEN 'growth'
    ELSE 'smb'
  END,
  pg_temp.demo_numeric(g.i, 5000, 250000, 2),
  CASE
    WHEN g.i <= (dsc.n_customers * 0.60) THEN 'active'
    WHEN g.i <= (dsc.n_customers * 0.85) THEN 'active'
    WHEN g.i <= (dsc.n_customers * 0.95) THEN 'at_risk'
    ELSE 'churned'
  END,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'seed', dsc.seed,
    'health', CASE
      WHEN g.i <= (dsc.n_customers * 0.60) THEN 'healthy'
      WHEN g.i <= (dsc.n_customers * 0.85) THEN 'watch'
      ELSE 'risk'
    END,
    'generated_at', now()
  ),
  NULL,
  pg_temp.demo_ago(g.i, 60, 365),
  pg_temp.demo_ago(g.i, 3, 60)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_customers) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 2. CUSTOMER PROFILES
-- ==================================================================

INSERT INTO customer_profiles (
  id, tenant_id, customer_id, stage, stage_confidence, stage_updated_at,
  features, updated_at
)
SELECT
  pg_temp.demo_uuid('customer_profile', g.i),
  dsc.tenant_id,
  pg_temp.demo_uuid('customer', g.i),
  CASE
    WHEN g.i <= (dsc.n_customers * 0.15) THEN 'onboarding'
    WHEN g.i <= (dsc.n_customers * 0.40) THEN 'activation'
    WHEN g.i <= (dsc.n_customers * 0.70) THEN 'adoption'
    WHEN g.i <= (dsc.n_customers * 0.90) THEN 'expansion'
    ELSE 'renewal'
  END,
  pg_temp.demo_numeric(g.i, 0.55, 0.98, 2)::double precision,
  now(),
  jsonb_build_object(
    'source', 'demo-scale',
    'health', CASE
      WHEN g.i <= (dsc.n_customers * 0.60) THEN 'healthy'
      WHEN g.i <= (dsc.n_customers * 0.85) THEN 'watch'
      ELSE 'risk'
    END
  ),
  now()
FROM dsc
CROSS JOIN generate_series(1, dsc.n_customers) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 3. LEADS (default 1000)
-- ==================================================================

INSERT INTO leads (
  id, tenant_id, name, email, phone, company, source, status, score,
  assigned_to, metadata, created_by, created_at, updated_at
)
SELECT
  pg_temp.demo_uuid('lead', g.i),
  dsc.tenant_id,
  (SELECT name FROM demo_first_names ORDER BY md5(:'tenant_id' || 'lead' || g.i::text) LIMIT 1)
    || ' ' ||
  (SELECT name FROM demo_last_names ORDER BY md5(:'tenant_id' || 'lead' || (g.i * 3)::text) LIMIT 1),
  pg_temp.demo_pick(g.i, ARRAY['contact','hello','info','sales','admin','support',
    'bizdev','growth','partnerships','office']) || '.' ||
  pg_temp.demo_pick(g.i * 11, ARRAY['doe','smith','johnson','williams','brown',
    'davis','miller','wilson','moore','taylor','anderson','thomas','jackson',
    'white','harris']) || '@' ||
  pg_temp.demo_pick(g.i * 13, ARRAY['prospect','lead','biz','corp']) || '.example',
  '+1-555-' || lpad(pg_temp.demo_int(g.i, 200, 9999)::text, 4, '0'),
  pg_temp.demo_pick(g.i * 17, ARRAY(SELECT name FROM demo_co_prefix))
    || ' ' ||
  pg_temp.demo_pick(g.i * 19, ARRAY(SELECT name FROM demo_co_suffix)),
  pg_temp.demo_pick(g.i, ARRAY(SELECT source FROM demo_lead_sources)),
  CASE
    WHEN g.i <= (dsc.n_leads * 0.30) THEN 'new'
    WHEN g.i <= (dsc.n_leads * 0.55) THEN 'contacted'
    WHEN g.i <= (dsc.n_leads * 0.75) THEN 'qualified'
    WHEN g.i <= (dsc.n_leads * 0.88) THEN 'proposal'
    WHEN g.i <= (dsc.n_leads * 0.94) THEN 'nurture'
    ELSE 'unqualified'
  END,
  pg_temp.demo_int(g.i, 10, 99)::int,
  NULL,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'seed', dsc.seed,
    'scenario', pg_temp.demo_pick(g.i, ARRAY[
      'AI ops discovery', 'compliance automation eval', 'partner referral follow-up',
      'governance workflow fit', 'data quality assessment', 'security audit prep',
      'automation pilot interest', 'renewal risk review', 'expansion opportunity',
      'integration evaluation', 'cost optimization', 'vendor consolidation'
    ]),
    'generated_at', now()
  ),
  NULL,
  pg_temp.demo_ago(g.i, 4, 120),
  pg_temp.demo_ago(g.i, 1, 30)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_leads) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 4. DEALS (default 600)
-- ==================================================================

INSERT INTO deals (
  id, tenant_id, name, customer_id, stage, amount, currency, probability,
  expected_close_date, actual_close_date, won, assigned_to, metadata,
  created_by, created_at, updated_at
)
SELECT
  pg_temp.demo_uuid('deal', g.i),
  dsc.tenant_id,
  pg_temp.demo_pick(g.i, ARRAY(SELECT name FROM demo_co_prefix))
    || ' ' ||
  pg_temp.demo_pick(g.i * 23, ARRAY[
    'AI Governance Expansion', 'Compliance Evidence Pilot',
    'Workflow Automation Renewal', 'Data Quality Rollout',
    'Audit Export Add-on', 'Security Suite Upgrade',
    'Support AI Integration', 'Analytics Platform',
    'Identity Management', 'Knowledge Base Migration',
    'Automation Studio', 'Reporting Dashboard',
    'GDPR Toolkit', 'Tenant Isolation Audit',
    'Predictive Scoring Module', 'Chat Intelligence',
    'Multi-Agent Orchestration', 'Policy Engine',
    'Insight Pipeline', 'Governance Framework'
  ]),
  pg_temp.demo_uuid('customer', 1 + ((g.i - 1) % dsc.n_customers)),
  CASE
    WHEN g.i <= (dsc.n_deals * 0.25) THEN 'prospecting'
    WHEN g.i <= (dsc.n_deals * 0.45) THEN 'qualification'
    WHEN g.i <= (dsc.n_deals * 0.62) THEN 'proposal'
    WHEN g.i <= (dsc.n_deals * 0.76) THEN 'negotiation'
    WHEN g.i <= (dsc.n_deals * 0.89) THEN 'closed_won'
    WHEN g.i <= (dsc.n_deals * 0.97) THEN 'closed_lost'
    ELSE 'deferred'
  END,
  pg_temp.demo_numeric(g.i, 5000, 200000, 2),
  'USD',
  CASE
    WHEN g.i <= (dsc.n_deals * 0.25) THEN pg_temp.demo_int(g.i, 5, 25)::int
    WHEN g.i <= (dsc.n_deals * 0.45) THEN pg_temp.demo_int(g.i, 20, 45)::int
    WHEN g.i <= (dsc.n_deals * 0.62) THEN pg_temp.demo_int(g.i, 40, 70)::int
    WHEN g.i <= (dsc.n_deals * 0.76) THEN pg_temp.demo_int(g.i, 60, 90)::int
    WHEN g.i <= (dsc.n_deals * 0.89) THEN 100
    ELSE 0
  END,
  current_date + make_interval(days => pg_temp.demo_int(g.i, -30, 180)::int),
  CASE
    WHEN g.i > (dsc.n_deals * 0.76) AND g.i <= (dsc.n_deals * 0.97)
    THEN current_date + make_interval(days => pg_temp.demo_int(g.i, -180, -1)::int)
    ELSE NULL
  END,
  CASE
    WHEN g.i > (dsc.n_deals * 0.76) AND g.i <= (dsc.n_deals * 0.89) THEN true
    WHEN g.i > (dsc.n_deals * 0.89) AND g.i <= (dsc.n_deals * 0.97) THEN false
    ELSE NULL
  END,
  NULL,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'seed', dsc.seed,
    'scenario', CASE
      WHEN g.i > (dsc.n_deals * 0.89) AND g.i <= (dsc.n_deals * 0.94)
      THEN 'high_value_renewal_concession'
      WHEN g.i > (dsc.n_deals * 0.94) AND g.i <= (dsc.n_deals * 0.97)
      THEN 'lost_to_competitor'
      WHEN g.i > (dsc.n_deals * 0.97)
      THEN 'deferred_budget_cycle'
      ELSE 'standard_pipeline'
    END,
    'generated_at', now()
  ),
  NULL,
  pg_temp.demo_ago(g.i, 20, 180),
  pg_temp.demo_ago(g.i, 3, 45)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_deals) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 5. TICKETS (default 300)
-- ==================================================================

INSERT INTO tickets (
  id, tenant_id, subject, description, customer_id, priority, status,
  category, assigned_to, sla_due_at, resolved_at, resolution, metadata,
  created_by, created_at, updated_at
)
SELECT
  pg_temp.demo_uuid('ticket', g.i),
  dsc.tenant_id,
  pg_temp.demo_pick(g.i, ARRAY[
    'SSO provisioning follow-up', 'Audit evidence export request',
    'Workflow reminder configuration', 'API rate limit increase',
    'Data export timeout', 'Compliance report generation',
    'Role permission update', 'Knowledge article review',
    'Automation trigger failure', 'Dashboard loading issue',
    'Tenant-scoped filter verification', 'GDPR deletion request',
    'Bulk import validation', 'Webhook delivery delay',
    'Approval workflow stuck', 'Search index rebuild',
    'Notification template update', 'SLA breach investigation',
    'Integration health check', 'Security scan follow-up',
    'Model prediction review', 'Multi-agent handoff debug',
    'Customer merge request', 'Billing cycle adjustment',
    'Retention policy update', 'Access token rotation',
    'Audit log export', 'Custom field migration',
    'Performance degradation report', 'Configuration drift alert'
  ]),
  'Demo-scale ticket #' || g.i || ': ' ||
  pg_temp.demo_pick(g.i * 2, ARRAY[
    'Requires tenant-scoped review before action.',
    'Evidence must be verified against policy.',
    'Agent handoff needs approval boundary check.',
    'SLA-sensitive; route to appropriate specialist.',
    'Document findings before resolution.',
    'Cross-reference with knowledge articles.',
    'Verify tenant isolation before data access.',
    'Check against current governance policies.',
    'Validate against retention schedule.',
    'Confirm no cross-tenant data exposure.'
  ]),
  pg_temp.demo_uuid('customer', 1 + ((g.i - 1) % dsc.n_customers)),
  CASE
    WHEN g.i <= (dsc.n_tickets * 0.10) THEN 'urgent'
    WHEN g.i <= (dsc.n_tickets * 0.32) THEN 'high'
    WHEN g.i <= (dsc.n_tickets * 0.70) THEN 'medium'
    ELSE 'low'
  END,
  CASE
    WHEN g.i <= (dsc.n_tickets * 0.33) THEN 'open'
    WHEN g.i <= (dsc.n_tickets * 0.60) THEN 'in_progress'
    WHEN g.i <= (dsc.n_tickets * 0.92) THEN 'resolved'
    ELSE 'closed'
  END,
  pg_temp.demo_pick(g.i, ARRAY(SELECT category FROM demo_ticket_categories)),
  NULL,
  CASE
    WHEN g.i <= (dsc.n_tickets * 0.92)
    THEN now() + make_interval(hours => pg_temp.demo_int(g.i, 1, 72)::int)
    ELSE NULL
  END,
  CASE
    WHEN g.i > (dsc.n_tickets * 0.60) AND g.i <= (dsc.n_tickets * 0.95)
    THEN now() - make_interval(hours => pg_temp.demo_int(g.i, 1, 168)::int)
    ELSE NULL
  END,
  CASE
    WHEN g.i > (dsc.n_tickets * 0.60) AND g.i <= (dsc.n_tickets * 0.92)
    THEN pg_temp.demo_pick(g.i, ARRAY[
      'Published approval-aware resolution guide.',
      'Tenant-scoped fix applied and verified.',
      'Knowledge article created from resolution.',
      'Configuration updated with governance guard.',
      'Policy-compliant workaround deployed.',
      'Root cause identified and documented.'
    ])
    ELSE NULL
  END,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'seed', dsc.seed,
    'sla_risk', CASE
      WHEN g.i <= (dsc.n_tickets * 0.10) THEN 'breached'
      WHEN g.i <= (dsc.n_tickets * 0.25) THEN 'at_risk'
      ELSE 'on_track'
    END,
    'generated_at', now()
  ),
  NULL,
  pg_temp.demo_ago(g.i, 5, 90),
  pg_temp.demo_ago(g.i, 2, 30)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_tickets) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 6. KNOWLEDGE ARTICLES (default 40)
-- ==================================================================

CREATE TEMP TABLE demo_article_templates ON COMMIT DROP AS
SELECT * FROM (VALUES
  ('Enterprise renewal discount approval policy',
   E'# Enterprise renewal discount approval policy\n\nRenewals above 15 percent discount or 25000 USD ACV require human approval. A Sales Recommendation Agent may prepare the rationale but cannot apply the discount. Attach context, forecast impact, and policy citation before routing.',
   'renewal','discount','approval'),
  ('Tenant-scoped evidence handling guide',
   E'# Tenant-scoped evidence handling guide\n\nAgents may retrieve and summarize evidence only for the authenticated tenant. Before an export, the Compliance Guard verifies the tenant context, scope, and policy outcome. Cross-tenant requests are denied without touching the export tool.',
   'tenant-safety','evidence','compliance'),
  ('Support handoff for renewal risk signals',
   E'# Support handoff for renewal risk signals\n\nWhen Sales identifies a renewal at risk, Support retrieves open-ticket context and applicable guidance. It produces a cited internal handoff; it cannot send a customer message or modify ticket status.',
   'support','renewal','handoff'),
  ('Approval and audit trail requirements',
   E'# Approval and audit trail requirements\n\nHigh-impact commercial actions require an approval record with context, reviewer decision, timestamp, and reason. The Governance Decision retains evidence references and tool outcomes for explainability.',
   'governance','audit','human-in-the-loop'),
  ('Renewal health score interpretation',
   E'# Renewal health score interpretation\n\nA renewal health score combines adoption, support impact, executive engagement, and forecast confidence. Scores below 0.70 indicate risk; they do not authorize price changes by themselves.',
   'renewal','health-score','sales'),
  ('RLS multi-tenant query patterns',
   E'# RLS multi-tenant query patterns\n\nAll queries run under SET LOCAL app.tenant_id. The RLS policy appends WHERE tenant_id = current_setting(...) automatically. Never construct raw queries that bypass this filter.',
   'rls','tenant-safety','database'),
  ('Data export compliance checklist',
   E'# Data export compliance checklist\n\n1. Confirm authenticated tenant. 2. Verify export scope against policy. 3. Create approval for sensitive exports. 4. Execute only after authorized decision. 5. Log evidence trail.',
   'data-export','compliance','governance'),
  ('Automation governance boundaries',
   E'# Automation governance boundaries\n\nAutomation policies are always draft or dry-run until a human activates them. A simulation previews impact but never mutates data. Activation is a distinct approval action.',
   'automation','governance','safety'),
  ('Customer support response templates',
   E'# Customer support response templates\n\nDrafts prepared by the Support Knowledge Agent are review-only. A human must approve before sending. Templates include scoped evidence citations and policy references.',
   'support','templates','review'),
  ('Agent collaboration contract',
   E'# Agent collaboration contract\n\nMulti-agent workflows use correlation IDs to link tasks, events, and decisions. Each agent owns a specialist step and produces a bounded handoff to the next. No agent acts outside its declared capability set.',
   'multi-agent','collaboration','correlation'),
  ('Governance decision records',
   E'# Governance decision records\n\nEvery governed action creates an agent_decision row with input_context, reasoning, evidence, tool_calls, and approval_id. These are the audit backbone for AI actions.',
   'governance','decisions','audit'),
  ('Kill switch operational guide',
   E'# Kill switch operational guide\n\nThe kill switch operates at global, tenant, and agent scope. Pausing a tenant partition prevents new task dispatch. Existing in-flight tasks complete or timeout.',
   'kill-switch','operations','safety'),
  ('OPA policy authoring guide',
   E'# OPA policy authoring guide\n\nRego policies enforce RBAC, ABAC, and tenant-boundary rules. Each policy is versioned and tested. Changes require policy review before deployment.',
   'opa','policy','authoring'),
  ('GDPR data erasure workflow',
   E'# GDPR data erasure workflow\n\nErasure requests follow a governed pipeline: verify identity, scope data, create approval, execute erasure, record evidence. Hard vs soft delete is tenant-configurable.',
   'gdpr','erasure','compliance'),
  ('Agent telemetry dashboard guide',
   E'# Agent telemetry dashboard guide\n\nTelemetry tracks task counts, decision outcomes, approval rates, and SLA performance per agent type. Dashboards are tenant-scoped and refreshed from agent_events and agent_decisions.',
   'telemetry','dashboard','observability'),
  ('Predictive scoring model overview',
   E'# Predictive scoring model overview\n\nPredictions provide probabilistic signals (expansion propensity, renewal risk, churn likelihood). They are inputs to human decision-making, never autonomous authorizations.',
   'predictions','scoring','models'),
  ('Workflow automation studio guide',
   E'# Workflow automation studio guide\n\nThe Automation Studio converts natural-language rules to compiled JSON workflows. Every workflow has a simulation step and requires activation approval before live execution.',
   'automation','studio','workflows'),
  ('Customer health score methodology',
   E'# Customer health score methodology\n\nHealth scores combine product usage, support volume, NPS, payment history, and engagement metrics. Healthy (>0.80), Watch (0.50-0.80), Risk (<0.50).',
   'customer-health','scoring','methodology'),
  ('Deal pipeline analytics guide',
   E'# Deal pipeline analytics guide\n\nPipeline analytics show stage distribution, conversion rates, win/loss reasons, and forecast accuracy. Data is refreshed from the deal_pipeline_view read model.',
   'deals','pipeline','analytics'),
  ('Security event response playbook',
   E'# Security event response playbook\n\nSecurity events are classified by severity and type. Responses follow a triage -> investigate -> contain -> remediate -> review cycle with full audit trail.',
   'security','response','playbook'),
  ('Integration health monitoring',
   E'# Integration health monitoring\n\nExternal integrations are monitored via webhook health checks, circuit breaker state, and retry queue depth. Degraded integrations trigger a governed incident workflow.',
   'integration','monitoring','health'),
  ('Knowledge article lifecycle',
   E'# Knowledge article lifecycle\n\nArticles start as drafts sourced from ticket resolutions or conversation summaries. They progress through review -> approve -> publish -> index. Reuse count tracks impact.',
   'knowledge','lifecycle','publishing'),
  ('Approval workflow configuration',
   E'# Approval workflow configuration\n\nApproval workflows define request types, required approvers, expiration windows, and escalation paths. Each workflow is tenant-configurable and policy-governed.',
   'approval','workflow','configuration'),
  ('API rate limiting and quotas',
   E'# API rate limiting and quotas\n\nRate limits are tenant-scoped and configurable. The gateway enforces limits via Redis counters. Quota exhaustion triggers a governed notification, not a hard block.',
   'api','rate-limiting','quotas'),
  ('WebSocket event subscription guide',
   E'# WebSocket event subscription guide\n\nThe gateway exposes a /ws endpoint for real-time event streaming. Subscriptions are tenant-scoped. Events include deal updates, ticket changes, and agent task progress.',
   'websocket','events','realtime'),
  ('Compliance evidence packaging',
   E'# Compliance evidence packaging\n\nEvidence packages bundle agent decisions, approval records, and policy evaluations for a specified scope and time window. Packages are immutable and timestamped for audit.',
   'compliance','evidence','packaging'),
  ('Multi-agent trace debugging',
   E'# Multi-agent trace debugging\n\nDebug a collaboration trace by following the correlation ID through agent_tasks -> agent_events -> agent_decisions. Each handoff records the source agent, target agent, and evidence cited.',
   'multi-agent','debugging','trace'),
  ('Discount approval threshold matrix',
   E'# Discount approval threshold matrix\n\nDiscount thresholds vary by deal size and customer segment. Enterprise deals above 15% require VP approval. Mid-market above 10% requires manager approval. All require policy citation.',
   'discount','approval','thresholds'),
  ('Data retention schedule reference',
   E'# Data retention schedule reference\n\nRetention policies are entity-type specific. Audit logs: 7 years. Agent decisions: 3 years. Customer data: configurable per tenant. Hard-delete schedules trigger after soft-delete grace period.',
   'retention','schedule','reference'),
  ('Cross-tenant access prevention',
   E'# Cross-tenant access prevention\n\nThe RLS policy, OPA rules, and agent context checks form three layers of tenant isolation. Any cross-tenant access attempt is blocked at the earliest layer and logged as a security event.',
   'tenant-safety','cross-tenant','prevention'),
  ('Circuit breaker patterns for agents',
   E'# Circuit breaker patterns for agents\n\nAgent task dispatch uses circuit breakers: after N consecutive failures, the circuit opens and tasks are queued. Half-open state allows a single probe. Full recovery resets the breaker.',
   'circuit-breaker','resilience','agents'),
  ('Prompt and evidence separation',
   E'# Prompt and evidence separation\n\nAgent decisions store reasoning and evidence, never raw prompts. This prevents prompt injection persistence and keeps the governance record free of model internals.',
   'prompts','evidence','separation'),
  ('Customer timeline event catalog',
   E'# Customer timeline event catalog\n\nTimeline events include: business_review_completed, support_case_opened, automation_pilot_started, data_quality_review, contract_renewed, expansion_closed, churn_risk_escalated.',
   'timeline','events','catalog'),
  ('Forecast confidence bands',
   E'# Forecast confidence bands\n\nDeal forecasts include confidence bands: commit (>=90%), likely (70-89%), upside (50-69%), pipeline (<50%). Bands are recalculated nightly from stage, age, and engagement signals.',
   'forecast','confidence','deals'),
  ('Agent capability registry',
   E'# Agent capability registry\n\nEach agent declares capabilities as a JSON array. The orchestrator matches tasks to agents by capability intersection. An agent cannot execute actions outside its declared set.',
   'agents','capabilities','registry'),
  ('SLA calculation and reporting',
   E'# SLA calculation and reporting\n\nSLA is measured from ticket creation to resolution, excluding paused intervals. Breach thresholds: urgent=4h, high=8h, medium=24h, low=72h. Reports are tenant-filtered.',
   'sla','calculation','reporting'),
  ('Notification template variables',
   E'# Notification template variables\n\nTemplates use scoped variables: {{tenant.name}}, {{ticket.subject}}, {{agent.decision}}. Variables are resolved at send time with tenant context. Never embed secrets in templates.',
   'notifications','templates','variables'),
  ('Bulk operation governance',
   E'# Bulk operation governance\n\nBulk imports, exports, and updates require a governance pre-check: scope validation, impact preview, approval for high-volume changes. Results include counts and any blocked rows.',
   'bulk','operations','governance'),
  ('Webhook signature verification',
   E'# Webhook signature verification\n\nInbound webhooks are verified via HMAC-SHA256 signature. The shared secret is tenant-scoped and rotated on schedule. Failed verification triggers a security event.',
   'webhooks','signature','security'),
  ('Agent decision replay for audit',
   E'# Agent decision replay for audit\n\nDecision records can be replayed to reconstruct the exact evidence, tool calls, and policy evaluations that led to an outcome. Replay is read-only and tenant-scoped.',
   'audit','replay','decisions')
) AS t(title, content, tag_one, tag_two, tag_three);

-- Number the templates 1..40 so we can pick deterministically without
-- replacement.  For n_articles <= 40 this guarantees distinct articles;
-- for n_articles > 40 the pool cycles.
CREATE TEMP TABLE demo_article_indexed ON COMMIT DROP AS
SELECT *, row_number() OVER () AS rn
FROM demo_article_templates;

INSERT INTO knowledge_articles (
  id, tenant_id, source_draft_id, title, content, tags, reuse_count,
  last_accessed_at, created_at, updated_at
)
SELECT
  pg_temp.demo_uuid('knowledge_article', g.i),
  dsc.tenant_id,
  NULL,
  t.title,
  t.content,
  jsonb_build_array(t.tag_one, t.tag_two, t.tag_three, 'demo-scale'),
  pg_temp.demo_int(g.i, 0, 15)::int,
  pg_temp.demo_ago(g.i, 1, 30),
  pg_temp.demo_ago(g.i, 20, 180),
  pg_temp.demo_ago(g.i, 3, 90)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_articles) AS g(i)
CROSS JOIN LATERAL (
  SELECT *
  FROM demo_article_indexed
  WHERE rn = 1 + ((g.i - 1) % (SELECT count(*) FROM demo_article_indexed))
) AS t
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 7. AI AGENTS (5 agents for demo-scale)
-- ==================================================================

INSERT INTO ai_agents (
  id, name, type, description, capabilities, config, is_active, created_at, updated_at
)
SELECT v.id,
       v.base_name || ' ' || dsc.seed::text,
       v.type, v.description, v.capabilities::jsonb,
       (v.config::jsonb || jsonb_build_object('tenant_id', dsc.tenant_id)),
       true, now(), now()
FROM dsc
CROSS JOIN (
  VALUES
    (pg_temp.demo_uuid('ai_agent', 1), 'Sales Recommendation Agent (Demo-Scale', 'sales',
     'Offline demo-scale agent: prepares commercial recommendations with handoff to Support for evidence retrieval.',
     '["leads:score","deals:recommend","approvals:request","knowledge:retrieve"]',
     '{"source":"demo-scale","mode":"offline-fixture","dataset_version":"1.0.0"}'),
    (pg_temp.demo_uuid('ai_agent', 2), 'Support Knowledge Agent (Demo-Scale', 'support',
     'Offline demo-scale agent: retrieves knowledge articles and prepares cited handoffs.',
     '["tickets:triage","knowledge:retrieve","draft:prepare","handoff:create"]',
     '{"source":"demo-scale","mode":"offline-fixture","dataset_version":"1.0.0"}'),
    (pg_temp.demo_uuid('ai_agent', 3), 'Compliance Guard Agent (Demo-Scale', 'compliance',
     'Offline demo-scale agent: evaluates policy, blocks cross-tenant access, gates approvals.',
     '["evidence:inspect","policy:evaluate","export:guard","approval:verify"]',
     '{"source":"demo-scale","mode":"offline-fixture","dataset_version":"1.0.0"}'),
    (pg_temp.demo_uuid('ai_agent', 4), 'Analytics Forecast Agent (Demo-Scale', 'analytics',
     'Offline demo-scale agent: generates predictive health scores and pipeline forecasts.',
     '["scoring:predict","forecast:generate","risk:assess","report:compile"]',
     '{"source":"demo-scale","mode":"offline-fixture","dataset_version":"1.0.0"}'),
    (pg_temp.demo_uuid('ai_agent', 5), 'Automation Orchestrator Agent (Demo-Scale', 'automation',
     'Offline demo-scale agent: simulates workflow automation policies and routes to approval.',
     '["automation:simulate","workflow:compile","approval:route","policy:preview"]',
     '{"source":"demo-scale","mode":"offline-fixture","dataset_version":"1.0.0"}')
) AS v(id, base_name, type, description, capabilities, config)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 8. WORKFLOWS, AGENT TASKS, AGENT EVENTS, AGENT DECISIONS
--    (default 30 workflows, each a multi-step collaboration trace)
-- ==================================================================

-- Each workflow has:
--   - 2-4 agent_tasks (one per specialist step)
--   - 1-3 agent_events per task
--   - 1-2 agent_decisions per workflow
--   - Optionally 1 approval

-- Workflow distribution:
--   Type A (1-10):  Sales -> Support -> Compliance -> approval (completed)
--   Type B (11-18): approval pending (workflow in progress, awaiting human)
--   Type C (19-25): policy denied / tenant-boundary block
--   Type D (26-30): degraded / retriable

-- Helper tables for workflow steps
CREATE TEMP TABLE demo_wf_sales_tasks ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'renewal_discount_recommendation',
  'expansion_opportunity_assessment',
  'pipeline_health_review',
  'lead_qualification_scoring',
  'deal_forecast_update'
]) AS task_type;

CREATE TEMP TABLE demo_wf_support_tasks ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'renewal_policy_retrieval',
  'knowledge_article_search',
  'ticket_context_gathering',
  'support_handoff_preparation',
  'evidence_compilation'
]) AS task_type;

CREATE TEMP TABLE demo_wf_compliance_tasks ON COMMIT DROP AS
SELECT unnest(ARRAY[
  'renewal_policy_review',
  'tenant_boundary_verification',
  'approval_requirement_check',
  'export_scope_validation',
  'governance_decision_record'
]) AS task_type;

-- ---------- TYPE A: Sales -> Support -> Compliance -> approval (10 workflows) ----------

-- agent_tasks for type A (workflows 1-10)
INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', g.i * 10 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 1),
  (SELECT task_type FROM demo_wf_sales_tasks ORDER BY md5(:'tenant_id' || 'wfa_sales' || g.i::text) LIMIT 1),
  jsonb_build_object(
    'workflow_title', pg_temp.demo_pick(g.i, ARRAY[
      'Enterprise renewal discount review', 'Expansion governance check',
      'Pipeline health triage', 'Lead qualification with compliance',
      'Deal forecast with approval gate', 'Multi-agent renewal handoff',
      'Governed commercial review', 'Compliance-aware proposal',
      'Evidence-backed recommendation', 'Policy-gated expansion'
    ]),
    'scenario', 'demo-scale-type-a',
    'goal', 'prepare a governed commercial recommendation'
  ),
  jsonb_build_object(
    'summary', 'Prepared a commercial recommendation and routed policy evidence retrieval to Support.',
    'handoff_to', 'Support Knowledge Agent (Demo-Scale)',
    'next_task', 'evidence_retrieval'
  ),
  'completed', 4 + (g.i % 3),
  pg_temp.demo_ago(g.i, 6, 24),
  pg_temp.demo_ago(g.i, 4, 20),
  pg_temp.demo_uuid('correlation', g.i),
  pg_temp.demo_ago(g.i, 7, 25)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', g.i * 10 + 2),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 2),
  (SELECT task_type FROM demo_wf_support_tasks ORDER BY md5(:'tenant_id' || 'wfa_support' || g.i::text) LIMIT 1),
  jsonb_build_object(
    'workflow_title', 'Continuing from Sales handoff',
    'scenario', 'demo-scale-type-a',
    'query', 'enterprise renewal discount approval policy',
    'goal', 'retrieve tenant-scoped policy evidence and prepare cited handoff'
  ),
  jsonb_build_object(
    'summary', 'Retrieved the approval policy and support handoff guidance with tenant-scoped citations.',
    'handoff_to', 'Compliance Guard Agent (Demo-Scale)',
    'next_task', 'policy_review',
    'citation_article_ids', jsonb_build_array(
      pg_temp.demo_uuid('knowledge_article', 1 + ((g.i - 1) % dsc.n_articles)),
      pg_temp.demo_uuid('knowledge_article', 1 + ((g.i * 7) % dsc.n_articles))
    )
  ),
  'completed', 3 + (g.i % 3),
  pg_temp.demo_ago(g.i, 5, 22),
  pg_temp.demo_ago(g.i, 3, 18),
  pg_temp.demo_uuid('correlation', g.i),
  pg_temp.demo_ago(g.i, 6, 23)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', g.i * 10 + 3),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 3),
  (SELECT task_type FROM demo_wf_compliance_tasks ORDER BY md5(:'tenant_id' || 'wfa_compliance' || g.i::text) LIMIT 1),
  jsonb_build_object(
    'workflow_title', 'Continuing from Support handoff',
    'scenario', 'demo-scale-type-a',
    'goal', 'verify approval requirement and restrict execution'
  ),
  jsonb_build_object(
    'summary', 'Verified the high-impact action requires human approval; no automatic execution occurred.',
    'handoff_to', 'Human approver',
    'approval_id', pg_temp.demo_uuid('approval_a', g.i)
  ),
  'completed', 2 + (g.i % 2),
  pg_temp.demo_ago(g.i, 4, 20),
  pg_temp.demo_ago(g.i, 2, 15),
  pg_temp.demo_uuid('correlation', g.i),
  pg_temp.demo_ago(g.i, 5, 21)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- agent_events for type A workflows
INSERT INTO agent_events (
  id, tenant_id, agent_id, task_id, event_type, action_type, target_entity,
  target_id, reasoning, confidence, is_approved, requires_approval, metadata,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_event', g.i * 100 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 1),
  pg_temp.demo_uuid('agent_task', g.i * 10 + 1),
  'crm.agents.handoff_prepared',
  'deals:discount_recommend',
  'deal',
  pg_temp.demo_uuid('deal', 1 + ((g.i - 1) % dsc.n_deals)),
  'Sales prepared a governed recommendation and delegated policy evidence retrieval before any high-impact action.',
  0.82 + (g.i % 15)::numeric / 100,
  NULL, true,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'mode', 'offline-fixture',
    'workflow_title', 'demo-scale-type-a',
    'workflow_stage', 'sales_recommendation',
    'correlation_id', pg_temp.demo_uuid('correlation', g.i)
  ),
  pg_temp.demo_ago(g.i, 4, 20)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_events (
  id, tenant_id, agent_id, task_id, event_type, action_type, target_entity,
  target_id, reasoning, confidence, is_approved, requires_approval, metadata,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_event', g.i * 100 + 2),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 2),
  pg_temp.demo_uuid('agent_task', g.i * 10 + 2),
  'crm.agents.evidence_retrieved',
  'knowledge:retrieve',
  'knowledge',
  pg_temp.demo_uuid('knowledge_article', 1 + ((g.i - 1) % dsc.n_articles)),
  'Support retrieved the tenant-scoped approval policy and handed cited evidence to Compliance; no commercial action was performed.',
  0.88 + (g.i % 10)::numeric / 100,
  NULL, false,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'mode', 'offline-fixture',
    'workflow_title', 'demo-scale-type-a',
    'workflow_stage', 'policy_evidence',
    'correlation_id', pg_temp.demo_uuid('correlation', g.i)
  ),
  pg_temp.demo_ago(g.i, 3, 18)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_events (
  id, tenant_id, agent_id, task_id, event_type, action_type, target_entity,
  target_id, reasoning, confidence, is_approved, requires_approval, metadata,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_event', g.i * 100 + 3),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 3),
  pg_temp.demo_uuid('agent_task', g.i * 10 + 3),
  'crm.agents.approval_required',
  'approvals:request',
  'approval',
  pg_temp.demo_uuid('approval_a', g.i),
  'Compliance confirmed the discount threshold requires a human approver. The workflow stopped at the approval boundary.',
  0.95 + (g.i % 5)::numeric / 100,
  true, true,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'mode', 'offline-fixture',
    'workflow_title', 'demo-scale-type-a',
    'workflow_stage', 'policy_review',
    'correlation_id', pg_temp.demo_uuid('correlation', g.i)
  ),
  pg_temp.demo_ago(g.i, 2, 15)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- agent_decisions for type A
INSERT INTO agent_decisions (
  id, tenant_id, agent_id, action_type, risk_level, status, confidence,
  input_context, reasoning, evidence, tool_calls, approval_id, correlation_id,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_decision', g.i * 10 + 1),
  dsc.tenant_id,
  'sales-recommendation-agent',
  'deals:discount_recommend',
  CASE WHEN g.i <= 5 THEN 'high' WHEN g.i <= 8 THEN 'medium' ELSE 'low' END,
  'completed',
  0.82 + (g.i % 15)::numeric / 100,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'workflow_title', 'demo-scale-type-a',
    'stage', 'sales_recommendation'
  ),
  jsonb_build_object('summary', 'Prepared a governed proposal and delegated evidence retrieval.'),
  jsonb_build_array(
    jsonb_build_object('type', 'deal', 'sourceId', pg_temp.demo_uuid('deal', 1 + ((g.i - 1) % dsc.n_deals))),
    jsonb_build_object('type', 'customer', 'sourceId', pg_temp.demo_uuid('customer', 1 + ((g.i - 1) % dsc.n_customers)))
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'renewal_context_lookup', 'outcome', 'recorded'),
    jsonb_build_object('name', 'handoff_to_support', 'outcome', 'completed')
  ),
  NULL,
  pg_temp.demo_uuid('correlation', g.i),
  pg_temp.demo_ago(g.i, 4, 20)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_decisions (
  id, tenant_id, agent_id, action_type, risk_level, status, confidence,
  input_context, reasoning, evidence, tool_calls, approval_id, correlation_id,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_decision', g.i * 10 + 2),
  dsc.tenant_id,
  'support-knowledge-agent',
  'knowledge:retrieve',
  'low',
  'completed',
  0.88 + (g.i % 10)::numeric / 100,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'workflow_title', 'demo-scale-type-a',
    'stage', 'policy_evidence'
  ),
  jsonb_build_object('summary', 'Retrieved tenant-scoped policy citations and handed them to Compliance.'),
  jsonb_build_array(
    jsonb_build_object('type', 'knowledge', 'sourceId', pg_temp.demo_uuid('knowledge_article', 1 + ((g.i - 1) % dsc.n_articles))),
    jsonb_build_object('type', 'knowledge', 'sourceId', pg_temp.demo_uuid('knowledge_article', 1 + ((g.i * 7) % dsc.n_articles)))
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'semantic_policy_retrieval', 'outcome', 'grounded'),
    jsonb_build_object('name', 'handoff_to_compliance', 'outcome', 'completed')
  ),
  NULL,
  pg_temp.demo_uuid('correlation', g.i),
  pg_temp.demo_ago(g.i, 3, 18)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_decisions (
  id, tenant_id, agent_id, action_type, risk_level, status, confidence,
  input_context, reasoning, evidence, tool_calls, approval_id, correlation_id,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_decision', g.i * 10 + 3),
  dsc.tenant_id,
  'compliance-guard-agent',
  'deals:discount_apply',
  'high',
  'approved',
  0.95 + (g.i % 5)::numeric / 100,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'workflow_title', 'demo-scale-type-a',
    'stage', 'policy_review'
  ),
  jsonb_build_object('summary', 'Verified the approval requirement; human approval record is the final control boundary.'),
  jsonb_build_array(
    jsonb_build_object('type', 'policy', 'sourceId', 'renewal_discount_approval_policy'),
    jsonb_build_object('type', 'approval', 'sourceId', pg_temp.demo_uuid('approval_a', g.i))
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'policy_threshold_check', 'outcome', 'approval_required'),
    jsonb_build_object('name', 'approval_guard', 'outcome', 'human_approved')
  ),
  pg_temp.demo_uuid('approval_a', g.i),
  pg_temp.demo_uuid('correlation', g.i),
  pg_temp.demo_ago(g.i, 2, 15)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- approvals for type A
INSERT INTO approvals (
  id, tenant_id, request_type, requestor_type, requestor_id, action_type,
  target_entity, target_id, context, status, decided_by, decided_at,
  decision_reason, expires_at, created_at
)
SELECT
  pg_temp.demo_uuid('approval_a', g.i),
  dsc.tenant_id,
  'high_impact_export',
  'agent',
  '00000000-0000-0000-0000-000000000000',
  'deals:discount_apply',
  'deal',
  pg_temp.demo_uuid('deal', 1 + ((g.i - 1) % dsc.n_deals)),
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'confidence', 0.82 + (g.i % 15)::numeric / 100,
    'amount', pg_temp.demo_numeric(g.i, 15000, 120000, 2),
    'reasoning', 'A simulated agent recommends a controlled commercial concession subject to human review.'
  ),
  CASE WHEN g.i <= 7 THEN 'approved' ELSE 'rejected' END,
  NULL,  -- decided_by: no real user in demo-scale fixture
  CASE WHEN g.i <= 7 THEN pg_temp.demo_ago(g.i, 1, 15) ELSE NULL END,
  CASE WHEN g.i <= 7
    THEN 'Approved after review of policy evidence and commercial impact.'
    WHEN g.i <= 10
    THEN 'Rejected: requires additional evidence before concession can be applied.'
    ELSE NULL
  END,
  pg_temp.demo_ago(g.i, -10, 20),
  pg_temp.demo_ago(g.i, 2, 18)
FROM dsc
CROSS JOIN generate_series(1, 10) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ---------- TYPE B: approval pending (8 workflows, ids 11-18) ----------

INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', wf * 10 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 1),
  pg_temp.demo_pick(wf, ARRAY(SELECT task_type FROM demo_wf_sales_tasks)),
  jsonb_build_object(
    'workflow_title', pg_temp.demo_pick(wf, ARRAY[
      'Pending discount approval review', 'Awaiting governance sign-off',
      'Expansion pending compliance', 'Deferred approval workflow',
      'Staged commercial review', 'Queued policy evaluation',
      'Pending evidence verification', 'Approval gate awaiting human'
    ]),
    'scenario', 'demo-scale-type-b',
    'goal', 'prepare recommendation and await human approval'
  ),
  jsonb_build_object(
    'summary', 'Recommendation prepared and routed; workflow paused at approval gate.',
    'handoff_to', 'Human approver',
    'status', 'awaiting_approval'
  ),
  'completed', 4,
  pg_temp.demo_ago(wf, 4, 18),
  pg_temp.demo_ago(wf, 2, 16),
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 5, 20)
FROM dsc
CROSS JOIN generate_series(11, 18) AS g(wf)
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', wf * 10 + 2),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 3),
  pg_temp.demo_pick(wf, ARRAY(SELECT task_type FROM demo_wf_compliance_tasks)),
  jsonb_build_object(
    'workflow_title', 'Continuing from Sales handoff',
    'scenario', 'demo-scale-type-b',
    'goal', 'verify approval requirement'
  ),
  jsonb_build_object(
    'summary', 'Compliance verified the action requires human approval. Awaiting decision.',
    'handoff_to', 'Human approver',
    'approval_id', pg_temp.demo_uuid('approval_b', wf)
  ),
  CASE WHEN wf <= 15 THEN 'completed' ELSE 'pending' END,
  2,
  CASE WHEN wf <= 15 THEN pg_temp.demo_ago(wf, 3, 16) ELSE NULL END,
  CASE WHEN wf <= 15 THEN pg_temp.demo_ago(wf, 1, 12) ELSE NULL END,
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 4, 18)
FROM dsc
CROSS JOIN generate_series(11, 18) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- agent_events for type B
INSERT INTO agent_events (
  id, tenant_id, agent_id, task_id, event_type, action_type, target_entity,
  target_id, reasoning, confidence, is_approved, requires_approval, metadata,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_event', wf * 100 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 1),
  pg_temp.demo_uuid('agent_task', wf * 10 + 1),
  'crm.agents.handoff_prepared',
  'deals:discount_recommend',
  'deal',
  pg_temp.demo_uuid('deal', 1 + ((wf - 1) % dsc.n_deals)),
  'Sales prepared a recommendation and routed to approval; awaiting human decision.',
  0.80 + (wf % 15)::numeric / 100,
  NULL, true,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'mode', 'offline-fixture',
    'workflow_title', 'demo-scale-type-b',
    'workflow_stage', 'awaiting_approval',
    'correlation_id', pg_temp.demo_uuid('correlation', wf)
  ),
  pg_temp.demo_ago(wf, 2, 16)
FROM dsc
CROSS JOIN generate_series(11, 18) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- approvals for type B (all pending)
INSERT INTO approvals (
  id, tenant_id, request_type, requestor_type, requestor_id, action_type,
  target_entity, target_id, context, status, expires_at, created_at
)
SELECT
  pg_temp.demo_uuid('approval_b', wf),
  dsc.tenant_id,
  CASE WHEN wf <= 14 THEN 'high_impact_export' ELSE 'automation_activation' END,
  'agent',
  '00000000-0000-0000-0000-000000000000',
  CASE WHEN wf <= 14 THEN 'deals:discount_apply' ELSE 'automations:activate' END,
  CASE WHEN wf <= 14 THEN 'deal' ELSE 'automation_policy' END,
  CASE WHEN wf <= 14
    THEN pg_temp.demo_uuid('deal', 1 + ((wf - 1) % dsc.n_deals))
    ELSE pg_temp.demo_uuid('automation_policy', wf)
  END,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'reasoning', 'Awaiting human review before any action is executed.'
  ),
  'pending',
  now() + make_interval(hours => pg_temp.demo_int(wf, 12, 72)::int),
  pg_temp.demo_ago(wf, 2, 12)
FROM dsc
CROSS JOIN generate_series(11, 18) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- agent_decisions for type B
INSERT INTO agent_decisions (
  id, tenant_id, agent_id, action_type, risk_level, status, confidence,
  input_context, reasoning, evidence, tool_calls, approval_id, correlation_id,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_decision', wf * 10 + 1),
  dsc.tenant_id,
  'compliance-guard-agent',
  'deals:discount_apply',
  CASE WHEN wf <= 14 THEN 'high' ELSE 'medium' END,
  'pending',
  0.90 + (wf % 8)::numeric / 100,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'workflow_title', 'demo-scale-type-b',
    'stage', 'awaiting_human_approval'
  ),
  jsonb_build_object('summary', 'Action requires human approval; workflow is paused at the governance boundary.'),
  jsonb_build_array(
    jsonb_build_object('type', 'policy', 'sourceId', 'approval_required_policy'),
    jsonb_build_object('type', 'approval', 'sourceId', pg_temp.demo_uuid('approval_b', wf))
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'policy_threshold_check', 'outcome', 'approval_required'),
    jsonb_build_object('name', 'await_human_decision', 'outcome', 'pending')
  ),
  pg_temp.demo_uuid('approval_b', wf),
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 2, 12)
FROM dsc
CROSS JOIN generate_series(11, 18) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- ---------- TYPE C: policy denied / tenant-boundary block (7 workflows, ids 19-25) ----------

INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', wf * 10 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 3),
  pg_temp.demo_pick(wf, ARRAY[
    'tenant_boundary_verification',
    'export_scope_validation',
    'cross_tenant_access_check',
    'policy_denied_review',
    'governance_block_record'
  ]),
  jsonb_build_object(
    'workflow_title', pg_temp.demo_pick(wf, ARRAY[
      'Cross-tenant evidence request blocked', 'Export scope mismatch denied',
      'Unauthorized data access prevented', 'Policy boundary enforcement',
      'Tenant isolation gate triggered', 'RLS boundary verification',
      'Compliance policy denial recorded'
    ]),
    'scenario', 'demo-scale-type-c',
    'goal', 'verify tenant scope and deny out-of-boundary requests'
  ),
  jsonb_build_object(
    'summary', 'Request blocked: scope did not match authenticated tenant. No data access occurred.',
    'denied_reason', CASE
      WHEN wf <= 21 THEN 'tenant_scope_mismatch'
      WHEN wf <= 23 THEN 'policy_denied'
      ELSE 'export_scope_violation'
    END,
    'handoff_to', 'None (blocked at boundary)'
  ),
  'completed', 1,
  pg_temp.demo_ago(wf, 5, 15),
  pg_temp.demo_ago(wf, 3, 10),
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 6, 17)
FROM dsc
CROSS JOIN generate_series(19, 25) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- agent_events for type C
INSERT INTO agent_events (
  id, tenant_id, agent_id, task_id, event_type, action_type, target_entity,
  target_id, reasoning, confidence, is_approved, requires_approval, metadata,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_event', wf * 100 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 3),
  pg_temp.demo_uuid('agent_task', wf * 10 + 1),
  'crm.agents.policy_blocked',
  CASE
    WHEN wf <= 21 THEN 'evidence:cross_tenant_export'
    WHEN wf <= 23 THEN 'data:unauthorized_access'
    ELSE 'export:scope_violation'
  END,
  'knowledge',
  pg_temp.demo_uuid('knowledge_article', 1 + ((wf - 1) % dsc.n_articles)),
  CASE
    WHEN wf <= 21 THEN 'Request blocked: the evidence scope did not match the authenticated tenant. No export attempted.'
    WHEN wf <= 23 THEN 'Policy denied the request: action exceeds authorized scope for this tenant.'
    ELSE 'Export scope violation: requested data spans multiple tenants. Request denied at boundary.'
  END,
  0.99, false, false,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'mode', 'offline-fixture',
    'workflow_title', 'demo-scale-type-c',
    'workflow_stage', 'policy_blocked',
    'correlation_id', pg_temp.demo_uuid('correlation', wf)
  ),
  pg_temp.demo_ago(wf, 3, 10)
FROM dsc
CROSS JOIN generate_series(19, 25) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- agent_decisions for type C
INSERT INTO agent_decisions (
  id, tenant_id, agent_id, action_type, risk_level, status, confidence,
  input_context, reasoning, evidence, tool_calls, approval_id, correlation_id,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_decision', wf * 10 + 1),
  dsc.tenant_id,
  'compliance-guard-agent',
  CASE
    WHEN wf <= 21 THEN 'evidence:cross_tenant_export'
    WHEN wf <= 23 THEN 'data:unauthorized_access'
    ELSE 'export:scope_violation'
  END,
  'critical',
  'denied',
  0.99,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'reason', CASE
      WHEN wf <= 21 THEN 'tenant_scope_mismatch'
      WHEN wf <= 23 THEN 'policy_denied'
      ELSE 'export_scope_violation'
    END
  ),
  jsonb_build_object('summary', 'Policy denied a request outside the authenticated tenant boundary. No data was accessed.'),
  jsonb_build_array(
    jsonb_build_object('type', 'policy', 'sourceId', 'tenant_scope_guard'),
    jsonb_build_object('type', 'knowledge', 'sourceId', pg_temp.demo_uuid('knowledge_article', 1 + ((wf - 1) % dsc.n_articles)))
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'tenant_context_check', 'outcome', 'blocked'),
    jsonb_build_object('name', 'export_guard', 'outcome', 'not_executed')
  ),
  NULL,
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 3, 10)
FROM dsc
CROSS JOIN generate_series(19, 25) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- ---------- TYPE D: degraded / retriable (5 workflows, ids 26-30) ----------

INSERT INTO agent_tasks (
  id, tenant_id, agent_id, task_type, input_data, output_data, status,
  priority, started_at, completed_at, error_message, correlation_id, created_at
)
SELECT
  pg_temp.demo_uuid('agent_task', wf * 10 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 5),
  pg_temp.demo_pick(wf, ARRAY[
    'automation_simulation_execute',
    'workflow_compilation_retry',
    'policy_simulation_run',
    'bulk_operation_governance_check',
    'integration_health_probe'
  ]),
  jsonb_build_object(
    'workflow_title', pg_temp.demo_pick(wf, ARRAY[
      'Automation simulation retry', 'Workflow compilation degraded',
      'Policy evaluation timeout', 'Bulk operation circuit open',
      'Integration probe degraded'
    ]),
    'scenario', 'demo-scale-type-d',
    'goal', 'execute with retry or record degraded state'
  ),
  CASE
    WHEN wf <= 27
    THEN jsonb_build_object(
      'summary', 'Task succeeded after retry.',
      'retry_count', pg_temp.demo_int(wf, 1, 3),
      'last_error', 'Transient upstream timeout resolved on retry.'
    )
    ELSE jsonb_build_object(
      'summary', 'Task recorded as degraded; manual review required.',
      'last_error', pg_temp.demo_pick(wf, ARRAY[
        'Circuit breaker open after consecutive failures.',
        'Upstream service degraded: partial results returned.',
        'Compilation timeout after 3 attempts.'
      ])
    )
  END,
  CASE WHEN wf <= 27 THEN 'completed' ELSE 'failed' END,
  5,
  pg_temp.demo_ago(wf, 8, 20),
  CASE WHEN wf <= 27 THEN pg_temp.demo_ago(wf, 4, 15) ELSE NULL END,
  CASE WHEN wf > 27
    THEN pg_temp.demo_pick(wf, ARRAY[
      'Circuit breaker open after consecutive failures.',
      'Upstream dependency timeout after max retries.',
      'Compilation error: workflow JSON invalid after 3 retry attempts.'
    ])
    ELSE NULL
  END,
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 9, 22)
FROM dsc
CROSS JOIN generate_series(26, 30) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- agent_events for type D
INSERT INTO agent_events (
  id, tenant_id, agent_id, task_id, event_type, action_type, target_entity,
  target_id, reasoning, confidence, is_approved, requires_approval, metadata,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_event', wf * 100 + 1),
  dsc.tenant_id,
  pg_temp.demo_uuid('ai_agent', 5),
  pg_temp.demo_uuid('agent_task', wf * 10 + 1),
  CASE WHEN wf <= 27 THEN 'crm.agents.retry_succeeded' ELSE 'crm.agents.task_failed' END,
  'automation:execute',
  'automation_policy',
  pg_temp.demo_uuid('automation_policy', wf),
  CASE
    WHEN wf <= 27
    THEN 'Task completed after automated retry. Transient issue resolved.'
    ELSE 'Task failed after max retries. Manual investigation required.'
  END,
  CASE WHEN wf <= 27 THEN 0.75 ELSE 0.45 END,
  NULL, false,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'mode', 'offline-fixture',
    'workflow_title', 'demo-scale-type-d',
    'workflow_stage', CASE WHEN wf <= 27 THEN 'retry_succeeded' ELSE 'degraded' END,
    'correlation_id', pg_temp.demo_uuid('correlation', wf)
  ),
  pg_temp.demo_ago(wf, 4, 15)
FROM dsc
CROSS JOIN generate_series(26, 30) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- agent_decisions for type D
INSERT INTO agent_decisions (
  id, tenant_id, agent_id, action_type, risk_level, status, confidence,
  input_context, reasoning, evidence, tool_calls, approval_id, correlation_id,
  created_at
)
SELECT
  pg_temp.demo_uuid('agent_decision', wf * 10 + 1),
  dsc.tenant_id,
  'automation-orchestrator-agent',
  'automation:execute',
  CASE WHEN wf <= 27 THEN 'medium' ELSE 'high' END,
  CASE WHEN wf <= 27 THEN 'completed' ELSE 'degraded' END,
  CASE WHEN wf <= 27 THEN 0.75 ELSE 0.45 END,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'workflow_title', 'demo-scale-type-d',
    'stage', CASE WHEN wf <= 27 THEN 'retry_succeeded' ELSE 'degraded' END
  ),
  jsonb_build_object(
    'summary', CASE
      WHEN wf <= 27 THEN 'Task succeeded after retry. Circuit breaker reset.'
      ELSE 'Task degraded: manual review required before retry can proceed.'
    END
  ),
  jsonb_build_array(
    jsonb_build_object('type', 'automation_policy', 'sourceId', pg_temp.demo_uuid('automation_policy', wf)),
    jsonb_build_object('type', 'circuit_breaker', 'sourceId', 'retry_state')
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'circuit_breaker_check', 'outcome', CASE WHEN wf <= 27 THEN 'half_open_probe_success' ELSE 'open' END),
    jsonb_build_object('name', 'retry_orchestrator', 'outcome', CASE WHEN wf <= 27 THEN 'retry_succeeded' ELSE 'max_retries_exceeded' END)
  ),
  NULL,
  pg_temp.demo_uuid('correlation', wf),
  pg_temp.demo_ago(wf, 4, 15)
FROM dsc
CROSS JOIN generate_series(26, 30) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 9. PREDICTIONS (one per customer)
-- ==================================================================

INSERT INTO predictions (
  id, tenant_id, entity_type, entity_id, prediction_type, probability,
  risk_level, explanation, features, model_version, created_at
)
SELECT
  pg_temp.demo_uuid('prediction', g.i),
  dsc.tenant_id,
  'customer',
  pg_temp.demo_uuid('customer', g.i)::text,
  CASE
    WHEN g.i <= (dsc.n_customers * 0.33) THEN 'expansion_propensity'
    WHEN g.i <= (dsc.n_customers * 0.66) THEN 'renewal_risk'
    ELSE 'renewal_health'
  END,
  pg_temp.demo_numeric(g.i, 0.30, 0.95, 2)::double precision,
  CASE
    WHEN g.i <= (dsc.n_customers * 0.60) THEN 'low'
    WHEN g.i <= (dsc.n_customers * 0.85) THEN 'medium'
    ELSE 'high'
  END,
  pg_temp.demo_pick(g.i, ARRAY[
    'High adoption and engagement support expansion.',
    'Open compliance tickets create renewal risk.',
    'Low engagement requires monitored follow-up.',
    'Multiple support escalations indicate churn risk.',
    'Strong product usage and executive alignment.',
    'Forecast confidence improving from recent activity.',
    'Support impact score below threshold; review needed.',
    'Pipeline activity supports healthy renewal outlook.'
  ]),
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'seed', dsc.seed,
    'signals', jsonb_build_array(
      pg_temp.demo_pick(g.i, ARRAY['product_adoption','executive_engagement','support_volume']),
      pg_temp.demo_pick(g.i * 3, ARRAY['forecast_confidence','renewal_timing','nps_score'])
    )
  ),
  'demo-scale-v1',
  pg_temp.demo_ago(g.i, 1, 14)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_customers) AS g(i)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 10. CUSTOMER TIMELINES (2-5 events per customer)
-- ==================================================================

INSERT INTO customer_timelines (
  id, tenant_id, customer_id, event_type, event_payload, "timestamp", created_at
)
SELECT
  pg_temp.demo_uuid('customer_timeline', g.i * 10 + e.pos),
  dsc.tenant_id,
  pg_temp.demo_uuid('customer', g.i),
  e.event_type,
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'summary', e.summary
  ),
  pg_temp.demo_ago(g.i, e.days_ago, 10),
  pg_temp.demo_ago(g.i, e.days_ago, 10)
FROM dsc
CROSS JOIN generate_series(1, dsc.n_customers) AS g(i)
CROSS JOIN LATERAL (
  VALUES
    ('business_review_completed', 'Governance review completed.', 40, 1),
    ('support_case_opened', 'Support ticket created for compliance review.', 25, 2),
    ('automation_pilot_started', 'Workflow automation pilot initiated.', 15, 3),
    ('data_quality_review', 'Data hygiene assessment scheduled.', 8, 4),
    ('contract_renewed', 'Annual contract renewal processed.', 3, 5)
) AS e(event_type, summary, days_ago, pos)
WHERE g.i % 7 != e.pos % 3  -- vary count per customer
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- 11. AUTOMATION POLICIES (one per workflow type D)
-- ==================================================================

INSERT INTO automation_policies (
  id, tenant_id, created_by, status, nl_rule_text, trigger_type, workflow_json,
  compiled_json, version, last_simulation_id, created_at, updated_at
)
SELECT
  pg_temp.demo_uuid('automation_policy', wf),
  dsc.tenant_id,
  COALESCE(
    (SELECT u.id FROM users u WHERE u.tenant_id = dsc.tenant_id LIMIT 1),
    '00000000-0000-0000-0000-000000000000'
  ),
  CASE WHEN wf <= 27 THEN 'active' ELSE 'draft' END,
  CASE
    WHEN wf <= 27
    THEN 'When a renewal discount exceeds 15 percent, collect cited policy evidence and route to human approval.'
    ELSE 'When an integration health probe fails 3 consecutive times, pause the circuit and notify the operations team.'
  END,
  CASE
    WHEN wf <= 27 THEN 'deal.renewal_review_required'
    ELSE 'integration.health_check_degraded'
  END,
  jsonb_build_object(
    'steps', CASE
      WHEN wf <= 27
      THEN jsonb_build_array(
        jsonb_build_object('name', 'collect_renewal_context', 'mode', 'read_only'),
        jsonb_build_object('name', 'retrieve_policy_evidence', 'mode', 'read_only'),
        jsonb_build_object('name', 'request_human_approval', 'mode', 'approval_required')
      )
      ELSE jsonb_build_array(
        jsonb_build_object('name', 'health_probe', 'mode', 'read_only'),
        jsonb_build_object('name', 'circuit_breaker_check', 'mode', 'read_only'),
        jsonb_build_object('name', 'retry_with_backoff', 'mode', 'retriable'),
        jsonb_build_object('name', 'notify_operations', 'mode', 'notification')
      )
    END
  ),
  jsonb_build_object(
    'source', 'demo-scale',
    'dataset_version', '1.0.0',
    'safety', 'no commercial mutation before recorded approval',
    'dry_run', true
  ),
  1,
  NULL,
  pg_temp.demo_ago(wf, 10, 30),
  pg_temp.demo_ago(wf, 3, 10)
FROM dsc
CROSS JOIN generate_series(26, 30) AS g(wf)
ON CONFLICT (id) DO NOTHING;

-- ==================================================================
-- SUMMARY
-- ==================================================================

COMMIT;

SELECT
  'demo-scale fixture applied' AS result,
  (SELECT count(*) FROM customers WHERE metadata ->> 'source' = 'demo-scale') AS customers,
  (SELECT count(*) FROM customer_profiles WHERE features ->> 'source' = 'demo-scale') AS profiles,
  (SELECT count(*) FROM leads WHERE metadata ->> 'source' = 'demo-scale') AS leads,
  (SELECT count(*) FROM deals WHERE metadata ->> 'source' = 'demo-scale') AS deals,
  (SELECT count(*) FROM tickets WHERE metadata ->> 'source' = 'demo-scale') AS tickets,
  (SELECT count(*) FROM knowledge_articles WHERE tenant_id = :'tenant_id'::uuid AND tags ? 'demo-scale') AS articles,
  (SELECT count(*) FROM ai_agents WHERE config ->> 'source' = 'demo-scale') AS agents,
  (SELECT count(*) FROM agent_tasks WHERE tenant_id = :'tenant_id'::uuid AND input_data ->> 'scenario' LIKE 'demo-scale-%') AS agent_tasks,
  (SELECT count(*) FROM agent_events WHERE metadata ->> 'source' = 'demo-scale') AS agent_events,
  (SELECT count(*) FROM agent_decisions WHERE input_context ->> 'source' = 'demo-scale') AS agent_decisions,
  (SELECT count(*) FROM predictions WHERE tenant_id = :'tenant_id'::uuid AND model_version = 'demo-scale-v1') AS predictions,
  (SELECT count(*) FROM customer_timelines WHERE event_payload ->> 'source' = 'demo-scale') AS timeline_events,
  (SELECT count(*) FROM approvals WHERE context ->> 'source' = 'demo-scale') AS approvals,
  (SELECT count(*) FROM automation_policies WHERE compiled_json ->> 'source' = 'demo-scale') AS automation_policies,
  (SELECT count(*) FROM agent_tasks WHERE input_data ->> 'scenario' = 'demo-scale-type-a') AS type_a_workflows,
  (SELECT count(*) FROM agent_tasks WHERE input_data ->> 'scenario' = 'demo-scale-type-b') AS type_b_workflows,
  (SELECT count(*) FROM agent_tasks WHERE input_data ->> 'scenario' = 'demo-scale-type-c') AS type_c_workflows,
  (SELECT count(*) FROM agent_tasks WHERE input_data ->> 'scenario' = 'demo-scale-type-d') AS type_d_workflows;
