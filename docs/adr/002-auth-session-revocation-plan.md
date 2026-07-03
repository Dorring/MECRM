# ADR-002 Implementation Plan: Group B

**Status:** Ready for implementation after ADR approval  
**Target branch:** `hardening/auth-session-revocation`  
**Baseline:** latest `main` after Group A merge and green CI

## 1. Scope

Group B implements ADR-002 only:

- strict JWT claim/type validation;
- tenant-scoped jti, sid and user-generation revocation;
- atomic refresh consumption and replay-triggered family revocation;
- HTTP/login/refresh/logout fail-closed behavior;
- WebSocket connect, subscribe, heartbeat and cross-instance revocation;
- Redis durability/no-eviction configuration required by this security model;
- unit, integration, fault and two-instance tests.

Out of scope:

- HttpOnly cookies and CSRF;
- frontend localStorage changes;
- frontend runtime API/WS URL;
- replacing query-string WebSocket authentication;
- OAuth/OIDC;
- Kafka event history;
- general image or dependency upgrades.

## 2. Branch and commit discipline

Before editing application code:

```bash
git switch main
git pull --ff-only origin main
git switch -c hardening/auth-session-revocation
```

Do not implement on `main`. Keep commits scoped:

1. `docs(auth): approve ADR-002 revocation contract`
2. `feat(auth): add tenant-scoped revocation service`
3. `feat(auth): enforce atomic refresh and session logout`
4. `feat(ws): enforce cross-instance session revocation`
5. `test(auth): add Redis durability and revocation proofs`
6. `docs(auth): record Group B self-review`

Do not mix formatting, dependency upgrades or Group C work.

## 3. B1 — Claims, keys and revocation service

Expected files:

- `gateway/src/services/authSession.ts` (new)
- `gateway/src/middleware/auth.ts`
- `gateway/src/config/*` as needed
- `.env.example`
- focused unit tests

Deliverables:

1. Define one strict decoded-token type and runtime validator.
2. Generate jti/sid with `crypto.randomUUID()`.
3. Add and validate `type`, `uv` and `sexp`.
4. Pin allowed JWT algorithm; retain existing issuer/audience only if currently
   configured, otherwise document a follow-up rather than inventing defaults.
5. Validate claims before any Redis key construction.
6. Implement tenant-scoped key builders in one module.
7. Implement TTL helpers with expiry, skew, floor and ceiling tests.
8. Implement pipelined revocation checks that reject null results,
   unexpected result counts and every tuple-level error.
9. Implement user-generation read/increment.
10. Construct the service through dependency injection; no mutable singleton.

Required B1 tests:

- malformed/missing claims rejected before Redis;
- refresh token rejected by access middleware;
- tenant A keys cannot affect tenant B;
- jti and sid revocation;
- user-version mismatch;
- user-version missing equals version zero;
- malformed user version fails closed;
- pipeline throw, null result and per-command error all fail closed;
- TTL boundaries;
- no complete JWT in keys/logs/errors.

Exit gate: lint, TypeScript build and B1 tests pass.

## 4. B2 — Atomic refresh and verified logout

Expected files:

- `gateway/src/services/authSession.ts`
- `gateway/src/routes/auth.ts`
- integration tests using real Redis

Deliverables:

1. Implement a versioned Lua script for atomic refresh consumption.
2. Script checks tenant jti/sid revocation, user generation and consumed jti.
3. First use writes the consumed key with expiry and returns `OK`.
4. Replay writes sid revocation until `sexp` and returns `REPLAY`.
5. Only `OK` can mint a token pair.
6. Rotation preserves sid, uv and sexp and creates new access/refresh jtis.
7. Login/register obtain the current user generation and create a fixed sexp.
8. Logout uses verified claims; supplied refresh token must match tenant, user
   and sid.
9. Logout closes local sockets and publishes only after durable Redis writes.
10. Redis dependency failures return 503 and never mint tokens or claim logout
    success.

Required B2 real-Redis tests:

- two concurrent refresh requests across two service instances: exactly one
  succeeds;
- replay revokes sid;
- replayed family cannot refresh again;
- logout is idempotent after a successful write;
- forged/decoded-only logout input cannot write keys;
- refresh token from a different tenant/user/sid cannot be attached to logout;
- user revoke rejects old session and immediate new login succeeds;
- session revocation lasts through absolute session expiry;
- Redis command/Lua error returns 503.

Exit gate: all B1/B2 tests pass with a real Redis process; skipped integration
tests do not count.

## 5. B3 — WebSocket enforcement

Expected files:

- `gateway/src/services/websocket.ts`
- composition/wiring code in `gateway/src/index.ts`
- WebSocket-focused tests

Deliverables:

1. Inject the same revocation service used by HTTP.
2. Check strict JWT claims and revocation at connection and subscription.
3. Store only validated auth metadata on sockets.
4. Maintain tenant/user/jti/sid indexes or bounded maps for efficient closure.
5. Remove sockets from every index on close/error.
6. Close known revoked/invalid credentials with 4401.
7. Close cross-tenant subscriptions with 4403.
8. Close on indeterminate Redis state with 1013.
9. Do not log the WebSocket URL or token query.

Required B3 tests:

