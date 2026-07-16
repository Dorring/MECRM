# H2-4 Preflight: Agent Run Evidence and Safe Trace View

Date: 2026-07-16

Baseline: `main@95132e4`

Status: Preflight only. No runtime behavior changes in this document.

## Goal

Make one agent action explainable during an interview without exposing chain of
thought, prompts, credentials, tenant data from another tenant, or unbounded
third-party payloads. The interface must show a safe evidence summary for a
completed, denied, pending approval, degraded, or failed action.

## Existing ownership and reuse decision

Do not create a new `agent_runs` table. The existing `agent_decisions` table is
the tenant-scoped decision read model and is already the source for:

- `agents/src/governance/explainability.py`
- `gateway/src/routes/governance.ts`
- `frontend/src/app/governance/page.tsx`
- `database/migrations/09-agent-decisions.sql`

Approvals remain owned by `approvals`; their state is linked with
`agent_decisions.approval_id`. Audit access events remain owned by Kafka and
`audit_logs`. H2-4 extends the decision artifact contract and its existing
read path instead of duplicating approval or audit state.

## Context diagram

```text
Authenticated tenant user
          |
          v
Frontend governance decision view
          |
          v
Gateway tenant-scoped decision API ----> audit access event
          |
          v
agent_decisions read model <---- ExplainabilityEngine <---- agent policy/tool flow
          |                                    |                    |
          |                                    +---- approvals ------+
          |
          +---- PostgreSQL RLS tenant boundary
```

## Container boundaries

| Container | Owns | H2-4 responsibility | Must not own |
| --- | --- | --- | --- |
| agents | agent execution and sanitized decision recording | Emit bounded evidence summary and explicit outcome | Browser-facing authorization |
| PostgreSQL | decision and approval read models | Enforce RLS for every decision lookup | Raw model prompts or chain of thought |
| gateway | authenticated tenant API | Read, validate, authorize, and audit view access | Agent execution state |
| frontend | operator presentation | Render a safe trace timeline and degraded state | Tenant authorization decisions |
| OPA, Weaviate, Kafka | policy, retrieval, events | Provide inputs represented only by safe summaries | UI-specific persistence |

## Target decision artifact contract

`agent_decisions` stays the persisted record. The application-facing safe
projection should have this shape:

```json
{
  "id": "uuid",
  "tenantId": "uuid",
  "agent": "support",
  "actionType": "ticket.escalation",
  "provider": "deterministic|ollama|nvidia_nim",
  "model": "configured-model-or-null",
  "status": "completed|pending_approval|denied|degraded|failed",
  "durationMs": 42,
  "toolCalls": [{"name": "ticket_lookup", "outcome": "ok"}],
  "retrievalEvidence": [{"sourceId": "kb-123", "status": "used"}],
  "policyDecision": {"allowed": true, "requiresApproval": false},
  "approval": {"id": "uuid-or-null", "status": "pending|approved|rejected|null"},
  "outputValidation": {"status": "passed|blocked"},
  "createdAt": "ISO-8601"
}
```

The API must return this projection, not the raw `input_context`, `reasoning`,
`evidence`, or `tool_calls` JSON columns. A migration is only needed if the
existing JSON fields cannot store the bounded safe summary without ambiguity.

## Data flow

1. An agent evaluates the policy and calls tools under its existing tenant
   context.
2. The agent records a redacted decision artifact through
   `ExplainabilityEngine` with correlation and approval identifiers.
3. The gateway reads the artifact through `withTenantDb`, which sets the
   PostgreSQL tenant context and applies RLS.
4. The gateway transforms the stored artifact into the explicit safe
   projection, emits an audit-access event, and returns it to an authorized
   governance user.
5. The frontend renders status, tool outcome, evidence identifiers, policy,
   approval, validation, and duration. It never renders free-form reasoning.

## Required implementation slices

### Slice A: Safe artifact schema and redaction

- Define an allow-list for persisted and returned tool-call fields.
- Replace generic reasoning display with typed factors or a fixed safe summary.
- Redact nested secrets and reject prompt-like fields by name and size.
- Preserve provider metadata without API keys.
- Record `durationMs`, provider/model identifier, and explicit degradation
  reason code, never raw exception text.

### Slice B: Tenant-safe gateway projection

- Add a versioned endpoint such as `GET /api/v1/governance/agent-runs/:id` or
  extend the current decision endpoint with a safe-projection response.
- Require `admin`, `super_admin`, or `auditor` roles as the existing decision
  endpoints do.
- Query by both `id` and authenticated `tenantId`, with `withTenantDb`.
- Emit the existing audit access event for every read.

### Slice C: Frontend trace view

- Add a dedicated route `/agents/runs/[id]` or a linked detail view from the
  Governance Decisions tab.
- Render a fixed timeline: received, policy checked, retrieval/tool summary,
  approval, validation, final outcome.
- Use visible `degraded` and `denied` states; do not hide failures behind a
  generic success view.
- Treat all text returned by the API as untrusted display content.

### Slice D: Demonstrable scenarios and tests

- Completed or pending approval: includes a linked approval state.
- Denied: no response data, prompt, or cross-tenant evidence leaks.
- Weaviate unavailable: records `degraded` with a bounded reason code.
- Add gateway route tests, agent redaction tests, and frontend rendering tests.
- Add a cross-tenant lookup test proving `404` or equivalent non-disclosure.

## Security invariants

1. No chain of thought is persisted or rendered.
2. No API key, token, password, authorization header, or raw prompt is returned.
3. Decision lookup is tenant scoped in both the gateway query and PostgreSQL RLS.
4. An unavailable policy or retrieval dependency is explicit and fail-closed;
   it cannot become a completed action silently.
5. Approval state is referenced from `approvals`; it is not copied as an
   independently writable state machine.

## Failure modes and expected UI result

| Failure | Stored/API status | UI behavior | Safety action |
| --- | --- | --- | --- |
| OPA unavailable | denied or pending_approval | Policy check unavailable | No automatic action |
| Weaviate unavailable | degraded | Retrieval unavailable; answer limited | Do not imply evidence was used |
| Approval pending | pending_approval | Waiting for human approval | No mutation before approval |
| Output validation fails | denied | Output blocked | Do not reveal blocked content |
| Cross-tenant ID lookup | not found | Generic unavailable state | Do not disclose existence |
| Kafka audit publish failure | request failure or logged retry | Access not silently accepted | Follow existing event reliability policy |

## Scaling path

The first version queries a single decision by primary key and tenant. The
existing `(tenant_id, created_at)` and `(tenant_id, agent_id, created_at)`
indexes support the list view. If interview demo traffic becomes operational
traffic, add cursor pagination, a restricted retention policy for evidence
summaries, and an asynchronously maintained compact trace projection. Do not
fan out from the frontend to agents, OPA, or Weaviate.

## Verification plan

```text
agents: pytest agents/tests -k "explainability or approval"
gateway: npm test -- --runInBand
frontend: npm run build
infra: pytest tests/infra -q
compose: docker compose config --quiet
```

CI acceptance must include tests for redaction, tenant isolation, completed or
pending approval, denied, and Weaviate-degraded results. A local Docker
demonstration then records screenshots for H2-6 only after H2-4 is merged.

## Out of scope

- No new independent workflow, approval, or audit store.
- No display of chain-of-thought, prompts, full tool payloads, or raw errors.
- No NVIDIA API request is required for H2-4 acceptance.
- No Kubernetes or production deployment changes.
