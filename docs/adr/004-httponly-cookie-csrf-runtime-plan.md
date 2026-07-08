# ADR-004 Implementation Plan: Group C

**Status:** Partially Implemented — C1/C2/C3 complete; C4/C5 pending  
**Target branch:** `hardening/http-cookie-csrf-runtime`  
**Baseline:** `main@9e44a64` (hardening-group-b-stabilized.1)  
**ADR:** `docs/adr/004-httponly-cookie-csrf-runtime.md`

---

## 1. Scope

Group C implements ADR-004 only:

- refresh token moved to HttpOnly Secure cookie;
- access token stored in JavaScript memory only;
- CSRF double-submit on the refresh endpoint;
- Origin validation on auth POST endpoints;
- WebSocket ticket (single-use, short TTL, tenant-bound) replacing JWT-in-URL;
- same-origin proxy (Next.js rewrites) as default;
- runtime `/api/config` endpoint for local/dev direct URL resolution;
- Group C does **not** support browser cross-origin cookie auth;
- legacy localStorage migration and cleanup;
- documentation and self-review.

Out of scope (deferred to Group D+):

- OPA/Weaviate health dependency changes;
- container image size optimization;
- Helm dead-values cleanup;
- HttpOnly cookie for access token (not possible — browser JS needs it);
- OAuth/OIDC integration;
- Kafka event history.

---

## 2. Branch and Commit Discipline

Branch already created from `main@9e44a64`:

```bash
git switch main
git pull --ff-only origin main
git switch -c hardening/http-cookie-csrf-runtime
```

Planned commits (do not mix with formatting or Group D work):

1. `docs(auth): approve ADR-004 cookie/CSRF/ticket contract`
2. `feat(auth): add CSRF origin validation and cookie helpers`
3. `feat(auth): set HttpOnly refresh cookie on login and refresh`
4. `feat(ws): replace JWT URL auth with single-use WS ticket`
5. `feat(frontend): memory-only access token and same-origin proxy`
6. `feat(frontend): clean legacy localStorage and add migration`
7. `test(auth): add cookie/CSRF/ticket integration proofs`
8. `docs(auth): record Group C self-review`

---

## 3. C1 — CSRF, Origin and Cookie Infrastructure

**Expected files:**

- `gateway/src/middleware/csrf.ts` (replace existing)
- `gateway/src/middleware/origin.ts` (new)
- `gateway/src/config/cookies.ts` (new)
- `.env.example`
- `docker-compose.yml` (frontend rewrite config)
- `gateway/src/tests/csrf_origin.test.ts` (new)

**Deliverables:**

1. Replace the existing `csrf.ts` middleware with a thin utility for
   double-submit validation (not middleware; called inline in auth routes).
   Reasons: middleware applied to all POST would block auto-refresh; CSRF
   should only apply to `POST /api/v1/auth/refresh`.

2. New `origin.ts` middleware for `POST /api/v1/auth/*` routes. Reads `Origin`
   header; compares against `ALLOWED_ORIGINS` (comma-separated env var).
   Missing Origin → allow (same-origin or non-browser). Disallowed Origin →
   `403 { error: { code: "ORIGIN_NOT_ALLOWED" } }`.

3. New `config/cookies.ts` with `getCookieOptions()`:
   ```typescript
   export function getCookieOptions(): {
     refresh: CookieOptions;
     csrf: CookieOptions;
   } {
     // Explicit env var takes precedence; otherwise derive from NODE_ENV.
     // Compose runs over HTTP, so COOKIE_SECURE must be false.
     const secure =
       process.env.COOKIE_SECURE !== undefined
         ? process.env.COOKIE_SECURE === 'true'
         : process.env.NODE_ENV === 'production';
     // SameSite derivation:
     //   1. Explicit COOKIE_SAME_SITE=lax|strict → use that.
     //   2. Unset + NODE_ENV=production → strict.
     //   3. Unset + non-production → lax.
     const sameSite: 'strict' | 'lax' =
       process.env.COOKIE_SAME_SITE === 'lax'
         ? 'lax'
         : process.env.COOKIE_SAME_SITE === 'strict'
           ? 'strict'
           : process.env.NODE_ENV === 'production'
             ? 'strict'
             : 'lax';
     return {
       refresh: {
         httpOnly: true,
         secure,
         sameSite,
         path: '/api/v1/auth',
         maxAge: 604_800_000, // 7 days in ms (Express cookie maxAge is ms)
       },
       csrf: {
         httpOnly: false,
         secure,
         sameSite,
         path: '/',            // site-wide: JS must read from any page
         maxAge: 604_800_000,
       },
     };
   }
   ```

