# Phase 4 Proof Artifacts (Demo Steps)

## Pre-reqs
- Stack running: Postgres, Kafka, Redis, OPA, Gateway, Agents, Frontend, Prometheus, Grafana
- Tenant context available (UI login) and at least one lead/ticket event flow reachable

## 1) Kill Switch Demo (≤ 1s propagation)
1. Open Governance UI:
   - Navigate to `Frontend → /governance`
2. Click:
   - Pause This Tenant
3. Observe:
   - Agents stop executing actions for the paused tenant (no new side-effect events)
   - Kafka partition is paused/rewound in the agents orchestrator, so messages are not lost
4. Click:
   - Resume This Tenant
5. Observe:
   - Agents resume processing queued messages

Capture evidence only after the deterministic fixture is available. Use the
[interview capture checklist](interview/capture-checklist.md); do not use
placeholder screenshots as proof.

## 2) Human-in-the-loop Approval Demo
1. Trigger a HIGH-impact action path (example: lead qualification requiring approval):
   - Produce a lead.created event for the tenant (or use existing lead creation flow).
2. Observe:
   - An approval request appears in `Frontend → /approvals`
   - The Governance UI `Approvals` tab shows the same pending request
3. Approve in the Approvals UI.
4. Observe:
   - Agents receive `crm.approvals.decision`
   - The previously pending action is executed (e.g., `crm.leads.qualified` emitted)

Capture the pending and decided states from the current fixture and record the
approval ID in the accompanying evidence note.

## 3) Explainability Demo (Decision Artifacts)
1. Open Governance UI:
   - `Frontend → /governance`
2. Click `Decisions`, select a decision.
3. Observe:
   - Stored decision artifact with:
     - confidence
     - reasoning factors
     - evidence references (kafka topic + event id)

Capture only redacted decision summaries. Do not include model chain-of-thought,
private prompts, credentials, or raw customer data.

## 4) Telemetry + Dashboard Demo
1. Open Prometheus targets to confirm scrape:
   - Agents service exposes `/metrics`
2. Open Grafana and import the dashboard JSON:
   - `observability/grafana/agent-governance-dashboard.json`
3. Observe panels:
   - decision latency p95
   - tool calls/sec
   - approvals required/sec
   - policy violations/sec
   - kill switch activations/sec
   - agent errors/sec

Capture the dashboard from a successful current run and identify the commit and
scenario used to generate it.
