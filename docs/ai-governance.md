# Phase 4: AI Governance + Agent Telemetry

## Goal
Make AI agents safe, controllable, explainable, and auditable for regulated environments, while preserving tenant isolation and low-latency control.

## Definitions
An AI action is any agent-initiated step that can impact data, customers, or operations:
- Command: requests a state change (e.g., “close deal”, “delete customer”).
- Tool call: calls an external/internal system (HTTP API, database write, message publish).
- Recommendation: proposes a decision that could lead to action (e.g., escalation, discount).

All AI actions are represented as a structured envelope:
- tenant_id (uuid)
- agent_id (string)
- action_type (string)
- resource (object, optional)
- inputs (object, redacted)
- risk_level (LOW | MEDIUM | HIGH)
- confidence (0.0–1.0)
- evidence (array of evidence references)
- correlation_id (string/uuid)

## Risk Classification
- LOW: auto-execute, audited
- MEDIUM: auto-execute + audited + policy enforced (OPA)
- HIGH: blocked until human approval is granted (non-negotiable)

Risk is derived from:
- action_type (intrinsic risk)
- resource sensitivity (e.g., deletes, PII access, financial thresholds)
- confidence (low-confidence escalates risk)
- policy decision (OPA can escalate risk or force approval)

## Approval Flow (OPA-Driven)
1) Agent prepares an action envelope.
2) Agent asks OPA for a decision:
   - allow/deny
   - requires_approval (boolean)
   - approvers (roles/users)
   - ttl_seconds (expiry)
3) If allow and not requires_approval:
   - execute action
   - record decision artifact
4) If requires_approval:
   - create approval record (tenant-scoped)
   - emit approval-required event
   - do not execute until an approval decision event is received
   - record “blocked pending approval” artifact
5) If denied:
   - do not execute
   - record “denied” artifact

Fail-closed: if OPA is unavailable or returns invalid output, treat as denied and require human approval.

## Kill Switch Semantics (≤ 1s propagation)
Kill switch supports:
- per-agent (stop a specific agent)
- per-tenant (pause/resume all agents for one tenant)
- global (super-admin emergency stop)

States:
- running: normal operation
- paused: finish in-flight message; do not accept new actions
- killed: stop accepting work immediately; do not execute side effects

Propagation model:
- Source of truth in Redis keys (tenant-scoped)
- Redis Pub/Sub broadcasts updates to all agent processes
- Agents cache the latest state in memory and check before every action

Guarantee:
- Agents check kill switch before processing a message and before any side-effectful step (LLM call, publish, DB write).
- The orchestrator uses manual Kafka commits; paused tenants do not commit offsets, so messages are not lost.

## Explainability Guarantees
Every decision produces a decision artifact with:
- inputs: redacted context used to decide (no raw prompts unless redacted)
- reasoning: structured factors (not free-form prompt text)
- confidence: numeric score
- evidence: references, not secrets (log event ids, kafka offsets, DB row identifiers)
- tool_calls: structured list (tool name, parameters summary, outcome)
- outcome: executed | denied | pending_approval | error | blocked_by_killswitch

Redaction rules:
- Do not store raw prompts by default.
- Strip/replace tokens/credentials/PII fields.
- Store hashes/pointers for sensitive payloads.

Auditability:
- Artifacts are persisted in Postgres with RLS by tenant_id.
- Artifacts are queryable by tenant admins and by super-admins (cross-tenant) only if explicitly allowed by policy.

## Telemetry Model (Prometheus)
Agents expose `/metrics` with per-tenant and per-agent labels where safe:
- decision_latency_ms (histogram)
- tool_call_count (counter)
- error_count (counter)
- approvals_required_total, approvals_denied_total (counters)
- kill_switch_activations_total (counter)
- policy_violations_total (counter)

Detecting drift / hallucination suspects:
- rising denial/approval-required rate for previously-low risk actions
- increasing latency/error spikes
- increasing policy violations or blocked-by-kill-switch events

## Tenant Isolation (Sacred)
- All governance records and decision artifacts include tenant_id.
- Redis keys are tenant-scoped unless global super-admin.
- Postgres tables enforce RLS by tenant_id.
- Governance UI is tenant-scoped by default; global controls require super-admin role and explicit policy allow.

## Migration & Rollout
Phase A (shadow mode):
- Record decision artifacts and metrics only (no blocking), validate policy outputs.
Phase B (enforcement mode):
- Enforce HIGH-risk approvals + kill switch guard at runtime.
Phase C (tightening):
- Expand OPA rules, require evidence fields for regulated actions, add more dashboards.

Rollback:
- Disable enforcement with config flag (keep auditing on).
- Kill switch remains available independently for incident response.