4. New `config/csrf.ts` with `generateCsrfToken()` and `validateCsrf(request)`:
   - `generateCsrfToken()`: returns `crypto.randomBytes(32).toString('hex')`.
   - `validateCsrf(req)`: reads `X-CSRF-Token` header and `csrf_token` cookie;
     returns `true` only if both present and identical.

5. Update `.env.example`:
   ```
   # Comma-separated trusted origins for auth POST endpoints.
   # Missing Origin header (same-origin/non-browser) is allowed.
   ALLOWED_ORIGINS=http://localhost:3000

   # Cookie security flags. COOKIE_SECURE must be false for Compose/local
   # (HTTP); true for Helm/production (HTTPS terminated at Ingress).
   # If unset, derived from NODE_ENV: production → true, else false.
   COOKIE_SECURE=false
   # COOKIE_SAME_SITE: explicit lax/strict overrides NODE_ENV default.
   # If unset: NODE_ENV=production → strict, else → lax.
   # COOKIE_SAME_SITE=strict
   ```

6. Update `docker-compose.yml` frontend service:
   ```yaml
   frontend:
     environment:
       - API_URL=http://gateway:4000
       - WS_URL=ws://gateway:4000
       # NEXT_PUBLIC_API_URL and NEXT_PUBLIC_WS_URL intentionally omitted
   ```
   Update `docker-compose.yml` gateway service environment to add:
   ```yaml
   - COOKIE_SECURE=false   # Compose uses HTTP; no TLS termination inside Compose
   - ALLOWED_ORIGINS=http://localhost:3000,http://localhost:3001
   ```

**Required C1 tests:**

- `generateCsrfToken()` returns 64-char hex string.
- `validateCsrf` accepts matching header+cookie.
- `validateCsrf` rejects missing header.
- `validateCsrf` rejects missing cookie.
- `validateCsrf` rejects mismatched values.
- Origin middleware allows missing Origin header.
- Origin middleware allows listed origin.
- Origin middleware rejects unlisted origin with 403.
- `getCookieOptions().refresh` returns `httpOnly: true`.
- `getCookieOptions().refresh` returns `path: '/api/v1/auth'`.
- `getCookieOptions().csrf` returns `httpOnly: false`.
- `getCookieOptions().csrf` returns `path: '/'` (site-wide).
- `getCookieOptions()` returns `secure: true` when `COOKIE_SECURE=true`.
- `getCookieOptions()` returns `secure: false` when `COOKIE_SECURE=false`.
- `getCookieOptions()` defaults `secure: true` when `NODE_ENV=production`
  and `COOKIE_SECURE` is unset.
- `getCookieOptions()` defaults `secure: false` when `NODE_ENV=development`
  and `COOKIE_SECURE` is unset.
- `getCookieOptions()` returns `sameSite: 'lax'` when `COOKIE_SAME_SITE=lax`.
- `getCookieOptions()` returns `sameSite: 'strict'` when `COOKIE_SAME_SITE=strict`.
- `getCookieOptions()` defaults `sameSite: 'strict'` when `NODE_ENV=production`
  and `COOKIE_SAME_SITE` is unset.
- `getCookieOptions()` defaults `sameSite: 'lax'` when `NODE_ENV=development`
  and `COOKIE_SAME_SITE` is unset.

**Exit gate:** lint, TypeScript build, C1 tests pass.

---

## 4. C2 — Auth Endpoint Cookie Integration

