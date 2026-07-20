# Demo-Scale Data Generator

## Purpose

`scripts/seed-demo-scale.sql` generates a configurable, repeatable synthetic
dataset whose entity IDs, relationships, and business distributions are
determined by `tenant_id` + `seed`. Timestamps use `now()` at insert time and
are NOT byte-identical across runs; re-running the same parameters is
idempotent (no duplicate rows) but does not produce identical timestamps.

**This is synthetic demonstration data only.** It does not invoke NVIDIA NIM,
Ollama, or any external API and produces no model-inference charges.

## Quick Start

```powershell
# Prerequisites: docker compose up -d, tenant created, and you are logged in.
$tenantId = '<your-tenant-uuid>'

# Generate default-scale data (200 customers, 1000 leads, 600 deals, etc.)
# docker compose exec -e passes TENANT_ID into the container so the shell
# expands "$TENANT_ID" inside the single-quoted psql command.
Get-Content -Raw scripts/seed-demo-scale.sql |
  docker compose exec -T -e "TENANT_ID=$tenantId" postgres sh -lc `
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" `
      -v tenant_id="$TENANT_ID"'

# Generate custom scale (numeric values are literals; only tenant_id uses the env var)
Get-Content -Raw scripts/seed-demo-scale.sql |
  docker compose exec -T -e "TENANT_ID=$tenantId" postgres sh -lc `
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" `
      -v tenant_id="$TENANT_ID" `
      -v customers=500 `
      -v leads=2000 `
      -v deals=1200 `
      -v tickets=800 `
      -v knowledge_articles=60 `
      -v workflows=50 `
      -v seed=20260721'

# Clear demo-scale data only
Get-Content -Raw scripts/clear-demo-scale.sql |
  docker compose exec -T -e "TENANT_ID=$tenantId" postgres sh -lc `
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" `
      -v tenant_id="$TENANT_ID"'
