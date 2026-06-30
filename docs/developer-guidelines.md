# Developer Guidelines (Platform Safety)

These rules exist so multiple teams can extend the platform without breaking tenant isolation, governance, auditing, or operability.

## Non-negotiables

- Every request is tenant-scoped. No tenant_id in key/materialized state means it is unsafe.
- RLS stays enabled and enforced in Postgres.
- OPA authorization must guard user and agent actions.
- Sensitive actions must emit audit events.
- Governance controls (approvals, kill switch, AI data guard) must not be bypassed.

## Adding a new agent

Checklist:

- Add OPA policy checks for proposed actions and execution.
- Ensure actions are tenant-scoped and include actor identity.
- Emit audit events for any data mutation or access to PII-classified data.
- Respect kill switch and AI data guard before action execution.

## Adding a new event

Checklist:

- Define a stable event type name and payload schema version.
- Include `tenantid` in CloudEvent envelope and ensure it matches the acting tenant.
- Publish via the shared Kafka publisher.
- Ensure consumers are idempotent and can tolerate replays.

## Adding a new read model

Checklist:

- Create a tenant-scoped table with `(tenant_id, ...)` primary key or unique constraint.
- Ensure RLS policies apply (via `database/migrations/02-rls-policies.sql`).
- Projection logic must be deterministic and ordered.
- Include a rebuild path from durable event history (no manual correction).

## What not to bypass

- Do not access tenant data without setting `app.tenant_id` in Postgres sessions.
- Do not add cache keys that omit tenant and policy context.
- Do not implement “temporary” auth bypasses or allow-on-error behavior.
- Do not write PII into logs or audit old_value fields.

## Operational readiness for new changes

Before merging:

- Add at least one metric that proves the new path is healthy.
- Add at least one alert or dashboard panel if the change affects critical flows.
- Add or update a runbook if the change creates a new failure mode.