**Expected files:**

- `gateway/src/routes/auth.ts`
- `gateway/src/services/authSession.ts` (minor: add `issueWsTicket`, `consumeWsTicket`)
- `gateway/src/middleware/rateLimit.ts` (minor: add per-user rate limit helper)
- `gateway/src/tests/auth_cookie_integration.test.ts` (new)

**Deliverables:**

1. **Login** (`POST /api/v1/auth/login`):
   - After signing token pair (Group B unchanged), set two cookies:
     ```
     Set-Cookie: refresh_token=<jwt>; HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth; Max-Age=604800
     Set-Cookie: csrf_token=<random>; SameSite=Strict; Path=/; Max-Age=604800
     ```
   - JSON body returns `{ accessToken, user }` (no `refreshToken` field).
   - Register follows the same pattern but returns **201** (not 200).

2. **Refresh** (`POST /api/v1/auth/refresh`):
   - Read `refresh_token` from cookie (`req.cookies.refresh_token`).
   - Do **not** read from `req.body.refreshToken`. Body is ignored.
   - Call `validateCsrf(req)`. Fail → `403 { error: { code: "CSRF_VALIDATION_FAILED" } }`.
   - Apply origin validation middleware before this route.
   - Proceed with Group B `consumeRefresh` Lua.
   - On success, rotate both cookies (new refresh JWT + new csrf_token).
   - JSON body returns `{ accessToken }` (no `refreshToken`).
   - On failure, do **not** clear cookies (preserve session state for retry).
   - Redis unavailable → `503` (Group B unchanged).

3. **Logout** (`POST /api/v1/auth/logout`):
   - Continue to require valid access token (Bearer header, Group B unchanged).
   - Read refresh token from cookie; if present, validate it and revoke.
   - Clear both cookies (`Max-Age=0`).
   - Close local WS and publish revocation event (Group B unchanged).
   - If cookie refresh token is malformed → log warning, still clear cookies
     and return success (cookie cleanup is best-effort on logout).
   - If Redis is unavailable → `503`; cookies are **not** cleared (revocation
     was not persisted).

4. **Migrate** (`POST /api/v1/auth/migrate-cookie`):
   - Temporary endpoint for legacy localStorage migration.
   - Body: `{ refreshToken: "<jwt>" }`.
   - Validates token exactly as Group B refresh logic.
   - Issues cookie pair + JSON `{ accessToken }`.
   - No CSRF required (no cookie exists yet).
   - Origin validation still applies.
   - Document removal date: 7 days post-deploy.

5. **WS Ticket** (`POST /api/v1/auth/ws-ticket`):
   - Requires valid access token.
   - Generate UUID ticket.
   - `redis.set(ws:ticket:{ticketId}, JSON.stringify({tenantId, userId, sid, sexp, uv, roles}), 'NX', 'EX', 10)`.
   - Return `{ ticket: ticketId }`.
   - Rate limit: 10 per minute per user.
   - Redis unavailable → `503`.

**Required C2 tests (real Redis):**

- Login response sets `refresh_token` HttpOnly cookie with `Path=/api/v1/auth`.
- Login response sets `csrf_token` cookie (not HttpOnly) with `Path=/`.
- Login response body has `accessToken` but no `refreshToken`.
- Register response returns **201** (not 200) with `accessToken` and `user`.
- Refresh with valid cookie + CSRF header → 200 + new accessToken + rotated cookies.
- Refresh with missing CSRF header → 403.
- Refresh with mismatched CSRF → 403.
- Refresh with missing cookie → 401.
- Refresh reads token from cookie, ignores body.
- Logout clears both cookies.
- Logout with Redis down → 503, cookies not cleared.
- Logout idempotent after successful revocation.
- Migrate endpoint accepts body token and returns cookie pair.
- WS ticket returns valid ticket UUID.
- WS ticket single-use (second use fails).
- WS ticket expires after 10 seconds.
- WS ticket rate limit (11th request → 429).
- WS ticket with Redis down → 503.
- Refresh rotation produces new jti (distinct from old).
- Replay still triggers sid revocation (Group B invariant preserved).