```

## Parameters

| Variable | Default | Description |
|---|---|---|
| `tenant_id` | **required** | Target tenant UUID |
| `customers` | 200 | Number of customer rows |
| `leads` | 1000 | Number of lead rows |
| `deals` | 600 | Number of deal rows |
| `tickets` | 300 | Number of ticket rows |
| `knowledge_articles` | 40 | Number of knowledge article rows |
| `workflows` | 30 | Number of multi-agent collaboration workflows |
| `seed` | 20260720 | Deterministic seed: IDs, relationships, and distributions repeat; timestamps use now() and differ per run |

## Generated Data Profile

### Customers (default 200)

| Category | Approx % | Description |
|---|---|---|
| Healthy | 60% | Active, high-value, expansion-stage |
| Watch | 25% | Active but with open tickets or medium risk |
| Risk | 15% | At-risk or churned, high-priority attention needed |

Segments: enterprise (60%), mid-market (22%), growth (12%), SMB (6%).
Lifetime values range from $5K to $250K.

### Leads (default 1000)

| Stage | Approx % |
|---|---|
| New | 30% |
| Contacted | 25% |
| Qualified | 20% |
| Proposal | 13% |
| Nurture | 6% |
| Unqualified | 6% |

Sources: webinar, inbound, partner, conference, outbound, referral, social,
email campaign, trade show, website, cold call, LinkedIn, advertisement,
community, content marketing.

### Deals (default 600)

| Stage | Approx % | Notes |
|---|---|---|
| Prospecting | 25% | Early pipeline |
| Qualification | 20% | Being validated |
| Proposal | 17% | Under review |
| Negotiation | 14% | Near close |
| Closed Won | 13% | Includes high-value renewals |
| Closed Lost | 8% | Lost to competitor or budget |
| Deferred | 3% | Pushed to next cycle |

Amounts: $5K - $200K in USD.

### Tickets (default 300)

| Priority | Approx % | SLA |
|---|---|---|
| Urgent | 10% | 4 hours |
| High | 22% | 8 hours |
| Medium | 38% | 24 hours |
| Low | 30% | 72 hours |

Status: open (33%), in_progress (27%), resolved (32%), closed (8%).
SLA risk: breached (10%), at_risk (15%), on_track (75%).
Categories cover identity, compliance, automation, billing, support,
integration, security, data_export, performance, onboarding, configuration,
governance, reporting, access_control, and workflow.

### Knowledge Articles (default 40)

40 distinct articles drawn from a pool covering:
- Renewal and discount approval policies
- RLS and tenant isolation patterns
- Data export compliance
- Automation governance boundaries
- Customer support response templates
- Multi-agent collaboration contracts
- Governance decision records
- Kill switch operations
- OPA policy authoring
- GDPR workflows
- Agent telemetry and observability
- Predictive scoring methodology
- Security event response
- Circuit breaker patterns
- SLA calculation and reporting
- And more

Each article carries the `demo-scale` tag alongside 3 topic tags.

### Multi-Agent Workflows (default 30)

Four workflow categories, each demonstrating a distinct governance scenario:

| Type | Count | Description | Key Signal |
|---|---|---|---|
| **A: Sales -> Support -> Compliance -> approval** | 10 | Three-specialist handoff ending at human approval gate | Completed collaboration trace; 3 tasks + 3 events + 3 decisions per workflow |
| **B: Approval pending** | 8 | Workflow paused at human-approval boundary | Demonstrates the governance gap; approvals remain in `pending` state |
| **C: Policy denied** | 7 | Cross-tenant or out-of-scope request blocked | `tenant_scope_mismatch`, `policy_denied`, `export_scope_violation`; all status = `denied` |
| **D: Degraded / retriable** | 5 | Circuit breaker open, retry with backoff, partial failure | `retry_succeeded` or `degraded` outcomes; `failed` task status with error messages |

Every workflow has:
- A `correlation_id` linking tasks, events, and decisions
- `agent_tasks` (2-4 per workflow) with `input_data` and `output_data`
- `agent_events` (1-3 per task) with reasoning, confidence, and approval flags
- `agent_decisions` (1-3 per workflow) with evidence, tool_calls, and policy citations

### AI Agents (5)

| Agent | Type | Key Capability |
|---|---|---|
| Sales Recommendation Agent | sales | Deal recommendation, handoff to Support |
| Support Knowledge Agent | support | Knowledge retrieval, cited handoff |
| Compliance Guard Agent | compliance | Policy evaluation, tenant-boundary enforcement |
| Analytics Forecast Agent | analytics | Health scoring, pipeline forecasting |
| Automation Orchestrator Agent | automation | Workflow simulation, retry orchestration |

All agents carry `config.source = 'demo-scale'` and `config.mode = 'offline-fixture'`.
These are deterministic fixture records, not claims of live model inference.

## Idempotency

The generator is row-idempotent but not byte-identical:

- All primary keys are deterministic `md5(tenant || namespace || seed || index)::uuid`.
- All inserts use `ON CONFLICT (id) DO NOTHING`.
- Same `tenant_id` + `seed` + scale produces the same IDs, entity relationships, and
  business distributions on every run.  Timestamp columns (`created_at`,
  `updated_at`, etc.) use `now()` and differ between runs; on conflict the
  first-run timestamps are preserved.
- Rerunning with the same parameters is safe: no duplicate rows (skipped by
  conflict), no updates to existing rows.
- To generate a fresh dataset for the same tenant, `clear` then `seed` with a
  new `seed` value.  Running `seed` with a different seed WITHOUT clearing
  first produces a mix of old and new rows.

## Inspection Queries

```sql
-- Count all demo-scale records for a tenant
SELECT 'customers' AS tbl, count(*) FROM customers
WHERE tenant_id = '<tenant-id>' AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'leads', count(*) FROM leads
WHERE tenant_id = '<tenant-id>' AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'deals', count(*) FROM deals
WHERE tenant_id = '<tenant-id>' AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'tickets', count(*) FROM tickets
WHERE tenant_id = '<tenant-id>' AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'articles', count(*) FROM knowledge_articles
WHERE tenant_id = '<tenant-id>' AND tags ? 'demo-scale'
UNION ALL
SELECT 'agent_tasks', count(*) FROM agent_tasks
WHERE tenant_id = '<tenant-id>' AND input_data ->> 'scenario' LIKE 'demo-scale-%'
UNION ALL
SELECT 'agent_events', count(*) FROM agent_events
WHERE tenant_id = '<tenant-id>' AND metadata ->> 'source' = 'demo-scale'
UNION ALL
SELECT 'agent_decisions', count(*) FROM agent_decisions
WHERE tenant_id = '<tenant-id>' AND input_context ->> 'source' = 'demo-scale';

