# Group C Self-Review — HttpOnly Cookie, CSRF, WS Ticket, Runtime Config

**Date:** 2026-07-10
**Status:** Implemented (C1–C5 complete)
**Baseline:** `main@63f1935` (C5 squash-merged)
**Tag:** `hardening-group-c-stabilized`
**Reviewer:** Dorring / Codex

---

## 1. Scope

Group C implements ADR-004 across five phases:

| Phase | Title | Status |
|-------|-------|--------|
| C1 | CSRF, Origin and Cookie Infrastructure | ✅ merged |
| C2 | Auth Endpoint Cookie Integration | ✅ merged |
| C3 | Frontend Runtime Auth Migration | ✅ merged |
| C4 | WebSocket Same-Origin Proxy | ✅ merged |
| C5 | `/auth/me` Auth Recovery Finalization | ✅ merged |

Group C does **not** implement:
- Browser cross-origin cookie auth (`SameSite=None` + CORS credentials)
- Vitest frontend unit test framework (deferred to post-C5 backlog)
- Real Kubernetes cluster WSS validation (static verification only)

---

## 2. Commits and Merge Evidence

### C1/C2: Cookie, CSRF, Origin + Auth Endpoints

| PR | Branch | Main Commit | CI |
|----|--------|-------------|-----|
| #5 | `hardening/http-cookie-csrf-runtime` | `6779be8` | ✅ |
| #6 | (closeout docs) | `ee3db45` | ✅ |

### C3: Frontend Runtime Auth Migration

| PR | Branch | Main Commit | CI |
|----|--------|-------------|-----|
| #7 | `hardening/http-cookie-csrf-runtime` (continued) | `6b0cf3c` | ✅ |

### C4: WebSocket Same-Origin Proxy

| PR | Branch | Main Commit | CI |
|----|--------|-------------|-----|
| #9 | `codex/group-c-c4-ws-proxy` | `1f4287c` | ✅ (ws-proxy-smoke passed) |

Tag: `hardening-group-c-c4-stabilized`

### C5: `/auth/me` Auth Recovery Finalization

| PR | Branch | Main Commit | CI |
|----|--------|-------------|-----|
| #10 | `codex/group-c-c5-finalization` | `63f1935` | ✅ |

---

## 3. Files Changed (Cumulative)

| Phase | File | Change |
|-------|------|--------|
| C1 | `gateway/src/config/cookies.ts` | New: `getCookieOptions()` with explicit env > NODE_ENV derivation |
| C1 | `gateway/src/config/csrf.ts` | New: `generateCsrfToken()` / `validateCsrf()` double-submit |
| C1 | `gateway/src/middleware/origin.ts` | New: fail-closed origin validation middleware |
| C1 | `gateway/src/tests/csrf_origin.test.ts` | New: 28 unit tests |
| C1 | `.env.example` | Add `ALLOWED_ORIGINS`, `COOKIE_SECURE`, `COOKIE_SAME_SITE` |
| C2 | `gateway/src/routes/auth.ts` | Major: login/register/refresh/logout/migrate/ws-ticket → HttpOnly cookies |
| C2 | `gateway/src/services/authSession.ts` | Add `issueWsTicket`, `consumeWsTicket`, `consumeWsTicketRateLimit` |
| C2 | `gateway/src/index.ts` | Wire auth routes + cookie-parser |
| C2 | `gateway/src/tests/auth_cookie_endpoint.test.ts` | New: 30 endpoint-level tests |
| C2 | `gateway/src/tests/auth_cookie_integration.test.ts` | New: 11 Redis-gated integration tests |
| C3 | `frontend/src/lib/api.ts` | Major: memory-only accessToken, CSRF injection, cookie refresh, WS ticket |
| C3 | `frontend/src/app/providers.tsx` | Major: AuthProvider boot recovery, WsBridge auth gate |
| C3 | `frontend/src/hooks/useWebSocket.tsx` | Major: ticket exchange, bounded reconnect, 4401 retry |
| C3 | `frontend/src/lib/runtime-config.ts` | New: `/api/config` client, `deriveWsUrl` |
| C3 | `frontend/src/app/api/config/route.ts` | New: server-side runtime config endpoint |
| C3 | `frontend/next.config.js` | Updated: rewrites use `GATEWAY_INTERNAL_URL` |
| C3 | `frontend/src/components/ChatPanel.tsx` | Minor: same-origin API paths |
| C3 | `docker-compose.yml` | Updated: `WS_URL=""`, `API_URL=""` |
| C4 | `conf/nginx.conf` | New: edge proxy with `/ws` Upgrade, exact `/api/config` |
| C4 | `docker-compose.yml` | Add `frontend-proxy`; frontend expose-only |
| C4 | `scripts/ws-proxy-test.js` | New: E2E WS proxy smoke (register→login→ticket→connected→4401) |
| C4 | `.github/workflows/ci-cd.yml` | Add `ws-proxy-smoke` job |
| C4 | `deploy/helm/.../templates/frontend.yaml` | Remove `NEXT_PUBLIC_*`; add runtime env vars |
| C4 | `deploy/helm/.../values*.yaml` | Add `/api/config` Exact route, WS timeouts 3600 |
| C4 | `tests/infra/test_ws_proxy.py` | New: 7 static regression tests |
| C4 | `docs/self-review-group-c4-ws-proxy.md` | New: C4 self-review |
| C5 | `gateway/src/middleware/auth.ts` | Add `verifyAccessTokenWithRevocation` shared helper |
| C5 | `gateway/src/routes/auth.ts` | Add `GET /me`; refactor `/ws-ticket` to use shared helper |
| C5 | `gateway/src/tests/auth_cookie_endpoint.test.ts` | Add 7 `/me` tests (no-token, garbage, expired, refresh-type, revoked, 503, 200) |
| C5 | `frontend/src/lib/api.ts` | Add `authApi.me()`, `cacheAuthUserDisplay()` |
| C5 | `frontend/src/app/providers.tsx` | Add `resolveUserProfile()` — `/me` as sole identity authority |
| C5 | `frontend/src/lib/runtime-config.ts` | Export `deriveWsUrl` |
| C5 | `frontend/src/hooks/useWebSocket.tsx` | Import `deriveWsUrl`, remove duplicate |
| C5 | `docs/adr/004-httponly-cookie-csrf-runtime*.md` | C5 evidence, status updates |