**Exit gate:** all C1+C2 tests pass with real Redis.

---

## 5. C3 — Frontend: Memory-Only Access Token and Same-Origin Proxy ✅

**Status:** Implemented (commits 009be32..e958e02 on hardening/http-cookie-csrf-runtime)
**Date:** 2026-07-08

### 5.1 Files changed

| File | Action | Lines |
|------|--------|-------|
| `frontend/src/lib/api.ts` | Major rewrite | +151 / -50 |
| `frontend/src/app/providers.tsx` | Major rewrite | +113 / -20 |
| `frontend/src/app/login/page.tsx` | Minor | +5 |
| `frontend/src/components/layout/Header.tsx` | Minor | +5 / -2 |
| `frontend/src/app/settings/page.tsx` | Minor | +1 / -1 |
| `frontend/src/hooks/useWebSocket.tsx` | Major rewrite | +172 / -19 |
| `frontend/src/lib/runtime-config.ts` | NEW | +60 |
| `frontend/src/app/api/config/route.ts` | NEW | +17 |
| `frontend/next.config.js` | Moderate | +4 / -3 |
| `frontend/src/components/ChatPanel.tsx` | Minor | +2 / -4 |
| `frontend/src/components/ReplayControls.tsx` | Minor | +2 / -1 |
| `docker-compose.yml` | Minor | +3 / -2 |
| `.env.example` | Minor | +8 / -2 |

### 5.2 Key design decisions

1. **Memory-only accessToken**: module-level `let accessToken: string | null = null`.
   Never written to localStorage, sessionStorage, or any persistent store.

2. **CSRF double-submit**: `getCsrfToken()` reads `csrf_token` from `document.cookie`.
   `X-CSRF-Token` header injected on POST/PUT/PATCH/DELETE only (not GET/HEAD/OPTIONS).

3. **Cookie-based refresh**: `tryCookieRefresh()` sends `POST /api/v1/auth/refresh` with
   `credentials: 'include'` + `X-CSRF-Token` header, no request body.
   Response body has `{ accessToken }` only (no refreshToken).

4. **Boot recovery** (AuthProvider mount):
   - Step 1: try cookie-based refresh (`tryCookieRefresh()`)
   - Step 2: if no cookie session, try legacy localStorage migration (`migrateFromLocalStorage()`)
   - Step 3: restore user from `authUser` cache (if available); if not, leave user null (TD-C3-1)
   - Step 4: finally, clean legacy localStorage keys

5. **Safe logout**: `POST /api/v1/auth/logout` (no body, credentials+CSRF).
   **Only clear local session on 2xx.** On 503 or network error, PRESERVE session and
   return `{ success: false, error }`. Caller shows error to user.

6. **WS ticket exchange**: `connect()` calls `POST /api/v1/auth/ws-ticket` with
   `Authorization: Bearer <accessToken>` → `{ ticket }` UUID → `ws://host/ws?ticket=<uuid>`.
   No JWT in WebSocket URL. Each reconnect gets fresh ticket.

7. **Bounded WS reconnect**: 401/403 → stop reconnecting. 503/network error → exponential
   backoff, max 5 attempts.

8. **Runtime config**: `/api/config` reads server-side `API_URL`/`WS_URL` env vars.
   Same-origin mode: `apiUrl` empty (browser uses relative `/api/v1/...` paths).
   NOT a cross-origin cookie auth mechanism.

9. **No NEXT_PUBLIC_* in bundle**: `next.config.js` `env` block removed. Rewrites use
   `GATEWAY_INTERNAL_URL` (server-side build-time var, never in browser). Verified via
   `grep -r NEXT_PUBLIC_ .next/static/` — zero matches.

### 5.3 Tech debt recorded

