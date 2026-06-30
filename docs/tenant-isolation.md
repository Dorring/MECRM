# Tenant Isolation (Phase 1 Proof)

## What Is a Tenant?

- A tenant is an isolated customer boundary identified by `tenant_id` (UUID).
- All tenant-scoped rows include a `tenant_id` column.

## Where `tenant_id` Comes From

- Source of truth: authentication token issued by Keycloak.
- Required claim: `tenant_id` (preferred). The gateway also accepts `tenantId` for backward compatibility and normalizes it to `tenant_id` internally.

## Tenant Context Propagation Flow

1. Next.js → Gateway
   - Sends `Authorization: Bearer <JWT>`.
2. Gateway
   - Extracts tenant from JWT claim (`tenant_id`/`tenantId`) and sets request tenant context.
   - Injects `x-tenant-id` for downstream services.
   - Calls OPA for authorization decisions.
3. FastAPI services (when present)
   - Read `x-tenant-id` and set DB tenant context per request.
4. PostgreSQL
   - Enforces Row Level Security (RLS) using `current_setting('app.tenant_id')::uuid`.

## Enforcement Points (Defense in Depth)

### A) Gateway Middleware

- Auth: JWT is required; tenant claim is required.
- Tenant strictness:
  - If tenant context is missing → reject early.
  - If `x-tenant-id` differs from token tenant → only allow for `super_admin`.
- Downstream propagation: `x-tenant-id` header is injected/overwritten by the gateway.

### B) OPA Decision

- The gateway sends OPA input with:
  - subject tenant id
  - resource tenant id (when known)
  - action string
- OPA denies cross-tenant access unless explicitly allowed for `super_admin` and action is permitted.

### C) Database RLS (Final Gate)

- Every request-scoped DB operation runs in a transaction that executes:
  - `SET LOCAL app.tenant_id = '<uuid>'`
- RLS policies require `tenant_id = current_setting('app.tenant_id')::uuid`.
- Fail-closed behavior:
  - If tenant context is missing/invalid, the query must not return tenant data (preferred: error).

## Threat Model

- JWT tenant_id tampering
- ID enumeration / guessing
- SQL injection
- Cache key collisions (cross-tenant cache pollution)
- WebSocket channel injection (subscribe to other-tenant channels)
- Missing tenant context (misconfigured service)

## Proof Plan (Threat → Test Case)

| Threat                      | Enforcement Layer(s) | Proof Test                                                                                                                                        |
| --------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| JWT tenant tampering        | Gateway + OPA        | `gateway/tests/test_rls_enforcement.ts`: token tenant A + header tenant B ⇒ 403                                                                   |
| ID enumeration              | Gateway + DB RLS     | `gateway/tests/test_rls_enforcement.ts`: tenant A requests B resource id ⇒ deny/404; `agents/tests/test_tenant_isolation.py` validates RLS blocks |
| SQL injection               | Gateway + DB RLS     | `agents/tests/test_tenant_isolation.py`: injection-like patterns do not bypass RLS                                                                |
| Cache collisions            | Redis keying         | `gateway/tests/test_rls_enforcement.ts`: tenant-scoped cache keys prevent cross-tenant reads                                                      |
| WebSocket channel injection | WS server routing    | `gateway/tests/test_rls_enforcement.ts`: tenant A cannot subscribe to tenant B channel                                                            |
| Missing tenant context      | DB RLS               | `agents/tests/test_tenant_isolation.py`: “Tenant Escape Kill Test” (no `SET LOCAL`) returns zero rows or errors                                   |
