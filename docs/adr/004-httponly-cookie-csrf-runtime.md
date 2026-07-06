# ADR-004: HttpOnly Refresh Cookie, CSRF, WS Ticket and Runtime URL

**Status:** Proposed — pending independent review  
**Date:** 2026-07-06  
**Scope:** Hardening 1.1 Group C  
**Supersedes:** localStorage-based refresh token storage, JWT-in-URL WebSocket authentication, build-time `NEXT_PUBLIC_API_URL`  
**Depends on:** ADR-002 (session revocation, Group B — unmodified)

---

## 1. Context

Group B solved session revocation with tenant-scoped `jti`/`sid`/`uv` keys, atomic
refresh rotation, and cross-instance WebSocket close. Two token-exposure surfaces
remain:

1. **localStorage**: both `accessToken` and `refreshToken` live in
   `window.localStorage`. An XSS payload can exfiltrate both, including the 7-day
   refresh token, and use it from any origin.
2. **WebSocket URL**: `ws://gateway:4000/ws?token=<full-jwt>` places the complete
   access token in the URL, where it can leak into server logs, proxy logs,
   browser history, and HTTP `Referer` headers.

The frontend also hardcodes `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL` at
build time, preventing runtime configuration and requiring a rebuild to change
environments.

Group C addresses all four issues without changing Group B revocation semantics.

---