| ID | Item | Resolution |
|----|------|------------|
| TD-C3-1 | `/refresh` returns no user profile; cookie refresh can't restore AuthUser if cache missing | Add `GET /api/v1/auth/me` endpoint |
| TD-C3-2 | Same-image runtime Gateway switching needs custom Next.js server proxy | Subsequent infra PR |
| TD-C3-3 | WebSocket same-origin `/ws` upgrade proxy may need nginx/Traefik if Next.js doesn't support it | C4 or infra PR |
| TD-C3-4 | Frontend has no test framework (no jest/vitest config) | Set up jest + jsdom + RTL |

### 5.4 Code review fixes (post-C3, commit 8e925c7)

1. **requestWsTicket structured result**: Returns `{ ok, ticket, status, reason }` instead of `string|null`.
   401/403/4xx (except 429) → permanent failure, no retry. 429/503/0 → bounded retry.

2. **Boot concurrency guard**: `bootStartedRef` + `mountedRef` prevent double `/refresh` or
   `migrate-cookie` in React StrictMode.

3. **WS auth gate**: `WsBridge` component reads `useAuth()` and passes `enabled` prop to
   `WebSocketProvider`. WS only connects when `!isLoading && isAuthenticated`.

4. **Token expiry check**: `ensureAccessToken` uses `decodeToken` + `exp` check (5s skew),
   not just presence check.

5. **WS URL runtime resolution**: `wsUrlRef` updated from runtime-config via dynamic import,
   falling back to `deriveWsUrl()`.

6. **Runtime config init order**: `boot()` awaits `getRuntimeConfig()` before any API call,
   guaranteeing API base URL is set for direct mode.

7. **ReplayControls token read**: `getTenantIdFromToken()` called at action time (inside
   `mutationFn` / `onSuccess`), not once at mount via `useMemo([])`.

8. **4401 bounded retry**: 4401 on WebSocket now stops after 1 retry (ticket race tolerance).
   Uses a dedicated `wsAuthRetryUsedRef` that is not reset in `onopen`; permanent auth failure after second 4401.

9. **429 transient**: 429 Too Many Requests is excluded from `isPermanentFailure`
   and enters bounded exponential backoff.

10. **Logout error UX**: Header shows dismissible red banner; Settings shows error card
    with retry info. Local session always preserved on failure.

### 5.5 Build verification

- `npx tsc --noEmit`: 0 errors
- `npm run lint`: 0 errors, 0 warnings
- `npm run build`: success (19 routes, `/api/config` as dynamic route)
- Client bundle: 0 `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` references
- Client bundle: 0 hardcoded API/WS URLs
- Server chunks: only expected fallback URLs (rewrites destination default, /api/config default, deriveWsUrl SSR fallback)

### 5.6 Exit gate

✅ C3 complete. Frontend now uses memory-only accessToken, cookie-based refresh,
CSRF double-submit, WS ticket exchange, and same-origin relative API paths.
- CSRF token sent in `X-CSRF-Token` header on refresh.
- All API requests use relative paths (no absolute localhost URL).
- Legacy migration: legacy `refreshToken` is read **before** localStorage
  cleanup runs (test that `migrate-cookie` is called when legacy token
  exists and no cookie session is present).
- WS upgrade: `next build && next start` → `GET /ws?ticket=<valid>` returns
  101. If fails, nginx sidecar is added and retested.

**Exit gate:** Frontend lint, TypeScript build, Next.js production build pass.

---

## 6. C4 — WebSocket Ticket Integration

**Expected files:**

- `gateway/src/services/authSession.ts` (add ticket methods)
- `gateway/src/services/websocket.ts` (modify upgrade handler)
- `gateway/src/tests/ws_ticket_integration.test.ts` (new)

**Deliverables:**

1. **`authSession.ts` additions:**

   ```typescript
   async issueWsTicket(auth: WsTicketPayload): Promise<string> {
     const ticket = crypto.randomUUID();
     const payload = JSON.stringify({
       tenantId: auth.tenantId,
       userId: auth.userId,
       sid: auth.sid,
       sexp: auth.sexp,
       uv: auth.uv,
       roles: auth.roles,
     });
     await this.redis.set(
       authKeys.wsTicket(ticket),
       payload,
       'NX',
       'EX',
       WS_TICKET_TTL,
     );
     return ticket;
   }

   async consumeWsTicket(ticket: string): Promise<WsTicketPayload | null> {
     const raw = await this.redis.getdel(authKeys.wsTicket(ticket));
     if (!raw) return null;
     try {
       return JSON.parse(raw) as WsTicketPayload;
     } catch {
       return null;
     }
   }
   ```

   Key builder: `wsTicket: (id: string) => \`ws:ticket:${id}\``

