# Group B Self-Review: Session/WS Revocation Model

**Branch:** `hardening/auth-session-revocation`
**Base:** `main@HEAD` (post-Group-A)
**ADR:** `docs/adr/002-auth-session-revocation.md`
**Plan:** `docs/adr/002-auth-session-revocation-plan.md`

---

## 1. Commits

| Commit | Message |
|---|---|
| `abb029e` | docs(auth): approve ADR-002 revocation contract |
| `3c83c68` | feat(auth): add tenant-scoped revocation service |
| `5f0c810` | feat(auth): enforce atomic refresh and session logout |
| `d0c69f1` | feat(ws): enforce cross-instance session revocation |
| `9bfe031` | fix(auth): address review findings |
| `6123474` | docs(auth): record Group B self-review |

## 2. Modified Files

| File | Status |
|---|---|
| `docs/adr/002-auth-session-revocation.md` | NEW — ADR design record |
| `docs/adr/002-auth-session-revocation-plan.md` | NEW — implementation plan |
| `gateway/src/services/authSession.ts` | NEW — TokenRevocationService, Lua script, key builders, TTL helpers, Pub/Sub subscriber |
| `gateway/src/middleware/auth.ts` | MODIFIED — new JWT claims (jti, sid, type, uv, sexp), algorithm pinning, DI via createAuthMiddleware, 503 for Redis errors |
| `gateway/src/routes/auth.ts` | MODIFIED — factory pattern, login/register reads user version, atomic refresh with Lua, verified logout, WS closure on logout |
| `gateway/src/services/websocket.ts` | MODIFIED — JWT claim validation at connect, subscribe re-check, heartbeat re-validation, jti/sid indexes, closeConnectionsByEvent |
| `gateway/src/index.ts` | MODIFIED — revocation service + subscriber initialization, shutdown |
| `gateway/src/tests/revocation.test.ts` | NEW — 38 unit tests for service, TTL, keys, pipeline errors |
| `gateway/src/tests/nodb_security.test.ts` | MODIFIED — updated for 503/401 fail-closed semantics |
| `gateway/src/tests/leads_mocked.test.ts` | MODIFIED — updated Redis mock + JWT claims |
| `gateway/src/tests/productivity.test.ts` | MODIFIED — updated generateToken signature |
| `docker-compose.yml` | MODIFIED — Redis AOF + noeviction |

## 3. Contracts

### JWT Claims

| Claim | Type | Required | Access Token | Refresh Token |
|---|---|---|---|---|
| `jti` | UUID | Yes | Generated per token | Generated per token |
| `sid` | UUID | Yes | Fixed at login | Same as access |
| `sub` | UUID | Yes | User ID | User ID |
| `tenantId` | UUID | Yes | Tenant boundary | Tenant boundary |
| `type` | `"access"`/`"refresh"` | Yes | `"access"` | `"refresh"` |
| `uv` | integer ≥ 0 | Yes | User version at login | Same as access |
| `sexp` | Unix sec | Yes | Absolute session expiry | Same as access |
| `iat`/`exp` | Unix sec | Yes | Standard | Min(configured, sexp) |

### Redis Keys

| Key | TTL | Purpose |
|---|---|---|
| `auth:{tenantId}:revoked:jti:{jti}` | `max(1, min(604800, exp - now + 60))` | Single-token revocation |
| `auth:{tenantId}:revoked:sid:{sid}` | Until `sexp` + skew | Session revocation |
| `auth:{tenantId}:refresh:consumed:{jti}` | Until refresh `exp` + skew | Replay detection |
| `auth:{tenantId}:user:{userId}:version` | No TTL | User generation counter |

### HTTP Status Codes

| Condition | Status Code |
|---|---|
| Missing/invalid/expired token | 401 |
| Token/session/user revoked | 401 |
| Redis unavailable | 503 `AUTH_DEPENDENCY_UNAVAILABLE` |
| Logout cannot persist | 503 |

### WebSocket Close Codes

| Code | Reason | When |
|---|---|---|
| 4001 | "Authentication required" / "Invalid token" | Token missing, bad signature, expired |
| 4401 | "Token revoked" / "Session revoked" / "Session terminated" | Revoked at connect, subscribe, or heartbeat |
| 4403 | "Forbidden" | Cross-tenant subscribe attempt |
| 1013 | "Service unavailable" | Redis unavailable during connect/subscribe/heartbeat |

## 4. Security Invariants Check

| Invariant | Status |
|---|---|
| No complete JWT in Redis keys | ✅ Keys use jti (UUID), never full JWT |
| No JWT in logs | ✅ Only userId/tenantId logged; Redis error messages sanitized |
| No JWT in error responses | ✅ Error responses contain no token, jti, or sid |
| No Redis KEYS/SCAN | ✅ All lookups are O(1) GET/EXISTS |
| All revocation keys have TTL | ✅ computedTtl with floor/ceiling |
| Refresh tokens are single-use | ✅ Lua script with NX; replay revokes sid |
| User revocation via increment | ✅ Atomic INCR; old sessions rejected; new login works |
| Fail-closed on Redis error | ✅ 503 for HTTP, 1013 for WS |
| Production cannot enable fallback | ✅ AUTH_ALLOW_OPENREDIS_FALLBACK removed |
| Same service for HTTP and WS | ✅ TokenRevocationService injected into both |
| Cross-instance revocation | ✅ Pub/Sub subscriber + closeConnectionsByEvent |
| No mutable singleton | ✅ Constructor injection via createAuthMiddleware/createAuthRoutes |

