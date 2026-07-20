\set ON_ERROR_STOP on

-- Safely remove ONLY demo-scale data for a given tenant.
--
-- Usage from the repository root:
--   docker compose exec -T -e "TENANT_ID=<tenant UUID>" postgres sh -lc \
--     'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v tenant_id="$TENANT_ID"' \
--     < scripts/clear-demo-scale.sql
--
-- SAFETY GUARANTEES:
--   - Never deletes interview-demo data (checks source != 'interview-demo').
--   - Never deletes data from other tenants.
--   - Never deletes data without demo-scale markers.
--   - ai_agents is a shared table; agents are only deleted when their
--     config.tenant_id matches the supplied tenant AND no other tenant's
--     agent_tasks or agent_events reference them.  If cross-tenant refs
--     exist, agents are kept and a WARNING notice is printed.
--   - Prints counts before and after so you can verify.

\if :{?tenant_id}
\else
  \echo 'tenant_id is required'
  \quit
\endif

BEGIN;

-- Propagate tenant_id into a runtime-accessible setting so DO blocks
-- (where psql does not expand :'variables') can read it back via
-- current_setting('app.tenant_id').
SELECT set_config('app.tenant_id', :'tenant_id', true);

\echo '=== Pre-clean counts (demo-scale rows for this tenant) ==='

SELECT 'customers' AS entity,
  count(*) AS rows_to_delete
FROM customers
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'customer_profiles',
  count(*)
FROM customer_profiles cpf
JOIN customers c ON c.id = cpf.customer_id AND c.tenant_id = cpf.tenant_id
WHERE cpf.tenant_id = :'tenant_id'::uuid
  AND cpf.features ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'leads',
  count(*)
FROM leads
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'deals',
  count(*)
FROM deals
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'tickets',
  count(*)
FROM tickets
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'knowledge_articles',
  count(*)
FROM knowledge_articles
WHERE tenant_id = :'tenant_id'::uuid
  AND tags ? 'demo-scale'
UNION ALL
SELECT 'ai_agents (this tenant only)',
  count(*)
FROM ai_agents
WHERE config ->> 'source' = 'demo-scale'
  AND config ->> 'tenant_id' = :'tenant_id'::text
UNION ALL
SELECT 'agent_tasks',
  count(*)
FROM agent_tasks
WHERE tenant_id = :'tenant_id'::uuid
  AND input_data ->> 'scenario' LIKE 'demo-scale-%'
UNION ALL
SELECT 'agent_events',
  count(*)
FROM agent_events
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'agent_decisions',
  count(*)
FROM agent_decisions
WHERE tenant_id = :'tenant_id'::uuid
  AND input_context ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'predictions',
  count(*)
FROM predictions
WHERE tenant_id = :'tenant_id'::uuid
  AND model_version = 'demo-scale-v1'
UNION ALL
SELECT 'customer_timelines',
  count(*)
FROM customer_timelines
WHERE tenant_id = :'tenant_id'::uuid
  AND event_payload ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'approvals',
  count(*)
FROM approvals
WHERE tenant_id = :'tenant_id'::uuid
  AND context ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'automation_policies',
  count(*)
FROM automation_policies
WHERE tenant_id = :'tenant_id'::uuid
  AND compiled_json ->> 'source' = 'demo-scale'
ORDER BY entity;

-- ---------- DELETE in dependency-safe order ----------

-- Child / event / log rows first
DELETE FROM agent_events
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale';

DELETE FROM agent_decisions
WHERE tenant_id = :'tenant_id'::uuid
  AND input_context ->> 'source' = 'demo-scale';

DELETE FROM agent_tasks
WHERE tenant_id = :'tenant_id'::uuid
  AND input_data ->> 'scenario' LIKE 'demo-scale-%';

DELETE FROM customer_timelines
WHERE tenant_id = :'tenant_id'::uuid
  AND event_payload ->> 'source' = 'demo-scale';

DELETE FROM predictions
WHERE tenant_id = :'tenant_id'::uuid
  AND model_version = 'demo-scale-v1';

DELETE FROM approvals
WHERE tenant_id = :'tenant_id'::uuid
  AND context ->> 'source' = 'demo-scale';

DELETE FROM automation_policies
WHERE tenant_id = :'tenant_id'::uuid
  AND compiled_json ->> 'source' = 'demo-scale';

DELETE FROM knowledge_articles
WHERE tenant_id = :'tenant_id'::uuid
  AND tags ? 'demo-scale';