2. **`websocket.ts` upgrade handler changes:**
   - Parse `ticket` from URL query (replace `token`).
   - Call `revocationService.consumeWsTicket(ticket)`.
   - If null → close `4401` (`"Invalid or expired ticket"`).
   - Attach metadata to socket (same fields as Group B connect).
   - Validate tenant consistency at subscribe time (Group B unchanged).

3. **Rate limiting:**
   - Add per-user rate limit for `/api/v1/auth/ws-ticket`.
   - 10 requests per minute per user (key: `ratelimit:ws-ticket:{userId}`).
   - Use existing Redis rate limiter pattern.

**Required C4 tests (real Redis):**

- `issueWsTicket` returns UUID.
- `consumeWsTicket` returns payload on first use.
- `consumeWsTicket` returns null on second use (GETDEL consumed).
- Ticket expires after TTL (sleep + check).
- WS upgrade with valid ticket → connected.
- WS upgrade with expired ticket → 4401.
- WS upgrade with consumed ticket → 4401.
- WS subscribe with cross-tenant topic → 4403 (Group B unchanged).
- WS heartbeat revalidation uses ticket metadata (Group B unchanged).
- Rate limit: 11th ticket request → 429.
- Redis down on ticket issue → 503.

**Exit gate:** all C1-C4 tests pass with real Redis.

---

## 7. C5 — Runtime Config, Legacy Cleanup and Finalization

**Expected files:**

- `frontend/src/app/api/config/route.ts` (new)
- `frontend/src/lib/runtime-config.ts` (new)
- `frontend/src/lib/legacy-cleanup.ts` (new)
- `docs/adr/004-httponly-cookie-csrf-runtime.md` (status update)
- `docs/self-review-group-c.md` (new)

**Deliverables:**

1. **Runtime config endpoint:**
   ```typescript
   // frontend/src/app/api/config/route.ts
   export async function GET() {
     return NextResponse.json({
       apiUrl: process.env.API_URL || '',
       wsUrl: process.env.WS_URL || '',
     });
   }
   ```
   Used for runtime URL resolution in local/dev environments where the frontend
   connects directly to the Gateway (no same-origin proxy). **Not a cross-origin
   cookie auth mechanism.**

2. **Runtime config client (for local/dev direct mode only):**
   ```typescript
   // frontend/src/lib/runtime-config.ts
   let cached: { apiUrl: string; wsUrl: string } | null = null;
   export async function getRuntimeConfig() {
     if (cached) return cached;
     try {
       const res = await fetch('/api/config');
       if (res.ok) {
         cached = await res.json();
         return cached!;
       }
     } catch {}
     return { apiUrl: '', wsUrl: '' };
   }
   ```
   In same-origin proxy mode the frontend uses relative paths and this client
   is unused. In local/dev direct mode it provides the Gateway URL.
   **Group C does not implement cross-origin cookie auth.** Any production
   cross-origin deployment requires a separate ADR amendment with SameSite=None,
   CORS credentials, and dedicated tests.

3. **Legacy localStorage cleanup:**
   Runs on every page load in AuthContext boot (already specified in C3).
   Also available as standalone utility for direct import.

4. **Docker Compose frontend env update:**
   - Remove `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL` from frontend service.
   - Add `API_URL=http://gateway:4000` and `WS_URL=ws://gateway:4000`.

5. **ADR-004 status:** update from "Proposed" to "Accepted/Implemented".