- revoked jti cannot connect;
- revoked sid cannot subscribe;
- cross-tenant subscription remains rejected;
- Redis outage rejects new connection/subscription;
- map/index cleanup occurs once on close/error;
- no token or token identifier appears in logs/errors.

## 6. B4 — Pub/Sub, heartbeat and multi-instance behavior

Expected files:

- `gateway/src/services/authSession.ts`
- `gateway/src/services/websocket.ts`
- `gateway/src/index.ts`
- observability configuration/tests

Deliverables:

1. Create command and subscriber Redis connections explicitly at startup.
2. Validate revocation event version, type, tenant and identifier at runtime.
3. Limit event payload size.
4. Close local sockets synchronously on the publishing instance.
5. Publish cross-instance notification and record publish failure metrics.
6. Track subscriber readiness, reconnects and permanent failure.
7. Add a non-overlapping heartbeat revalidation loop.
8. Batch checks or enforce bounded concurrency.
9. Treat every Redis tuple/connection error as indeterminate and close checked
   sockets with 1013.
10. Implement ordered shutdown.

Required B4 tests:

- revocation on Gateway A closes matching socket on Gateway B;
- unrelated tenant/user sockets stay open;
- dropped Pub/Sub notification is caught by heartbeat within the documented
  bound;
- subscriber disconnect/reconnect;
- heartbeat runs do not overlap;
- Redis loss during an established connection closes it;
- malformed/oversized Pub/Sub event is ignored and metered.

The two-instance test must start two actual Gateway processes sharing one real
Redis. Two objects inside one process are insufficient evidence.

## 7. B5 — Redis durability and deployment configuration

Expected files may include:

- `docker-compose.yml`
- `.env.example`
- Helm Redis values or production prerequisites
- CI workflow/integration scripts
- `docs/self-review-group-b.md`

Deliverables:

1. Compose Redis uses a persistent volume, AOF, `appendfsync always` and
   `noeviction`.
2. Production Helm/external Redis requirements explicitly enforce or document
   equivalent durability, HA and acknowledged writes.
3. Gateway readiness reports command Redis failure and subscriber degradation.
4. Add metrics listed by ADR-002.
5. Add CI jobs/profiles that actually run real Redis tests.
6. Do not hide integration tests behind an unset flag in the required Group B
   workflow.

Required B5 fault tests:

- revoke token, restart Redis, token remains rejected;
- increment user generation, restart Redis, old session remains rejected and a
  new login works;
- Redis stop causes HTTP 503 and WS 1013;
- Redis recovery restores service without accepting revoked state;
- simulated write/OOM failure fails closed;
- eviction policy is `noeviction`;
- AOF/persistence configuration is active, not merely present in YAML.

Exit gate: local Docker and CI fault tests pass.

## 8. Stable external contracts

HTTP:

| Condition | Status |
|---|---|
| Missing, invalid, expired or known-revoked credential | 401 |
| Redis state cannot be determined | 503 `AUTH_DEPENDENCY_UNAVAILABLE` |
| Logout revocation cannot be persisted | 503 |

WebSocket:

| Condition | Close code |
|---|---|
| Invalid or revoked credential | 4401 |
| Cross-tenant/forbidden subscription | 4403 |
| Redis dependency unavailable | 1013 |

No response or close reason contains token, jti, sid, Redis key or topology.

## 9. Migration and rollback

Deployment forces reauthentication:

- production rejects tokens without the new required claims;
- production validation rejects `AUTH_ACCEPT_LEGACY_TOKENS=true`;
- no `blacklist:<full-jwt>` dual-write is introduced;
- old blacklist entries expire naturally without scan.

Rollback to old auth code requires JWT signing-key rotation and forced
reauthentication because old code cannot see new revocation keys. The PR and
runbook must call this out explicitly.

## 10. Verification matrix

CC must record actual commands and counts for:

- Gateway lint;
- Gateway TypeScript build;
- complete Gateway Jest suite;
- real-Redis B1/B2 integration suite;
- two-process/two-instance WebSocket suite;
- Redis stop/start and restart persistence suite;
- Compose config;
- Helm lint/template for default, staging and production;
- Batch 1 authenticated smoke test;
- tenant-isolation proof suite.

For every suite report passed, failed and skipped counts. A required test that
is skipped means the exit gate is not met.

## 11. Independent review checklist

Before requesting merge, review the diff for:

- `jwt.decode()` used as authorization input;
- complete JWT in Redis/log/error/metric;
- non-tenant-scoped auth keys;
- refresh check-then-set races;
- pipeline tuple errors ignored;
- Redis error converted into a cache miss;
- refresh expiry extending past sexp;
- Pub/Sub used as source of truth;
- subscriber connection reused for commands;
- mutable singleton initialization order;
- unbounded or overlapping heartbeat work;
- sockets retained after close;
- production legacy-token fallback;
- Redis eviction or restart resurrecting revoked tokens.

## 12. Required self-review

Create `docs/self-review-group-b.md` containing:

- commits and modified files;
- final JWT, Redis, HTTP and WS contracts;
- threat-model mapping to tests;
- actual command output summaries and skipped counts;
- two-instance evidence;
- Redis restart/eviction evidence;
- security and tenant-isolation review;
- rollback steps;
- known limitations and deferred Group C work.

CC must stop after pushing the implementation branch and PR. Do not merge,
move tags or begin Group C until independent review and main-branch CI pass.