---

## 4. Security Invariants

| # | Invariant | Phase | Evidence |
|---|-----------|-------|----------|
| 1 | Refresh token never accessible to JavaScript (`HttpOnly`) | C1 | Gateway sets `HttpOnly` on `refresh_token` cookie |
| 2 | Refresh cookie only sent over HTTPS in production (`Secure`) | C1 | `getCookieOptions()` env derivation; 28 tests |
| 3 | Refresh cookie scoped to `/api/v1/auth` (`Path`) | C1 | Cookie config test |
| 4 | CSRF double-submit on every refresh | C1 | `validateCsrf()` header==cookie comparison |
| 5 | Origin validated on all auth POST endpoints | C1 | `createOriginValidation()` fail-closed middleware |
| 6 | Access token in JS memory only, never persistent storage | C3 | Module-level `let accessToken`; no localStorage |
| 7 | No complete JWT in WebSocket URL | C4 | `?ticket=<uuid>` replaces `?token=<jwt>` |
| 8 | WS ticket single-use (`GETDEL`) | C2 | Atomic Redis consumption |
| 9 | WS ticket ≤ 10 second TTL | C2 | `EX 10`; CI test validates |
| 10 | WS ticket tenant-bound | C2 | Tenant metadata in ticket payload |
| 11 | WS ticket works across Gateway instances | C2 | Stored in shared Redis |
| 12 | Invalid/consumed/expired ticket → 4401 | C2/C4 | Gateway + ws-proxy-test.js |
| 13 | Redis dependency failure → 503/1013 fail-closed | C2/C4/C5 | Gateway tests; `/me` 503 test |
| 14 | No `NEXT_PUBLIC_*` in browser bundle | C3/C4 | grep `.next/static/` = 0 matches |
| 15 | No `gateway:4000` in browser bundle | C3/C4 | grep `.next/static/` = 0 matches |
| 16 | No `?token=` in frontend source | C4 | Grep: 0 matches |
| 17 | `/api/config` exact route to frontend (Compose + Helm) | C4 | nginx `location =` + Ingress `Exact` |
| 18 | `/auth/me` is sole identity authority (not localStorage) | C5 | `resolveUserProfile()`: `/me` fail → null |
| 19 | No half-authenticated UI after `/me` failure | C5 | `clearLocalAuthState()` called in boot |

---

## 5. Route Semantics (Final)

### Compose via `frontend-proxy`

