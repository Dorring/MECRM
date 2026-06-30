# Integration Tests

This directory hosts the staging integration test suite referenced by the
`integration-tests` job in `.github/workflows/ci-cd.yml`. It runs against a
deployed staging environment (not against ephemeral containers) and is gated
behind the `STAGING_API_URL`, `TEST_USER_EMAIL`, and `TEST_USER_PASSWORD`
repository secrets — when any of those secrets is unset the CI job skips
cleanly instead of failing.

## Purpose

Per `docs/project-optimization-plan.md` Phase 8 (Test system & release
certification), the **Integration** layer validates real cross-service
interactions against live dependencies:

- PostgreSQL RLS enforcement through the Gateway.
- Redis cache behavior under the real tenant context.
- Kafka event flow: command → Outbox → consumer → read model projection.
- OPA policy decisions against the running OPA server.
- End-to-end auth: login, token refresh, session expiry, 401/403 handling.

These differ from the unit tests under `agents/tests/` and `gateway/test/`,
which run in-process against ephemeral services. The suite here targets a
deployed, integrated system.

## Current state

This directory is intentionally minimal today. It exists primarily so the CI
pipeline has a valid, runnable target instead of a dangling path reference
(Phase 0 P0: "CI references non-existent paths"). It ships with:

- `package.json` — a Node test harness so `npm ci` / `npm test` resolve.
- `health.test.js` — a placeholder smoke test that pings the staging API root
  and asserts a 2xx/3xx response. It is a starting point, not a coverage
  target.

## Planned test suite (Phase 5 + Phase 8)

The following scenarios will be added incrementally as the corresponding
phases land. Each should map to a documented user journey and emit a JUnit
report consumable by the release certification step.

| Scenario | Layer | Owner phase | Notes |
|---|---|---|---|
| Login + token refresh + logout | Auth | Phase 5 | Verifies token storage strategy and 401 refresh. |
| Lead create → event → read model | CQRS/Outbox | Phase 2 | Asserts projection appears within SLO. |
| Deal update + duplicate command | Idempotency | Phase 2 | Same `idempotencyKey` must not double-apply. |
| Cross-tenant CRUD denied | Tenant isolation | Phase 1 | 403 on foreign tenantId. |
| WebSocket subscribe + push | Realtime | Phase 5 | Auth + topic permission on connect. |
| Agent suggestion → approval → action | Governance | Phase 6 | Kill switch pauses new actions. |
| GDPR export + forget | Compliance | Phase 3 | No cross-tenant data in export. |
| Replay determinism | Event Store | Phase 2 | Read model matches baseline snapshot. |

## Running locally

```bash
# From the repo root, against a locally-running Gateway (port 4000):
cd tests/integration
npm install
API_URL=http://localhost:4000 npm test
```

For the full staging suite in CI, the secrets above must be configured on the
repository. Without them the job is skipped, which is the intended behavior
for forks and pre-staging branches.