-- Verify interview-demo data is intact
SELECT 'interview-demo customers' AS label, count(*) FROM customers
WHERE tenant_id = '<tenant-id>' AND metadata ->> 'source' = 'interview-demo'
UNION ALL
SELECT 'interview-demo articles', count(*) FROM knowledge_articles
WHERE tenant_id = '<tenant-id>' AND tags ? 'interview-demo';

-- View collaboration traces (Type A workflows)
SELECT
  at.id AS task_id,
  at.task_type,
  at.status,
  at.correlation_id,
  aa.name AS agent_name
FROM agent_tasks at
JOIN ai_agents aa ON aa.id = at.agent_id
WHERE at.tenant_id = '<tenant-id>'
  AND at.input_data ->> 'scenario' = 'demo-scale-type-a'
ORDER BY at.created_at;

-- View denied decisions (Type C)
SELECT agent_id, action_type, risk_level, status, reasoning ->> 'summary' AS summary
FROM agent_decisions
WHERE tenant_id = '<tenant-id>'
  AND status = 'denied';

-- View pending approvals (Type B)
SELECT id, request_type, action_type, status, context ->> 'reasoning' AS reasoning
FROM approvals
WHERE tenant_id = '<tenant-id>'
  AND context ->> 'source' = 'demo-scale'
  AND status = 'pending';

-- View workflow distribution
SELECT
  input_data ->> 'scenario' AS scenario,
  count(*) AS task_count,
  count(DISTINCT correlation_id) AS workflow_count
FROM agent_tasks
WHERE tenant_id = '<tenant-id>'
  AND input_data ->> 'scenario' LIKE 'demo-scale-%'
GROUP BY input_data ->> 'scenario'
ORDER BY input_data ->> 'scenario';
```

## NIM Vector Reindex (OPTIONAL)

The seed script creates knowledge articles with `source_draft_id = NULL` and
no vector embeddings. If you need semantic search for the demo-scale articles
and NVIDIA NIM is configured, run the reindex command **after** seeding:

```powershell
docker compose exec -T agents python -m intelligence.knowledge.reindex `
  --tenant-id $tenantId
```

**This command calls the configured embedding provider (NVIDIA NIM) and
produces API usage.** It is not required for the demo-scale data to appear in
the UI; articles, tags, and reuse counts are visible without vectors.

To verify retrieval quality after reindexing:

```powershell
docker compose exec -T agents python -m intelligence.knowledge.evaluate_retrieval `
  --tenant-id $tenantId --top-k 3 --fail-under-recall-at-k 0.8
```

## Safety Boundaries

| Guarantee | How |
|---|---|
| Tenant-scoped | Every INSERT/UPDATE/DELETE filtered by `tenant_id` or `app.tenant_id` |
| Demo-scale markers | All rows have `source='demo-scale'` in metadata, tags, or context |
| Clear safety | `clear-demo-scale.sql` only deletes rows with demo-scale markers |
| ai_agents isolation | ai_agents is a shared table; agents carry `config.tenant_id` and are only deleted when no other tenant references them |
| Interview-demo isolation | Clear script never touches `interview-demo` tagged data |
| No external API calls | Pure SQL; no NIM, no Ollama, no HTTP |
| No model claims | All agent data is `offline-fixture`; no live inference |
| Row-idempotent | Deterministic UUIDs + `ON CONFLICT DO NOTHING`; timestamps use now() |
| Reversible | `clear` then `seed` = fresh dataset |

## Troubleshooting

| Problem | Fix |
|---|---|
| `tenant ... not found` | Create the tenant first: use `seed-interview-demo.sql` or create manually via Keycloak |
| No rows generated | Verify `tenant_id` is a valid UUID in the `tenants` table |
| Duplicate rows after multiple runs | Should not happen with same seed (`ON CONFLICT DO NOTHING`); verify you used matching `tenant_id` and `seed` |
| Clear script reports non-zero remaining | Check for manual edits to demo-scale rows that changed their markers |