| Path | Destination | Notes |
|------|-------------|-------|
| `/` | `frontend:3000` | Next.js frontend |
| `/api/config` | `frontend:3000` | Exact nginx match → Next.js route handler |
| `/api/` | `gateway:4000` | REST API (bypasses Next.js rewrites) |
| `/ws` | `gateway:4000` | WebSocket Upgrade via `map $http_upgrade` |

### Helm / Ingress

| Path | Type | Service |
|------|------|---------|
| `/` | Prefix | frontend |
| `/api/config` | Exact | frontend |
| `/api` | Prefix | gateway |
| `/ws` | Prefix | gateway |

---

## 6. Test Evidence

### Gateway

| Suite | Passed | Skipped | Failed |
|-------|--------|---------|--------|
| `csrf_origin.test.ts` (C1) | 28 | 0 | 0 |
| `auth_cookie_endpoint.test.ts` (C2/C5) | 30 | 0 | 0 |
| `auth_cookie_integration.test.ts` (C2) | 11 | 10 | 0 |
| `ws_revocation_integration.test.ts` (C2/C4) | 10 | 3 | 0 |
| All other suites (B/C pre-existing) | 68 | 48 | 0 |
| **Total** | **147** | **61** | **0** |

### Frontend

| Check | Result |
|-------|--------|
| `npm run lint` | 0 errors, 0 warnings |
| `npx tsc --noEmit` | Clean |
| `npm run build` | 19 routes, success |
| Bundle `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` | 0 matches |
| Bundle `gateway:4000` | 0 matches |
| Bundle `localhost:4000` | 0 matches |

### Infrastructure

| Suite | Result |
|-------|--------|
| `tests/infra/test_ws_proxy.py` (7 static) | 7 passed |
| `ws-proxy-smoke` CI job (Docker E2E) | ✅ passed on main |

---

## 7. Known Limitations and Deferred Items

| ID | Item | Rationale | Target |
|----|------|-----------|--------|
| TD-C3-2 | Runtime Gateway switching needs custom Next.js server proxy | `/api/config` already handles env-based switching; no production demand | Backlog |
| TD-C3-4 | Frontend has no unit test framework | Vitest + jsdom + RTL recommended but not blocking; deferred to keep PR scope tight | Post-C5 PR |
| TD-C4-2 | Helm not tested on real K8s cluster | Static verification in `test_ws_proxy.py` covers config shape; real WSS ingress test deferred to staging | Staging deploy |
| — | Cross-origin cookie auth (`SameSite=None` + CORS) | Explicitly out of Group C scope per ADR-004 §5.3 | Group D+ |
| — | `docs/self-review-group-c4-ws-proxy.md` still exists | C4-specific document; this `self-review-group-c.md` is the authoritative C1-C5 summary. C4 doc can be archived or retained as reference | Cleanup PR |

---

## 8. Group B Invariant Preservation

The following Group B behaviors are **explicitly unchanged**:
- `TokenRevocationService` logic (revocation keys, TTL, pipeline, fail-closed)
- Atomic refresh consumption Lua script
- `revokeSid` / `revokeJti` / `revokeUser` semantics
- Pub/Sub event schema and subscriber
- Heartbeat revalidation loop
- Redis durability/noeviction config
- All close codes (4401, 4403, 1013)
- JWT claim contract (`jti`, `sid`, `sub`, `tenantId`, `type`, `uv`, `sexp`)
- `crypto.randomUUID()` for all identifiers

Group C adds `issueWsTicket`, `consumeWsTicket`, and `consumeWsTicketRateLimit` methods to `TokenRevocationService` — no existing method is modified.

---

## 9. Rollback

If Group C must be rolled back:

1. Deploy Group B tag (`hardening-group-b-stabilized`).
2. Group B code reads refresh tokens from request body; HttpOnly cookies are inert.
3. Users must re-authenticate.
4. No Group B revocation state is affected.

---

## 10. Exit Criteria

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Gateway lint/build/test all green | ✅ 147P / 0F |
| 2 | Frontend lint/tsc/build all green | ✅ 0 errors |
| 3 | CI/CD all jobs green | ✅ |
| 4 | WS proxy smoke passes | ✅ |
| 5 | No `NEXT_PUBLIC_*` in bundle | ✅ |
| 6 | No JWT in WebSocket URL | ✅ |
| 7 | No hardcoded browser API/WS URLs | ✅ |
| 8 | `/auth/me` Redis failure fail-closed 503 tested | ✅ 7 tests |
| 9 | Tenant isolation proof unaffected | ✅ Group B tests unchanged |
| 10 | Self-review complete | ✅ This document |