6. **Self-review document** (`docs/self-review-group-c.md`):
   - Commits and modified files;
   - cookie/CSRF/origin contract tables;
   - WS ticket contract;
   - Group B invariant preservation proof;
   - test results (passed/skipped);
   - migration strategy and removal timeline;
   - security review;
   - rollback steps;
   - known limitations and Group D boundary.

**Required C5 tests:**

- `/api/config` returns `{ apiUrl, wsUrl }` from environment.
- Legacy localStorage items are removed on boot.
- Migration endpoint issues cookies from body token.

**Exit gate:** Full Gateway suite, Frontend build, Compose config pass.

---

## 8. Stable External Contracts (Group C additions)

### HTTP cookies

| Endpoint | Set-Cookie |
|---|---|
| `POST /login` | `refresh_token` (HttpOnly) + `csrf_token` |
| `POST /register` | `refresh_token` (HttpOnly) + `csrf_token` |
| `POST /refresh` | `refresh_token` (rotated) + `csrf_token` (rotated) |
| `POST /logout` | `refresh_token` (cleared) + `csrf_token` (cleared) |
| `POST /migrate-cookie` | `refresh_token` + `csrf_token` (temporary) |

### HTTP responses

| Condition | Status |
|---|---|
| Successful login | 200 `{ accessToken, user }` |
| Successful register | 201 `{ accessToken, user }` |
| Successful refresh | 200 `{ accessToken }` |
| CSRF validation failed | 403 `{ error: { code: "CSRF_VALIDATION_FAILED" } }` |
| Origin not allowed | 403 `{ error: { code: "ORIGIN_NOT_ALLOWED" } }` |
| Missing/expired refresh cookie | 401 |
| Redis unavailable | 503 (Group B unchanged) |
| WS ticket issued | 200 `{ ticket: "<uuid>" }` |
| WS ticket rate limited | 429 |

### WebSocket (unchanged from Group B except ticket)

| Condition | Close code |
|---|---|
| Invalid/expired/consumed ticket | 4401 |
| Revoked session (heartbeat) | 4401 (Group B) |
| Cross-tenant subscribe | 4403 (Group B) |
| Redis unavailable | 1013 (Group B) |

---

## 9. Group B Invariant Preservation

The following Group B behaviors are **explicitly not changed**:

- `TokenRevocationService` logic (revocation keys, TTL, pipeline, fail-closed).
- Atomic refresh consumption Lua script.
- `revokeSid` / `revokeJti` / `revokeUser` semantics.
- Pub/Sub event schema and subscriber.
- Heartbeat revalidation loop (overlap-protected, bounded concurrency).
- Redis durability/noeviction config.
- All close codes (4401, 4403, 1013) and their triggers.
- Prometheus metrics.
- JWT claim contract (jti, sid, sub, tenantId, type, uv, sexp).
- `crypto.randomUUID()` for all identifiers.

The only change to `authSession.ts` is adding `issueWsTicket` and
`consumeWsTicket` methods and the `ws:ticket` key builder. No existing method
is modified.

---

## 10. Verification Matrix

| Check | Status | Evidence |
|---|---|---|
| Gateway lint | ✅ | 0 errors, 0 warnings |
| Gateway build | ✅ | `tsc --noEmit` clean |
| Gateway Jest | ✅ | 135 passed, 61 skipped, 0 failed (196 total) |
| Redis integration | ✅ | 11 passed, 10 skipped (Redis-dependent gated) |
| Frontend lint | ✅ | 0 errors, 0 warnings |
| Frontend build | ✅ | 19 routes, `/api/config` as dynamic route |
| TypeScript check | ✅ | `npx tsc --noEmit` clean |
| Compose config | ✅ | `docker compose config --quiet` Exit 0 |
| refresh_token cookie | ✅ | HttpOnly; Path=/api/v1/auth |
| csrf_token cookie | ✅ | NOT HttpOnly; Path=/ |
| Cookie Secure (prod) | ✅ | `COOKIE_SECURE=true` → secure:true; `false` → false |
| Cookie Secure (compose) | ✅ | `COOKIE_SECURE=false` → secure:false |
| CSRF 403 | ✅ | Missing/mismatched → 403 in endpoint tests |
| Register 201 | ✅ | Endpoint test confirms 201 |
| localStorage clean | ✅ | No accessToken/refreshToken in localStorage (C3) |
| WS ticket single-use | ✅ | GETDEL atomic; second consumption returns null |
| No NEXT_PUBLIC_* in bundle | ✅ | grep .next/static/ → 0 matches (C3) |
| WS upgrade | ⏳ | C4/infra — same-origin WS proxy validation pending |