## 2. Container Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ Browser                                                         │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ React SPA (Next.js client bundle)                        │  │
│  │                                                          │  │
│  │  accessToken  ← memory closure (lost on refresh)         │  │
│  │  NO refreshToken in JS (HttpOnly cookie, not readable)   │  │
│  │  csrfToken    ← read from non-HttpOnly cookie            │  │
│  │  user (AuthUser) ← React state only, not localStorage    │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  Cookies (browser-managed, not accessible to JS except csrf):   │
│                                                                 │
│  refresh_token:  HttpOnly  Secure  SameSite=Strict              │
│                  Path=/api/v1/auth  Max-Age=604800              │
│                                                                 │
│  csrf_token:     SameSite=Strict                                │
│                  Path=/api/v1/auth  Max-Age=604800              │
│                  (JS-readable; not HttpOnly)                    │
└─────────────────────────────────────────────────────────────────┘
           │ HTTPS  (same-origin: browser → Next.js)
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Next.js Server                                                  │
│                                                                 │
│  Rewrites:                                                      │
│    /api/v1/*  → http://gateway:4000/api/v1/*   (no path change) │
│    /ws        → http://gateway:4000/ws          (upgrade proxy)  │
│                                                                 │
│  Serves:                                                        │
│    /api/config → runtime { apiUrl, wsUrl }                      │
│    /*          → React SPA (App Router)                          │
└─────────────────────────────────────────────────────────────────┘
           │ HTTP  (internal container network)
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Gateway (Express)                                               │
│                                                                 │
│  Middleware stack:                                               │
│    Helmet → requestLogging →                                    │
│    originValidation  (/api/v1/auth/* POST — Origin allowlist)   │
│    csrfValidation    (/api/v1/auth/refresh POST — double-submit)│
│    corsMiddleware    (cross-origin mode only; Same-origin skips) │
│    authMiddleware    (Bearer token → revocation check)           │
│    tenantMiddleware  → opaMiddleware → auditMiddleware           │
│                                                                 │
│  Auth routes:                                                   │
│    POST /api/v1/auth/login    → Set-Cookie + JSON               │
│    POST /api/v1/auth/refresh  → Cookie-in / Set-Cookie-out      │
│    POST /api/v1/auth/logout   → clear cookie + revoke           │
│    POST /api/v1/auth/ws-ticket → one-time ticket                │
│                                                                 │
│  WebSocket:                                                     │
│    GET /ws?ticket=<uuid>  (replaces ?token=<jwt>)               │
│    → ticket consumed from Redis, auth metadata attached          │
│                                                                 │
│  Services:                                                      │
│    TokenRevocationService (Group B — unchanged)                 │
│    Redis client + subscriber (Group B — unchanged)              │
└─────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────┐    ┌──────────────────────────────────────┐
│ Redis               │    │ PostgreSQL                           │
│                     │    │                                      │
│ revoked:jti:*       │    │  users, tenants, user_roles          │
│ revoked:sid:*       │    │  (Group B unchanged)                 │
│ user version        │    │                                      │
│ refresh:consumed:*  │    │                                      │
│ ws:ticket:<uuid>    │    │                                      │
└─────────────────────┘    └──────────────────────────────────────┘
```

---

## 3. Data Flows

### 3.1 Login

```
Browser                    Next.js Proxy              Gateway
   │                            │                        │
   │  POST /api/v1/auth/login   │                        │
   │  {email, password, slug}   │                        │
   │ ──────────────────────────>│                        │
   │                            │  POST /api/v1/auth/login
   │                            │ ──────────────────────>│
   │                            │                        │
   │                            │   validate credentials │
   │                            │   read uv from Redis   │
   │                            │   generate sid (UUID)  │
   │                            │   fix sexp             │
   │                            │   sign access JWT      │
   │                            │   sign refresh JWT     │
   │                            │                        │
   │                            │  Set-Cookie:           │
   │                            │    refresh_token=...;  │
   │                            │    HttpOnly; Secure;   │
   │                            │    SameSite=Strict;    │
   │                            │    Path=/api/v1/auth;  │
   │                            │    Max-Age=604800      │
   │                            │  Set-Cookie:           │
   │                            │    csrf_token=<rand>;  │
   │                            │    SameSite=Strict;    │
   │                            │    Path=/api/v1/auth;  │
   │                            │    Max-Age=604800      │
   │                            │                        │
   │                            │  JSON body:            │
   │                            │    {accessToken, user} │
   │                            │<───────────────────────│
   │<───────────────────────────│                        │
   │                            │                        │
   │  store accessToken         │                        │
   │  in JS memory              │                        │
   │  (NOT localStorage)        │                        │
   │                            │                        │
   │  browser auto-stores       │                        │
   │  HttpOnly refresh_token    │                        │
   │  cookie (not JS-readable)  │                        │
   │                            │                        │
   │  clear any legacy          │                        │
   │  localStorage tokens       │                        │
```

### 3.2 Token Refresh

```
Browser                    Next.js Proxy              Gateway
   │                            │                        │
   │  POST /api/v1/auth/refresh │                        │
   │  headers:                  │                        │
   │    X-CSRF-Token: <value>   │                        │
   │    Content-Type: ...       │                        │
   │  Cookie:                   │                        │
   │    refresh_token=...       │                        │
   │    csrf_token=...          │                        │
   │  body: {}                  │                        │
   │  credentials: 'include'    │                        │
   │ ──────────────────────────>│                        │
   │                            │  (proxied, cookies     │
   │                            │   forwarded)           │
   │                            │ ──────────────────────>│
   │                            │                        │
   │                            │  CSRF double-submit:   │
   │                            │    header == cookie?   │
   │                            │  read refresh_token    │
   │                            │  from cookie           │
   │                            │  verify JWT signature  │
   │                            │  validate claims       │
   │                            │  consumeRefresh (Lua)  │
   │                            │  sign new pair         │
   │                            │                        │
   │                            │  Set-Cookie:           │
   │                            │    refresh_token=NEW   │
   │                            │    (rotated)           │
   │                            │  Set-Cookie:           │
   │                            │    csrf_token=NEW      │
   │                            │    (rotated)           │
   │                            │                        │
   │                            │  JSON body:            │
   │                            │    {accessToken}       │
   │                            │<───────────────────────│
   │<───────────────────────────│                        │
   │                            │                        │
   │  replace accessToken       │                        │
   │  in memory                 │                        │
```

### 3.3 Logout

```
Browser                    Next.js Proxy              Gateway
   │                            │                        │
   │  POST /api/v1/auth/logout  │                        │
   │  headers:                  │                        │
   │    Authorization: Bearer.. │                        │
   │    Origin: <host>          │                        │
   │  Cookie:                   │                        │
   │    refresh_token=...       │                        │
   │ ──────────────────────────>│ ──────────────────────>│
   │                            │                        │
   │                            │  validate access token │
   │                            │  read refresh cookie   │
   │                            │  verify refresh claims │
   │                            │  tenant/user/sid match │
   │                            │  revokeSid (sexp)      │
   │                            │  revokeJti (access)    │
   │                            │  publish event         │
   │                            │  close local WS        │
   │                            │                        │
   │                            │  Set-Cookie:           │
   │                            │    refresh_token=;     │
   │                            │    Max-Age=0           │
   │                            │  Set-Cookie:           │
   │                            │    csrf_token=;        │
   │                            │    Max-Age=0           │
   │                            │                        │
   │  JSON: {message}           │                        │
   │<───────────────────────────│<───────────────────────│
   │                            │                        │
   │  clear accessToken         │                        │
   │  from memory               │                        │
   │  clear legacy localStorage │                        │
```

### 3.4 WebSocket Connection (ticket flow)

```
Browser                    Next.js Proxy              Gateway
   │                            │                        │
   │  (1) POST /api/v1/auth/   │                        │
   │      ws-ticket             │                        │
   │  headers:                  │                        │
   │    Authorization: Bearer.. │                        │
   │ ──────────────────────────>│ ──────────────────────>│
   │                            │                        │
   │                            │  verify access token   │
   │                            │  generate ticket UUID  │
   │                            │  redis.set(             │
   │                            │    ws:ticket:<ticket>,  │
   │                            │    {tenantId, userId,  │
   │                            │     sid, sexp, roles}, │
   │                            │    'NX',               │
   │                            │    'EX', 10)           │
   │                            │                        │
   │  JSON: {ticket}            │                        │
   │<───────────────────────────│<───────────────────────│
   │                            │                        │
   │  (2) GET /ws?ticket=<uuid> │                        │
   │  Connection: Upgrade       │                        │
   │  (NO Authorization header) │                        │
   │ ──────────────────────────>│ ──────────────────────>│
   │                            │                        │
   │                            │  redis.getdel(          │
   │                            │    ws:ticket:<ticket>)  │
   │                            │  (single-use; GETDEL)  │
   │                            │                        │
   │                            │  if missing/expired:   │
   │                            │    close 4401          │
   │                            │                        │
   │                            │  attach auth metadata  │
   │                            │  to socket             │
   │                            │  (tenantId, userId,    │
   │                            │   sid, uv, sexp, roles)│
   │                            │                        │
   │  WebSocket upgrade         │                        │
   │<═══════════════════════════│<═══════════════════════│
```

### 3.5 Legacy localStorage Migration (one-time)

```
Browser (page load)
   │
   │  const rt = localStorage.getItem('refreshToken')
   │  const at = localStorage.getItem('accessToken')
   │  if (rt && at) {
   │    // POST /api/v1/auth/migrate-cookie
   │    //   body: { refreshToken: rt }
   │    //   credentials: 'include'
   │    // → Gateway reads body token, validates, issues cookies
   │    // → return { accessToken }
   │    // localStorage.removeItem('accessToken')
   │    // localStorage.removeItem('refreshToken')
   │    // localStorage.removeItem('authUser')
   │  }
```

---

## 4. Cookie Specification

### 4.1 refresh_token cookie

| Attribute | Value | Rationale |
|---|---|---|
| Name | `refresh_token` | Explicit purpose; avoids collisions |
| Value | Full refresh JWT | Only transmitted cookie-to-server, never exposed to JS |
| HttpOnly | `true` | Prevents XSS exfiltration |
| Secure | `true` in production/compose; `false` in local dev | HTTPS-only in deployed environments |
| SameSite | `Strict` in production; `Lax` in dev | `Strict` blocks all cross-site; `Lax` allows top-level navigations for local dev convenience |
| Path | `/api/v1/auth` | Scoped to auth endpoints; not sent on asset requests |
| Max-Age | `604800` (7 days) | Matches Group B refresh token TTL |
| Domain | Omit (defaults to issuing host) | Not shared across subdomains unless explicitly configured |

### 4.2 csrf_token cookie

| Attribute | Value | Rationale |
|---|---|---|
| Name | `csrf_token` | Standard CSRF double-submit naming |
| Value | 32-byte hex (`crypto.randomBytes(32).toString('hex')`) | High-entropy, unpredictable |
| HttpOnly | `false` | JS must read this to set `X-CSRF-Token` header |
| Secure | `true` in production/compose; `false` in local dev | Same policy as refresh_token |
| SameSite | `Strict` in production; `Lax` in dev | Same policy as refresh_token |
| Path | `/api/v1/auth` | Scoped to auth endpoints |
| Max-Age | `604800` (7 days) | Matches refresh token lifetime |
| Domain | Omit | Same scoping as refresh_token |

### 4.3 CSRF double-submit validation

On `POST /api/v1/auth/refresh`:

1. Read `X-CSRF-Token` header.
2. Read `csrf_token` cookie.
3. Both must be present and must be **identical**.
4. Mismatch or absence → `403 { error: "CSRF validation failed" }`.

This is stateless: the server does not store CSRF tokens. Security comes from
the Same-Origin Policy: a cross-origin page cannot read the victim site's
cookies, so it cannot set the matching header.

### 4.4 Cookie rotation on refresh

Every successful refresh rotates both cookies:

1. Generate new refresh JWT (new `jti`; same `sid`, `uv`, `sexp`).
2. Generate new `csrf_token` random value.
3. Set both via `Set-Cookie` headers in the refresh response.
4. Old refresh JWT already consumed by Group B Lua script.

Rotation bounds the blast radius of a leaked CSRF token to one refresh cycle.

---

## 5. Origin Validation

### 5.1 Trusted origins

Configured via `ALLOWED_ORIGINS` environment variable (comma-separated):

| Environment | Value |
|---|---|
| Local dev | `http://localhost:3000` |
| Compose | `http://localhost:3000,http://localhost:3001` |
| Helm staging | `https://crm-staging.example.com` |
| Production | `https://crm.example.com` |

### 5.2 Validation rules

For every `POST /api/v1/auth/*` request:

1. Read `Origin` header.
2. If present and not in `ALLOWED_ORIGINS` → `403`.
3. If absent (same-origin by browser, or non-browser client) → allow; defense-in-depth
   is handled by CSRF for cookie-bearing requests.

For non-auth endpoints, CORS middleware already handles origin filtering (Group B).

### 5.3 Relationship to CORS

Same-origin mode (Next.js proxy, no cross-origin requests):

- CORS middleware is a no-op.
- CSRF + SameSite=Strict provide the security boundary.

Cross-origin mode (direct browser → Gateway):

- CORS middleware must return `Access-Control-Allow-Origin` matching the Origin.
- CORS must include `Access-Control-Allow-Credentials: true`.
- CSRF double-submit protects the refresh endpoint.
- SameSite=Lax/None required so browser sends cookies.

The ADR recommends same-origin proxy as the default. Cross-origin mode is
documented for deployments where the proxy is not feasible.

---

## 6. WebSocket Ticket Contract

### 6.1 Redis key

```
ws:ticket:{ticketId}
```

| Field | Value |
|---|---|
| Key | `ws:ticket:{uuid}` |
| Value | JSON: `{ tenantId, userId, sid, sexp, uv, roles }` |
| Set with | `SET ... NX EX 10` (only if not exists, 10 second TTL) |
| Consumed with | `GETDEL` (atomic read + delete) |

### 6.2 Ticket properties

| Property | Value | Rationale |
|---|---|---|
| Lifetime | 10 seconds | Covers one RTT from ticket request to WS upgrade |
| Single-use | Yes (`GETDEL`) | Replayed ticket returns null |
| Tenant-bound | `tenantId` in value | Validated at subscribe time |
| Session-bound | `sid`, `sexp`, `uv` in value | Checked against revocation at heartbeat |
| Generation | `crypto.randomUUID()` | Same convention as Group B `jti` |

### 6.3 Ticket generation endpoint

`POST /api/v1/auth/ws-ticket`

- Requires valid access token (Bearer header).
- Returns `{ ticket: "<uuid>" }`.
- Rate-limited: 10 tickets per minute per user (prevents ticket flooding).
- Error responses:
  - No/invalid/expired token: `401`
  - Rate limit: `429`
  - Redis unavailable: `503`

### 6.4 Upgrade flow

`GET /ws?ticket=<uuid>`

1. Extract `ticket` query parameter.
2. `GETDEL ws:ticket:{ticket}` from Redis.
3. If null (expired or already used): close `4401`.
4. Parse JSON; validate tenant/user/sid fields.
5. Attach metadata to socket object (same fields as Group B connect).
6. Proceed with Group B heartbeat and revocation checks.

### 6.5 Multi-instance behavior

Ticket is stored in Redis (shared across instances). Client may receive ticket
from Gateway A, but connect to Gateway B. `GETDEL` is atomic, so the first
instance to process the upgrade consumes the ticket. This is safe because the
ticket is bound to the user's session metadata, not to a specific Gateway
process.

---

## 7. Access Token: Memory Only

### 7.1 Storage

The access token is stored in a JavaScript closure variable (module-scope
`let` in `api.ts`). It is never written to `localStorage`, `sessionStorage`,
or a cookie.

On page refresh the token is lost. The frontend must auto-refresh to obtain a
new access token before rendering protected content.

### 7.2 Refresh-on-boot

On application start (before rendering protected routes):

1. `POST /api/v1/auth/refresh` with `credentials: 'include'`.
2. If `200`: store new access token in memory; continue.
3. If `401`/`403`/`503`: no valid session; show login page.

This replaces the current `hasValidAccessToken()` localStorage check.

### 7.3 XSS impact change

| Group | XSS can steal | Lifetime |
|---|---|---|
| B (current) | `accessToken` + `refreshToken` from localStorage | Access: 1h, Refresh: 7d |
| C (proposed) | `accessToken` from memory (if XSS runs in same context) | Access: 1h, Refresh: NOT stealable |

The refresh token moves behind HttpOnly. An XSS payload can still use the
access token during its 1h window if it executes in the page context, but
cannot persist access beyond that window without the refresh cookie (which
requires a CSRF token the XSS would need to read from the csrf_token cookie,
which is possible since it is not HttpOnly — but the double-submit still
requires the Origin to be allowed).

---

## 8. Runtime URL Configuration

### 8.1 Problem

`NEXT_PUBLIC_API_URL` is inlined at build time by Next.js. Changing the API
endpoint requires a rebuild and redeployment.

### 8.2 Solution

**Same-origin proxy** (default):

- Next.js `rewrites` forward `/api/v1/*` to the Gateway.
- No absolute URL needed in frontend code; all requests are relative paths.
- `NEXT_PUBLIC_API_URL` becomes empty string or unset; frontend uses `/api/v1`
  as the base.

**Runtime config endpoint** (fallback for direct cross-origin):

- `GET /api/config` (served by Next.js, not proxied to Gateway).
- Returns `{ apiUrl: "<from env API_URL>", wsUrl: "<from env WS_URL>" }`.
- Frontend fetches this once at boot and uses it for subsequent requests.
- `API_URL` and `WS_URL` are server-side env vars (not `NEXT_PUBLIC_`).

### 8.3 WebSocket URL

In same-origin mode: `ws(s)://${window.location.host}/ws`.

In direct mode: value from runtime config.

---

## 9. Environment Configuration

### 9.1 Gateway environment

| Variable | Local Dev | Compose | Helm/Prod |
|---|---|---|---|
| `NODE_ENV` | `development` | `production` | `production` |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | `http://localhost:3000,http://localhost:3001` | `https://crm.example.com` |
| `COOKIE_SECURE` | `false` (auto from NODE_ENV) | `true` (auto) | `true` (auto) |
| `COOKIE_SAME_SITE` | `Lax` (auto) | `Strict` (auto) | `Strict` (auto) |
| `CSRF_ENABLED` | `true` | `true` | `true` |
| `WS_TICKET_TTL` | `10` | `10` | `10` |

`COOKIE_SECURE` and `COOKIE_SAME_SITE` are derived from `NODE_ENV` by default
but can be overridden for testing.

### 9.2 Frontend environment

| Variable | Local Dev | Compose | Helm/Prod |
|---|---|---|---|
| `NEXT_PUBLIC_API_URL` | *(unset — uses proxy)* | *(unset — uses proxy)* | *(unset — uses proxy)* |
| `NEXT_PUBLIC_WS_URL` | *(unset — uses proxy)* | *(unset — uses proxy)* | *(unset — uses proxy)* |
| `API_URL` | `http://localhost:4000` | `http://gateway:4000` | `http://gateway:4000` |
| `WS_URL` | `ws://localhost:4000` | `ws://gateway:4000` | `ws://gateway:4000` |

When `NEXT_PUBLIC_API_URL` is unset, the frontend uses same-origin relative
paths. `API_URL`/`WS_URL` are only used by the Next.js server for proxy
rewrites and `/api/config` fallback.

---

## 10. Failure Modes

| Scenario | Auth endpoint | Protected API | WS connect | WS heartbeat |
|---|---|---|---|---|
| Redis unavailable | 503 (login/refresh/logout) | 503 (Group B) | 503 or 1013 | 1013 (Group B) |
| Redis ticket write fails | — | — | 503 (ticket request) | — |
| CSRF mismatch | 403 (refresh) | N/A | N/A | N/A |
| Origin not allowed | 403 (auth POST) | CORS reject | N/A | N/A |
| Refresh cookie missing | 401 (refresh) | N/A | N/A | N/A |
| WS ticket expired | — | — | 4401 | — |
| WS ticket replayed | — | — | 4401 (null from GETDEL) | — |
| DB unavailable | 503 (login) | 503 (Group B) | N/A | — |
| CSRF cookie missing | 403 (refresh) | N/A | N/A | — |
| Network partition | 401 (auto-refresh fails) | 401 (stale access) | ticket 10s TTL bounds risk | 1013 (Group B) |

Recovery:

- Browser auto-refreshes access token via cookie on next page load after
  transient failure.
- WS client re-requests ticket and reconnects after 1013.
- CSRF token rotates on every refresh; stale tokens are expected during
  recovery and handled gracefully.

---

## 11. Multi-Instance Scaling

| Component | Shared state needed? | Mechanism |
|---|---|---|
| Access token validation | No | JWT is self-contained (signature + exp) |
| Refresh token (cookie) | No | JWT validated per-instance; rotation via atomic Lua (Group B) |
| CSRF double-submit | No | Stateless comparison; no server-side CSRF store |
| Refresh token revocation | Yes | Redis (Group B `auth:{tenantId}:revoked:*`) |
| User version | Yes | Redis (Group B `auth:{tenantId}:user:{userId}:version`) |
| WS ticket | Yes | Redis `ws:ticket:{id}` with NX + GETDEL |
| Pub/Sub revocation events | Yes | Redis Pub/Sub (Group B `auth:revocation:events`) |

No sticky sessions required. Any Gateway instance can validate any token and
issue a new pair. The atomic Lua script (Group B) ensures exactly-once refresh
consumption across instances.

---

## 12. Migration and Cleanup Strategy

### 12.1 Legacy localStorage migration

Users who logged in under Group B have `accessToken` and `refreshToken` in
localStorage. On first page load after Group C deployment:

1. Frontend checks for `localStorage.getItem('refreshToken')`.
2. If present, calls `POST /api/v1/auth/migrate-cookie` with the refresh token
   in the request body.
3. Gateway validates the token (same as current refresh logic), issues new
   token pair with HttpOnly cookie.
4. Frontend clears all localStorage auth keys.
5. If migration fails (token expired/revoked), clear localStorage and redirect
   to login.

### 12.2 Migration endpoint

`POST /api/v1/auth/migrate-cookie`

- Body: `{ refreshToken: "<jwt>" }`.
- Validates token exactly as the current (Group B) refresh endpoint.
- Returns: Set-Cookie (refresh_token, csrf_token) + JSON `{ accessToken }`.
- No CSRF required (one-time migration; no cookie exists yet).
- Origin validation still applies.
- **Temporary**: remove after one full refresh-token TTL cycle (7 days post-deploy).

### 12.3 localStorage cleanup

On every page load, frontend runs:

```javascript
for (const key of ['accessToken', 'refreshToken', 'authUser']) {
  localStorage.removeItem(key);
}
```

This is safe to run unconditionally. If the key does not exist, `removeItem`
is a no-op.

### 12.4 Rollback

If Group C must be rolled back to Group B:

1. The Group B code reads refresh tokens from the request body, which the
   frontend would need to provide again.
2. Users must re-authenticate (Group B re-auth requirement).
3. HttpOnly cookies from Group C are inert if the endpoint does not read them.
4. No Group B revocation state is affected (ADR-002 invariants preserved).

---

## 13. Security Invariants (new for Group C)

All Group B invariants (ADR-002 §2) remain in force. Group C adds:

1. Refresh token is never accessible to JavaScript (`HttpOnly`).
2. Refresh cookie is only sent over HTTPS in production (`Secure`).
3. Refresh cookie is never attached to cross-origin requests in production
   (`SameSite=Strict`).
4. Refresh endpoint requires valid CSRF double-submit on every request.
5. Origin header is validated for all auth POST endpoints.
6. Access token lives only in JavaScript memory, never in persistent storage.
7. No complete JWT appears in WebSocket URLs, logs, or error responses.
8. WS ticket is single-use (`GETDEL`) and expires in ≤ 10 seconds.
9. WS ticket is bound to tenant; cross-tenant ticket usage is rejected.
10. WS ticket works across Gateway instances (stored in shared Redis).
11. Legacy localStorage tokens are cleaned on first Group C page load.
12. Migration endpoint is removed after one TTL cycle.

---

## 14. Exit Test Matrix

| # | Test | Location | Criterion |
|---|---|---|---|
| 1 | Refresh token in HttpOnly cookie only | Gateway response headers | `Set-Cookie: refresh_token=...; HttpOnly` present; no `refreshToken` in JSON body |
| 2 | localStorage empty after login | Frontend E2E / manual | `localStorage.getItem('accessToken')` → `null`; same for `refreshToken` |
| 3 | Missing CSRF → 403 | Gateway Jest | `POST /refresh` without `X-CSRF-Token` → 403 |
| 4 | Wrong CSRF → 403 | Gateway Jest | `POST /refresh` with mismatched header/cookie → 403 |
| 5 | Disallowed Origin → 403 | Gateway Jest | `POST /login` with Origin not in allowlist → 403 |
| 6 | Refresh rotation passes | Redis integration test | Two refreshes produce distinct jtis; old consumed |
| 7 | Replay detection | Redis integration test | Second use of same refresh token → REPLAY + sid revoked |
| 8 | Logout closes HTTP+WS | Gateway test | Logout revokes sid; WS heartbeat detects and closes |
| 9 | Redis down → refresh 503 | Redis integration test | Broken Redis → `POST /refresh` → 503 |
| 10 | Redis down → logout 503 | Redis integration test | Broken Redis → `POST /logout` → 503 |
| 11 | Cookie Secure in production | Gateway config test | `NODE_ENV=production` → `cookie.secure === true` |
| 12 | API URL no build-time inline | Frontend build | `grep -r "localhost:4000" frontend/.next/` → no matches (except source maps) |
| 13 | WS ticket single-use | Redis integration | `GETDEL` on consumed ticket → null |
| 14 | WS ticket short TTL | Redis integration | Ticket key TTL ≤ 10 |
| 15 | WS ticket cross-tenant rejected | Gateway WS test | Ticket with tenantA metadata; subscribe to tenantB → 4403 |
| 16 | WS ticket cross-instance | Redis integration | Ticket stored by service A; consumed by service B |
| 17 | Gateway lint/tsc/Jest green | CI | 0 errors, 0 failures |
| 18 | Frontend lint/tsc/build green | CI | 0 errors, 0 failures |
| 19 | Compose smoke | CI | `docker compose config --quiet` passes |
| 20 | Legacy localStorage migration | Gateway test | Body refresh token → cookie issued → localStorage cleared |
| 21 | Self-review | `docs/self-review-group-c.md` | All sections complete |

---

## 15. Approval Gates

Implementation may begin only after reviewers accept:

- HttpOnly refresh cookie with SameSite/Secure derivation from NODE_ENV;
- CSRF double-submit on refresh endpoint (stateless, no server-side store);
- Origin validation on auth POST endpoints;
- WS ticket single-use, short TTL, cross-instance via Redis;
- migration endpoint with defined removal timeline;
- 10-second ticket TTL and 10/minute rate limit;
- same-origin proxy as default (cross-origin as documented alternative);
- Group B revocation semantics unchanged.
