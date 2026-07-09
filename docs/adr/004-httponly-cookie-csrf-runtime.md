# ADR-004: HttpOnly Refresh Cookie, CSRF, WS Ticket and Runtime URL

**Status:** Partially Implemented — C1/C2/C3/C4 complete; C5 pending
**Date:** 2026-07-05 (approved), 2026-07-07 (C1/C2 implemented), 2026-07-08 (C3 implemented), 2026-07-09 (C3 merged, C4 implemented and merged)  
**Scope:** Hardening 1.1 Group C  
**Tag:** `hardening-group-c-c4-stabilized`  
**Supersedes:** localStorage-based refresh token storage, JWT-in-URL WebSocket authentication, build-time `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`  
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
│                  Path=/  Max-Age=604800                         │
│                  (JS-readable; not HttpOnly; visible site-wide) │
└─────────────────────────────────────────────────────────────────┘
           │ HTTPS  (same-origin: browser → Next.js)
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Next.js Server                                                  │
│                                                                 │
│  Rewrites (HTTP only):                                         │
│    /api/v1/*  → http://gateway:4000/api/v1/*   (no path change) │
│                                                                 │
│  WebSocket (/ws):                                               │
│    Proxied at infrastructure layer (nginx/Ingress/compose)      │
│    → http://gateway:4000/ws                                     │
│    NOT via Next.js rewrite (see §8.4)                           │
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
   │                            │    Path=/;             │
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
   │                            │    (rotated; Path=/)   │
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

### 3.5 Legacy localStorage Migration (one-time, on boot)

```
Browser (page load, before rendering protected routes)
   │
   │  // 1. Read legacy tokens into memory (BEFORE any cleanup)
   │  const legacyRefresh = localStorage.getItem('refreshToken')
   │  const legacyAccess  = localStorage.getItem('accessToken')
   │  const legacyUser    = localStorage.getItem('authUser')
   │
   │  try {
   │    // 2. First, try cookie-based refresh (for users who already migrated)
   │    const ok = await POST /api/v1/auth/refresh
   │                credentials: 'include'
   │                headers: { X-CSRF-Token: <from cookie> }
   │    if (ok) {
   │      // Cookie session valid — store access token in memory
   │      return  // skip migration, user already has cookie session
   │    }
   │
   │    // 3. No cookie session; check for legacy localStorage token
   │    if (legacyRefresh) {
   │      // 4. Migrate: send body token to migration endpoint
   │      const res = await POST /api/v1/auth/migrate-cookie
   │                   body: { refreshToken: legacyRefresh }
   │                   credentials: 'include'
   │      if (res.ok) {
   │        // Cookie issued — store new access token in memory
   │      }
   │      // If migration fails (token expired/revoked) → fall through to login
   │    }
   │  } finally {
   │    // 5. ALWAYS clear localStorage, regardless of migration outcome
   │    //    This prevents stale tokens from lingering.
   │    localStorage.removeItem('accessToken')
   │    localStorage.removeItem('refreshToken')
   │    localStorage.removeItem('authUser')
   │  }
```

**Critical ordering:** Legacy tokens are read **before** any cleanup. Cleanup
runs in `finally` only **after** migration is attempted or skipped. Never
clear localStorage before migration — that would destroy the refresh token
needed for migration.

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
| Path | `/` | JS must read cookie from any page to set `X-CSRF-Token` header; not restricted to auth endpoints |
| Max-Age | `604800` (7 days) | Matches refresh token lifetime |
| Domain | Omit | Same scoping as refresh_token |

**Why `Path=/` and not `Path=/api/v1/auth`:** The frontend reads `csrf_token` via
`document.cookie` from pages like `/dashboard` or `/settings`. If `Path` were
scoped to `/api/v1/auth`, the browser would not send the cookie on those pages,
and `document.cookie` would not see it. `Path=/` makes the cookie site-wide.
The `refresh_token` cookie can safely use `Path=/api/v1/auth` because it is
HttpOnly and never read by JavaScript — it is only sent on auth endpoint
requests, which is exactly where it is needed.

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

Same-origin mode (Next.js proxy, no cross-origin requests) — **Group C default**:

- CORS middleware is a no-op.
- CSRF + SameSite=Strict provide the security boundary.
- All auth endpoints, refresh, logout and WS ticket work via same-origin proxy.

**Cross-origin mode (direct browser → Gateway) — DEFERRED, not in Group C scope:**

- Requires `SameSite=None; Secure` on both cookies (browser sends cross-site).
- Requires full CORS credentials: `Access-Control-Allow-Origin` matching the
  specific Origin (not `*`), `Access-Control-Allow-Credentials: true`, and
  explicit `Access-Control-Allow-Headers` including `X-CSRF-Token` and
  `Authorization`.
- Requires additional tests: cross-origin preflight, cookie attachment with
  `credentials: 'include'`, CSRF validation with `SameSite=None`.
- Group C does **not** implement or test this mode. Deployments requiring
  cross-origin must implement it in a follow-up with dedicated ADR amendments
  and test coverage.

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

- Next.js `rewrites` forward `/api/v1/*` to the Gateway (HTTP requests only).
- No absolute URL needed in frontend code; all requests are relative paths.
- `NEXT_PUBLIC_API_URL` becomes empty string or unset; frontend uses `/api/v1`
  as the base.

**WebSocket proxy** (`/ws`):

- Next.js `rewrites` do **not** reliably support WebSocket upgrade across all
  deployment modes. The `/ws` path is proxied at the infrastructure layer:
  - **Compose:** a lightweight nginx sidecar or the built-in Next.js custom
    server forwards `/ws` to `http://gateway:4000/ws`. If Next.js rewrite is
    used, verify with a production build (`next build && next start`) that
    `/ws` upgrade actually works; if it fails, add a dedicated reverse proxy.
  - **Helm/production:** Ingress controller (nginx-ingress, Traefik) routes
    `/ws` to the Gateway service with WebSocket upgrade annotations.
  - **Local dev (no Docker):** connect directly to `ws://localhost:4000/ws`
    via runtime config (`/api/config`); `NEXT_PUBLIC_WS_URL` remains unset.
- The implementation plan includes a validation test (C3) that must pass
  `/ws` upgrade under `next build && next start`. If it fails, the proxy
  layer must be added before Group C can merge.

**Runtime config endpoint** (`/api/config`):

- `GET /api/config` (served by Next.js; in Compose nginx uses an exact
  `location = /api/config` route to keep it on the frontend).
- Returns `{ apiUrl: "<from env API_URL>", wsUrl: "<from env WS_URL>" }`.
- Used for runtime URL resolution in local/dev environments where the frontend
  connects directly to the Gateway (no same-origin proxy).
- `API_URL` and `WS_URL` are server-side env vars (not `NEXT_PUBLIC_`).
- **Not a cross-origin cookie auth mechanism.** Group C does not support
  browser cross-origin cookie auth. Any production deployment requiring
  the frontend to authenticate against a different origin than the one
  serving the SPA must implement SameSite=None; Secure + full CORS
  credentials in a separate ADR amendment with dedicated test coverage.

### 8.3 WebSocket URL

In same-origin mode: `ws(s)://${window.location.host}/ws` — relies on
infrastructure proxy forwarding `/ws` to the Gateway (see §8.2).

In local/dev direct mode (no same-origin proxy): value from runtime config
or environment variable (e.g. `ws://localhost:4000/ws`). This mode does **not**
use cross-origin cookie auth; it is a direct Gateway connection for development
only.

---

## 9. Environment Configuration

### 9.1 Gateway environment

| Variable | Local Dev | Compose | Helm/Prod |
|---|---|---|---|
| `NODE_ENV` | `development` | `production` | `production` |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | `http://localhost:3000,http://localhost:3001` | `https://crm.example.com` |
| `COOKIE_SECURE` | `false` | `false` (Compose uses HTTP; see §9.3) | `true` |
| `COOKIE_SAME_SITE` | `Lax` | `Strict` | `Strict` |
| `CSRF_ENABLED` | `true` | `true` | `true` |
| `WS_TICKET_TTL` | `10` | `10` | `10` |

`COOKIE_SECURE` and `COOKIE_SAME_SITE` are derived from explicit environment
configuration, not solely from `NODE_ENV`. See §9.3 for the derivation rules.

### 9.3 Cookie attribute derivation rules

**COOKIE_SECURE** is **not** derived from `NODE_ENV` alone. The derivation:

1. If `COOKIE_SECURE` env var is explicitly set (`true`/`false`), use that value.
2. Otherwise, `NODE_ENV === 'production'` → `true`, else `false`.

**Compose uses HTTP** between the browser and the Next.js frontend (no TLS
terminates inside Compose). If `COOKIE_SECURE=true` were set, the browser
would silently refuse to store or send the cookie, breaking login/refresh
entirely. Compose therefore sets `COOKIE_SECURE=false` explicitly (or relies
on the default when `NODE_ENV` is not `production`).

**Production/Helm** terminates TLS at the Ingress/load balancer. The browser
sees HTTPS, so `COOKIE_SECURE=true` is required.

**COOKIE_SAME_SITE** derivation:

1. If `COOKIE_SAME_SITE=lax` → `lax`.
2. If `COOKIE_SAME_SITE=strict` → `strict`.
3. If unset and `NODE_ENV=production` → `strict`.
4. If unset and non-production → `lax`.

This matches the table in §9.1: Local dev → `lax` (NODE_ENV=development),
Compose → `strict` (NODE_ENV=production), Helm/Prod → `strict`.

**Test matrix implication:** The C1 test suite must include:
- `COOKIE_SECURE=true` → `cookie.secure === true`.
- `COOKIE_SECURE=false` → `cookie.secure === false`.
- Default with `NODE_ENV=production` and no explicit override → `secure: true`.
- Default with `NODE_ENV=development` and no explicit override → `secure: false`.
- `COOKIE_SAME_SITE=lax` → `sameSite: 'lax'`.
- `COOKIE_SAME_SITE=strict` → `sameSite: 'strict'`.
- Default with `NODE_ENV=production` and no explicit override → `sameSite: 'strict'`.
- Default with `NODE_ENV=development` and no explicit override → `sameSite: 'lax'`.

### 9.2 Frontend environment

| Variable | Local Dev | Compose | Helm/Prod |
|---|---|---|---|
| `NEXT_PUBLIC_API_URL` | *(unset — uses proxy)* | *(unset — uses proxy)* | *(unset — uses proxy)* |
| `NEXT_PUBLIC_WS_URL` | *(unset — uses proxy)* | *(unset — uses proxy)* | *(unset — uses proxy)* |
| `API_URL` | `""` | `""` | `""` |
| `WS_URL` | `""` | `""` | `""` |

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

1. Read legacy `accessToken`, `refreshToken`, `authUser` from localStorage
   into memory variables **before any cleanup**.
2. Try cookie-based `POST /api/v1/auth/refresh` (for users who already
   migrated on a previous visit). If 200, store new access token in memory;
   skip migration.
3. If no cookie session and legacy `refreshToken` exists, call
   `POST /api/v1/auth/migrate-cookie` with the legacy token in the request
   body.
4. Gateway validates the token (same as current refresh logic), issues new
   token pair with HttpOnly cookie.
5. Frontend stores the new access token in memory.
6. **In `finally`**: clear all localStorage auth keys regardless of outcome.
   This ensures stale tokens never linger even if migration fails.

If migration fails (token expired/revoked), localStorage is still cleared
and the user is redirected to login. This is safe: the token was already
invalid.

### 12.2 Migration endpoint

`POST /api/v1/auth/migrate-cookie`

- Body: `{ refreshToken: "<jwt>" }`.
- Validates token exactly as the current (Group B) refresh endpoint.
- Returns: Set-Cookie (refresh_token, csrf_token) + JSON `{ accessToken }`.
- No CSRF required (one-time migration; no cookie exists yet).
- Origin validation still applies.
- **Temporary**: remove after one full refresh-token TTL cycle (7 days post-deploy).

### 12.3 localStorage cleanup

On every page load, frontend runs the migration boot sequence (see §3.5).
The final `finally` block clears localStorage:

```javascript
try {
  // ... migration logic ...
} finally {
  for (const key of ['accessToken', 'refreshToken', 'authUser']) {
    localStorage.removeItem(key);
  }
}
```

This runs **after** migration is attempted, never before. If the key does not
exist, `removeItem` is a no-op. The `finally` ensures cleanup even if
migration throws.

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
   CSRF cookie uses `Path=/` so it is readable from any page.
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
| 11 | Cookie Secure in production | Gateway config test | `COOKIE_SECURE=true` → `cookie.secure === true`; `COOKIE_SECURE=false` → `false` |
| 12 | API URL no build-time inline | Frontend build | `grep -r "localhost:4000" frontend/.next/` → no matches (except source maps) |
| 13 | WS ticket single-use | Redis integration | `GETDEL` on consumed ticket → null |
| 14 | WS ticket short TTL | Redis integration | Ticket key TTL ≤ 10 |
| 15 | WS ticket cross-tenant rejected | Gateway WS test | Ticket with tenantA metadata; subscribe to tenantB → 4403 |
| 16 | WS ticket cross-instance | Redis integration | Ticket stored by service A; consumed by service B |
| 17 | Gateway lint/tsc/Jest green | CI | 0 errors, 0 failures |
| 18 | Frontend lint/tsc/build green | CI | 0 errors, 0 failures |
| 19 | Compose smoke | CI | `docker compose config --quiet` passes |
| 20 | Legacy localStorage migration | Gateway test | Body refresh token → cookie issued → localStorage cleared; read-before-clear ordering verified |
| 21 | Self-review | `docs/self-review-group-c.md` | All sections complete |
| 22 | csrf_token Path is `/` | Gateway Jest | `Set-Cookie: csrf_token=...; Path=/` in login/refresh response |
| 23 | refresh_token Path is `/api/v1/auth` | Gateway Jest | `Set-Cookie: refresh_token=...; Path=/api/v1/auth` in login response |
| 24 | WS upgrade via infra proxy | CI (next build + next start) | `GET /ws?ticket=<valid>` returns 101 Switching Protocols; if fails, add nginx sidecar before merge |
| 25 | Register returns 201 | Gateway Jest | `POST /register` with valid payload → 201; body contains `accessToken` and `user` |

---

## 15. Approval Gates

Implementation may begin only after reviewers accept:

- HttpOnly refresh cookie with SameSite/Secure derivation from explicit env
  config (not solely `NODE_ENV`); Compose uses `COOKIE_SECURE=false` since
  it runs over HTTP;
- CSRF `Path=/` (site-wide) so frontend can read from any page;
- CSRF double-submit on refresh endpoint (stateless, no server-side store);
- Origin validation on auth POST endpoints;
- WS ticket single-use, short TTL, cross-instance via Redis;
- WS upgrade proxied at infrastructure layer, not assumed to work via
  Next.js rewrite; requires passing `next build && next start` test;
- migration endpoint with defined removal timeline;
- migration boot sequence: read legacy → try cookie refresh → migrate →
  clear in `finally` (never clear before migration);
- register returns 201 (distinct from login 200);
- cross-origin mode explicitly deferred (not in Group C scope);
- 10-second ticket TTL and 10/minute rate limit;
- same-origin proxy as Group C default;
- Group B revocation semantics unchanged.

## 16. Implementation Status

### 16.1 C1 — CSRF, Origin and Cookie Infrastructure ✅

| Deliverable | File | Status |
|---|---|---|
| `getCookieOptions()` with explicit env > NODE_ENV derivation | `gateway/src/config/cookies.ts` | ✅ |
| `generateCsrfToken()` / `validateCsrf()` double-submit | `gateway/src/config/csrf.ts` | ✅ |
| `createOriginValidation()` fail-closed middleware | `gateway/src/middleware/origin.ts` | ✅ |
| Unit tests (28): cookie (12), CSRF (8), origin (5), constants (3) | `gateway/src/tests/csrf_origin.test.ts` | ✅ |
| `cookie-parser` middleware | `gateway/src/index.ts` | ✅ |

### 16.2 C2 — Auth Endpoint Cookie Integration ✅

**Implemented contracts:**

| Endpoint | Method | Cookies Set | Body | Status |
|---|---|---|---|---|
| Login | POST | `refresh_token` (HttpOnly, Path=/api/v1/auth) + `csrf_token` (Path=/) | `{ accessToken, user }` | 200 |
| Register | POST | same | `{ accessToken, user }` | 201 |
| Refresh | POST | Rotated on success | `{ accessToken }`; reads token from cookie only | 200 / 403 / 401 |
| Logout | POST | Cleared on success; NOT on Redis fail | `{ message }` | 200 / 503 |
| Migrate-Cookie | POST | Issued for first time | `{ accessToken }`; no CSRF required | 200 (temporary) |
| WS-Ticket | POST | — | `{ ticket }`; Origin + revocation + rate limit | 200 / 401 / 403 / 429 / 503 |

**Key invariants enforced:**
- Refresh token only in HttpOnly cookie; never in JSON response body
- Refresh always reads from cookie, ignores body `refreshToken`
- CSRF double-submit validated on every refresh request
- Origin validation on all auth POST endpoints
- Logout clears cookies only after durable Redis revocation (fail-closed)
- WS ticket: single-use GETDEL, 10s TTL, real roles from JWT, per-user rate limit via `consumeWsTicketRateLimit`
- `issueWsTicket` checks SET NX result
- `verifyAccessToken` replaced with full `jwt.verify` + `validateDecodedToken` + `checkRevoked` in ws-ticket
- Rate limit encapsulated in `TokenRevocationService` method
- `redis` getter removed (no direct Redis access from routes)

**Test evidence:**

| Test file | Passed | Mode |
|---|---|---|
| `csrf_origin.test.ts` | 28 | Unit (no deps) |
| `auth_cookie_endpoint.test.ts` | 23 | HTTP endpoint (mocked Prisma/Redis/bcrypt) |
| `auth_cookie_integration.test.ts` | 11 + 10 skipped | No-Redis integration; Redis-dependent gated behind `CRM_REDIS_AVAILABLE=1` |

**CI evidence (local, per main merge):**
- Gateway lint: 0 errors, 0 warnings
- Gateway TypeScript: clean
- Gateway full test suite: 139 passed, 61 skipped, 0 failed (200 total)
- 7 DB-dependent suites skipped, 10 passed

### 16.3 C3 — Frontend Runtime Auth Migration ✅

**C3 complete** (commits 009be32..e958e02, hardening/http-cookie-csrf-runtime):
- Frontend memory-only accessToken (never in localStorage)
- CSRF double-submit: `X-CSRF-Token` header from `csrf_token` cookie (POST/PUT/PATCH/DELETE only)
- Cookie-based refresh via `POST /api/v1/auth/refresh` (credentials: 'include' + CSRF header)
- Legacy localStorage → cookie migration (`POST /api/v1/auth/migrate-cookie`, one-shot at boot)
- Safe logout: local session preserved on 503/network error
- WS ticket exchange: `POST /api/v1/auth/ws-ticket` → single-use UUID → `ws://host/ws?ticket=<uuid>`
- Bounded WS reconnect: 401/403 stop, 429/503/0 max 5 retries; 4401 allows one ticket-race retry then stops
- Runtime `/api/config` endpoint (server-side env, NOT NEXT_PUBLIC_*)
- Same-origin relative API paths (no absolute URL in browser bundle)
- `GATEWAY_INTERNAL_URL` for Next.js rewrites (server-side build-time var)
- Build verified: 0 `NEXT_PUBLIC_*` in client bundle

### 16.4 C4 — WebSocket Same-Origin Proxy ✅

**Merged:** `main@1f4287c` (PR #9, squash merge of `codex/group-c-c4-ws-proxy`, 6 commits)
**Tag:** `hardening-group-c-c4-stabilized`
**CI:** GitHub Actions all-green (lint, build, test, ws-proxy-smoke)

**Implemented in Gateway (merged to main@d69644b):**
- WebSocket upgrade accepts `?ticket=<uuid>` and consumes `ws:ticket:{uuid}` via `GETDEL`.
- Ticket payload carries `jti`, `exp`, tenant, user, session, user-version, and roles.
- Consumed/expired/missing tickets close the socket with `4401`.
- Redis failure during upgrade closes with `1013` (fail-closed).
- JTI/SID indexes populated from ticket metadata for Group B revocation.

**Implemented in Infra (this PR):**
- **Docker Compose**: `frontend-proxy` nginx edge container (port 3000:80) routes `/api` and `/ws` to Gateway, `/` to Frontend. Compose route semantics now match K8s Ingress.
- **Helm**: Removed all `NEXT_PUBLIC_*` env vars from frontend template. Replaced with `GATEWAY_INTERNAL_URL`, `API_URL=""`, `WS_URL=""`. Ingress `/ws` → Gateway unchanged. `proxy-read-timeout` and `proxy-send-timeout` increased to 3600s for WS long-lived connections.
- **WS Proxy Smoke Test**: `scripts/ws-proxy-test.js` validates register→login→ws-ticket→connect→connected→reuse 4401→invalid 4401 end-to-end through the edge proxy.
- **CI**: `ws-proxy-smoke` job in `.github/workflows/ci-cd.yml` runs the full topology and smoke test.

**Route semantics (Compose & Helm — semantically identical):**

| Path | Compose | Helm/Ingress |
|------|---------|-------------|
| `/` | nginx → frontend:3000 | Ingress → frontend:3000 |
| `/api/` | nginx → gateway:4000 | Ingress → gateway:4000 |
| `/ws` | nginx → gateway:4000 (Upgrade) | Ingress → gateway:4000 (Upgrade) |

**Test evidence:**

| Test file | Passed | Scope |
|---|---|---:|---|
| `ws_revocation_integration.test.ts` | 10 + 3 skipped | Ticket upgrade, multi-socket JTI close, 4401, 1013, Group B heartbeat |
| `auth_cookie_endpoint.test.ts` | 23 | `/ws-ticket` endpoint contract, origin, revocation, rate-limit |
| `auth_cookie_integration.test.ts` | 11 + 10 skipped | Ticket issue/consume contract |
| `scripts/ws-proxy-test.js` | manual/CI | E2E: valid ticket→connected, consumed→4401, invalid→4401 |

**Security invariants verified:**
- No `NEXT_PUBLIC_*` in frontend `.next/static` bundle ✅
- No `gateway:4000` in browser bundle ✅
- No `?token=` WebSocket URL pattern in frontend source ✅
- WS ticket single-use (`GETDEL`) ✅
- Invalid/consumed ticket → 4401 ✅
- Redis failure → 1013 (no auth downgrade) ✅

**C4 exit criteria (all satisfied, CI-verified):**
1. Gateway ticket handler merged ✅ (main@d69644b)
2. Compose same-origin `/ws` via nginx Upgrade ✅ (CI ws-proxy-smoke passed)
3. Consumed/invalid ticket → 4401 through proxy ✅ (CI ws-proxy-smoke passed)
4. Helm Ingress `/ws` → Gateway, no `NEXT_PUBLIC_*` in frontend template ✅ (static verification + CI)
5. CI reproducible evidence ✅ (ws-proxy-smoke job in ci-cd.yml, passed on main)
6. Identical route topology across Compose and Helm ✅ (exact /api/config, prefix /api, /ws Upgrade)

**Tech debt (carried forward to C5):**
- TD-C3-1: `/refresh` returns no user profile — need `GET /api/v1/auth/me`
- TD-C3-2: Runtime Gateway switching needs custom Next.js server proxy
- ~~TD-C3-3: Same-origin `/ws` upgrade proxy validation~~ — resolved by C4
- TD-C3-4: Frontend test framework gap (no jest/vitest config)
- ~~TD-C4-1: Docker Desktop unavailable on dev machine~~ — resolved by CI ws-proxy-smoke passing on main
- TD-C4-2: Helm rendered templates not tested on a real K8s cluster (static verification only)

C5 (runtime config finalization + self-review) is pending.