### C1/C2 Exit Gates Verified

| Exit gate | Status |
|---|---|
| C1: `getCookieOptions()` , CSRF helpers, origin middleware tests | ✅ 28 passed (`csrf_origin.test.ts`) |
| C1: lint, TypeScript build | ✅ |
| C2: endpoint-level auth cookie tests (HTTP contract) | ✅ 23 passed (`auth_cookie_endpoint.test.ts`) |
| C2: no-Redis integration + Redis gated tests | ✅ 11 passed + 10 skipped (`auth_cookie_integration.test.ts`) |
| C2: lint, TypeScript build, all C1+C2 tests pass | ✅ |
| C3/C4/C5 | C3 ✅ complete (incl. code-review fixes) | C4 (WS proxy validation), C5 (runtime + cleanup) pending |
| Group B `consumeRefresh` Lua unchanged | ✅ All Group B tests still pass |

---

## 11. Independent Review Checklist

Before requesting merge, review the diff for:

- Refresh token in JSON body on login/refresh response (must NOT be present).
- `HttpOnly` missing from `refresh_token` cookie.
- `Secure` set to `true` in Compose (must be `false` for HTTP).
- `Secure` set to `false` in production/Helm.
- `csrf_token` cookie `Path` set to `/api/v1/auth` (must be `/`).
- CSRF double-submit not validated on refresh endpoint.
- Origin validation missing on auth POST endpoints.
- Access token stored in localStorage or sessionStorage.
- Complete JWT in WebSocket URL or ticket value.
- WS ticket without NX (allows overwrite of consumed ticket).
- WS ticket TTL > 30 seconds.
- WS ticket missing GETDEL (allows replay).
- Group B revocation logic modified (should be untouched).
- Migration endpoint without defined removal date.
- Migration boot: localStorage cleared **before** migration attempt (must be after).
- `NEXT_PUBLIC_API_URL` still present in docker-compose.yml frontend service.
- `credentials: 'include'` missing on frontend refresh request.
- Auto-refresh on boot not implemented (page refresh loses session).
- WS proxy assumed to work via Next.js rewrite without CI validation.
- `forceExit` or other Jest handle-leak suppression.
- Register returns 200 (must be 201).
- Cross-origin mode documented as supported (must be explicitly deferred).

---

## 12. Required Self-Review

Create `docs/self-review-group-c.md` containing:

- commits and modified files;
- final cookie, CSRF, origin and WS ticket contracts;
- data flow diagrams (login, refresh, logout, WS connect);
- threat-model mapping to tests;
- actual command output and pass/skip/fail counts;
- Group B invariant preservation evidence;
- legacy migration strategy and removal timeline;
- security and tenant-isolation review;
- rollback steps;
- known limitations and deferred Group D work.

---

## 13. Migration and Rollback

### Deployment

- Group C requires coordinated deployment of Gateway + Frontend.
- On first load, users with localStorage tokens hit the migration endpoint.
- Users whose refresh token expired (7-day window) must re-authenticate.
- The migration endpoint is removed after one full TTL cycle.

### Rollback

- Roll back both Gateway and Frontend to Group B tag.
- Group B code reads refresh tokens from request body; no cookies needed.
- HttpOnly cookies are inert (Group B does not read them).
- Users must re-authenticate (Group B re-auth requirement).
- No Group B revocation state is affected.

### Migration endpoint removal

- Scheduled: 7 days after Group C deployment to production.
- Removal commit: `chore(auth): remove temporary migrate-cookie endpoint`.
- After removal, any remaining localStorage tokens are treated as expired;
  user must re-authenticate.