## 5. Test Results

### Unit Tests (no external deps)

```
Test Suites: 5 passed, 5 of 10 total (5 DB-dependent skipped)
Tests:       61 passed, 91 total (30 DB-dependent skipped)
```

| Test File | Passed | Notes |
|---|---|---|
| `revocation.test.ts` | 38 | Claims, TTL, keys, pipeline errors, revocation writes |
| `nodb_security.test.ts` | 12 | JWT secret, errorHandler, 503/401 fail-closed |
| `leads_mocked.test.ts` | 8 | Mocked-DB leads CRUD with new auth |
| `api.test.ts` | 2 | Login/refresh/logout shape (DB-dependent) |
| `auth_sql_injection.test.ts` | 1 | SQL injection rejection |
| `auth_redis_integration.test.ts` | 13 (with Redis) / 0 skipped | Real Redis: Lua atomicity, concurrent refresh, replay, user version, tenant isolation, Pub/Sub |

### Integration Tests (require CRM_DB_AVAILABLE + CRM_REDIS_AVAILABLE)

| Test | Status | Requirement |
|---|---|---|
| Revoked jti returns 401 | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Revoked sid rejects access | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Concurrent refresh atomicity | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Replay revokes sid | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| User revoke + new login | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Tenant isolation | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Redis outage fail-closed | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Pub/Sub event propagation | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| TTL correctness | ✅ `auth_redis_integration` | `CRM_REDIS_AVAILABLE=1` |
| Two-instance WS closure | ⏳ Requires two Gateway processes | CI enhancement needed |

## 6. Redis Durability Configuration

`docker-compose.yml`:
- `command: ["redis-server", "--appendonly", "yes", "--appendfsync", "always", "--maxmemory-policy", "noeviction"]`
- Persistent volume: `redis_data:/data`

Production Helm/Redis must provide:
- AOF persistence with `appendfsync always` (or `everysec` with acceptable loss window)
- `maxmemory-policy noeviction` (reject writes rather than evict revocation keys)
- Replica acknowledged-write policy for HA deployments
- Gateway readiness fails if Redis is unreachable

## 7. Migration and Rollback

- **Migration**: Forces reauthentication. Tokens without the new required claims (`jti`, `sid`, `type`, `uv`, `sexp`) are rejected.
- `AUTH_ACCEPT_LEGACY_TOKENS` is NOT implemented in this branch per ADR-002 decision to force reauthentication.
- **Rollback**: Requires JWT signing-key rotation and forced reauthentication. Old code cannot see new `auth:{tenantId}:*` revocation keys.

## 8. Known Limitations

| Limitation | Impact | ETA |
|---|---|---|
| WebSocket token in query string | Token visible in server logs, referrer headers | Group C |
| No integration tests in CI | Redis-dependent features not validated in CI | B5 CI job (pending) |
| No HttpOnly cookie for refresh | Token accessible to JS | Group C |
| No CSRF protection | POST endpoints could be targeted by CSRF | Group C |
| Pub/Sub is fire-and-forget | Missed events bounded by 30s heartbeat | B4 heartbeat mitigates |
| No metrics implementation | Per ADR-002 §11 (observability counters) | Post-hardening iteration |
| Lua script not loadable via SHA | Sent as text every call | Optimization for follow-up |

## 9. Boundary with Group C

The following are explicitly NOT implemented in Group B and remain for Group C:
- HttpOnly cookies for refresh token storage
- CSRF / Origin header validation
- Frontend runtime API/WS URL config
- Frontend localStorage token removal
- WebSocket token transport via cookie/header instead of query string

## 10. P0/P1/P2 Review

| ID | Finding | Status |
|---|---|---|
| P0-1 | Secret/connection string disclosure in 5xx | ✅ Unchanged (still sanitized) |
| P0-2 | Migration runner | ✅ Unchanged |
| P0-3 | CORS credentials | ✅ Unchanged |
| P0-4 | Healthcheck | ✅ Unchanged |
| P1-4 | Token jti/sid claims | ✅ Implemented |
| P1-5 | Refresh token rotation race | ✅ Implemented (Lua atomic) |
| P1-8 | WS token revocation | ✅ Implemented |
| P1-9 | User-level revocation | ✅ Implemented |
| P2-1 | Redis full-JWT key | ✅ Replaced with jti UUID |
| P2-3 | WS subscribe re-auth | ✅ Implemented |
| P2-5 | Fail-open on Redis error | ✅ Removed |