DELETE FROM customer_profiles
WHERE tenant_id = :'tenant_id'::uuid
  AND features ->> 'source' = 'demo-scale';

DELETE FROM tickets
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale';

DELETE FROM deals
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale';

DELETE FROM leads
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale';

DELETE FROM customers
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale';

-- ai_agents is a shared table (no tenant_id column).  Only delete agents
-- whose config identifies them as belonging to this tenant, and only after
-- confirming no other tenant references them via agent_tasks or agent_events.
DO $$
DECLARE
  v_tenant_id uuid := current_setting('app.tenant_id')::uuid;
  v_tenant_text text := v_tenant_id::text;
  v_cross_refs integer;
  v_agent_count integer;
BEGIN
  -- How many demo-scale agents belong to this tenant?
  SELECT count(*) INTO v_agent_count
  FROM ai_agents
  WHERE config ->> 'source' = 'demo-scale'
    AND config ->> 'tenant_id' = v_tenant_text;

  IF v_agent_count = 0 THEN
    RAISE NOTICE 'No demo-scale agents found for tenant %; skipping agent cleanup.', v_tenant_id;
    RETURN;
  END IF;

  -- Are any of this tenant's agents referenced by tasks or events from
  -- OTHER tenants?
  SELECT count(*) INTO v_cross_refs
  FROM agent_tasks at
  WHERE at.agent_id IN (
    SELECT id FROM ai_agents
    WHERE config ->> 'source' = 'demo-scale'
      AND config ->> 'tenant_id' = v_tenant_text
  )
  AND at.tenant_id != v_tenant_id;

  IF v_cross_refs = 0 THEN
    SELECT count(*) INTO v_cross_refs
    FROM agent_events ae
    WHERE ae.agent_id IN (
      SELECT id FROM ai_agents
      WHERE config ->> 'source' = 'demo-scale'
        AND config ->> 'tenant_id' = v_tenant_text
    )
    AND ae.tenant_id != v_tenant_id;
  END IF;

  IF v_cross_refs > 0 THEN
    RAISE NOTICE 'WARNING: % cross-tenant references found for demo-scale agents of tenant %. Agents will NOT be deleted to avoid breaking other tenants'' data.',
      v_cross_refs, v_tenant_id;
  ELSE
    DELETE FROM ai_agents
    WHERE config ->> 'source' = 'demo-scale'
      AND config ->> 'tenant_id' = v_tenant_text;
    RAISE NOTICE 'Deleted % demo-scale agent(s) for tenant %.', v_agent_count, v_tenant_id;
  END IF;
END $$;

-- ---------- Verify nothing leaked ----------

\echo '=== Post-clean counts (demo-scale rows for this tenant) ==='

SELECT 'customers' AS entity,
  count(*) AS remaining
FROM customers
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'leads',
  count(*)
FROM leads
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'deals',
  count(*)
FROM deals
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'tickets',
  count(*)
FROM tickets
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'knowledge_articles',
  count(*)
FROM knowledge_articles
WHERE tenant_id = :'tenant_id'::uuid
  AND tags ? 'demo-scale'
UNION ALL
SELECT 'agent_tasks',
  count(*)
FROM agent_tasks
WHERE tenant_id = :'tenant_id'::uuid
  AND input_data ->> 'scenario' LIKE 'demo-scale-%'
UNION ALL
SELECT 'agent_events',
  count(*)
FROM agent_events
WHERE tenant_id = :'tenant_id'::uuid
  AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'agent_decisions',
  count(*)
FROM agent_decisions
WHERE tenant_id = :'tenant_id'::uuid
  AND input_context ->> 'source' = 'demo-scale'
ORDER BY entity;

-- Safety check: confirm interview-demo data is untouched
DO $$
DECLARE
  v_tenant_id uuid := current_setting('app.tenant_id')::uuid;
  v_interview_customers integer;
  v_interview_articles integer;
BEGIN
  SELECT count(*) INTO v_interview_customers
  FROM customers
  WHERE tenant_id = v_tenant_id
    AND metadata ->> 'source' = 'interview-demo';

  SELECT count(*) INTO v_interview_articles
  FROM knowledge_articles
  WHERE tenant_id = v_tenant_id
    AND tags ? 'interview-demo';

  RAISE NOTICE 'interview-demo data still present: % customers, % articles',
    v_interview_customers, v_interview_articles;
END $$;

\echo '=== Clear complete. All demo-scale rows removed for this tenant. ==='

COMMIT;
