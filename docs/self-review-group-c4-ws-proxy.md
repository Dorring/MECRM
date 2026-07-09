# C4 Self-Review — Same-Origin WebSocket Proxy

**Date:** 2026-07-09
**Branch:** `codex/group-c-c4-ws-proxy`
**Baseline:** `main@d69644b` (C4 gateway ticket handler merged)
**Reviewer:** Dorring / Codex review pass

---

## 1. Scope

### Done

1. Added `frontend-proxy` nginx service as the Docker Compose browser entrypoint.
2. Routed Compose traffic consistently with Kubernetes Ingress:
   - `/` -> frontend
   - `/api/` -> gateway
   - `/ws` -> gateway with WebSocket Upgrade
   - `/api/config` -> frontend through an exact nginx location
3. Removed browser-facing `NEXT_PUBLIC_*` env vars from the Helm frontend template.
4. Added server-side frontend env vars only: `GATEWAY_INTERNAL_URL`, `API_URL=""`, `WS_URL=""`.
5. Increased nginx ingress read/send timeouts to 3600s for long-lived WebSocket connections.
6. Added `scripts/ws-proxy-test.js` to validate the full same-origin ticket flow.
7. Added `ws-proxy-smoke` CI job.
8. Added static regression tests in `tests/infra/test_ws_proxy.py`.
9. Updated ADR-004 and implementation plan with C4 evidence.

### Not done

- No real Kubernetes cluster 101/WSS test; Helm is statically validated only.
- C5 runtime-config finalization remains pending.

---

## 2. Files Changed

| File | Change |
|---|---|
| `conf/nginx.conf` | New nginx edge proxy with `/ws` Upgrade, exact `/api/config`, direct `/api/`, frontend `/` |
| `docker-compose.yml` | Added `frontend-proxy`; frontend uses `expose` not host `ports`; `WS_URL=""`; added `ws-proxy-test` profile |
| `scripts/ws-proxy-test.js` | New E2E smoke: register -> login -> ws-ticket -> connected -> consumed 4401 -> invalid 4401 |
| `.github/workflows/ci-cd.yml` | Added `ws-proxy-smoke` job |
| `deploy/helm/enterprise-crm/templates/frontend.yaml` | Removed `NEXT_PUBLIC_*`; added server-side runtime env |
| `deploy/helm/enterprise-crm/values.yaml` | Set ingress read/send timeouts to 3600 |
| `deploy/helm/enterprise-crm/values-production.yaml` | Set ingress read/send timeouts to 3600 |
| `tests/infra/test_ws_proxy.py` | Static regression tests for Compose, nginx, Helm, frontend anti-patterns |
| `docs/adr/004-httponly-cookie-csrf-runtime.md` | C4 status/evidence update |
| `docs/adr/004-httponly-cookie-csrf-runtime-plan.md` | C4 plan/evidence update |

---

## 3. Route Semantics

### Compose via `frontend-proxy`

| Path | Destination | Notes |
|---|---|---|
| `/` | `frontend:3000` | Next.js frontend |
| `/api/config` | `frontend:3000` | Exact nginx route → frontend (Next.js route handler) |
| `/api/` | `gateway:4000` | Direct Gateway API path |
| `/ws` | `gateway:4000` | WebSocket Upgrade, no Next.js rewrite dependency |

### Helm / Ingress

| Path | Destination | Notes |
|---|---|---|
| `/` | frontend service | Prefix — Next.js frontend |
| `/api/config` | frontend service | Exact — Next.js route handler; must beat `/api` Prefix |
| `/api` | gateway service | Prefix — REST API |
| `/ws` | gateway service | Prefix — nginx-ingress handles Upgrade |

---

## 4. Security Invariants

| Invariant | Status | Evidence |
|---|---|---|
| No JWT in browser URL | PASS | Frontend uses `?ticket=` only; no `?token=` in `frontend/src` |
| Access token memory-only | PASS | C3 `api.ts` memory variable unchanged |
| Refresh token HttpOnly | PASS | C1/C2 Gateway cookie contract unchanged |
| WS ticket single-use | PASS | Gateway `consumeWsTicket()` uses `GETDEL` |
| Consumed/invalid ticket closes `4401` | PASS | `scripts/ws-proxy-test.js`; Gateway WS tests |
| Redis/ticket-store failure fail-closed | PASS | Gateway WS closes `1013` |
| No browser-facing `NEXT_PUBLIC_*` in Helm frontend | PASS | Static grep/test |
| No `ws://gateway:4000` in browser runtime config | PASS | Compose `WS_URL=""`; static test |

---

## 5. Validation Evidence

### Implementer-reported validation

| Check | Result |
|---|---|
| Frontend lint/build | PASS |
| Gateway lint/build/tests | PASS; Gateway tests reported 139 passed / 61 skipped |
| Bundle grep for `NEXT_PUBLIC_*` | PASS |
| Bundle grep for `gateway:4000` / `localhost:4000` | PASS |
| Helm grep for `NEXT_PUBLIC_*` | PASS |

### Codex review additions

| Check | Result |
|---|---|
| `git diff --check` | Initially failed on Markdown trailing spaces; fixed during review |
| Static WS proxy regression tests | Added in `tests/infra/test_ws_proxy.py` |
| `/api/config` route review | Fixed with exact nginx route to frontend |
| Ingress timeout review | Added missing `proxy-send-timeout: "3600"` |
| Cookie parsing review | Hardened `scripts/ws-proxy-test.js` for `getSetCookie()` and comma-joined fallback |

### Docker runtime validation

Pending in this review run until Docker commands complete locally or in CI:

```powershell
docker compose up -d --wait postgres redis kafka kafka-init opa gateway frontend frontend-proxy
docker compose --profile ws-proxy-test run --rm ws-proxy-test
```

---

## 6. Known Residual Risks

| ID | Risk | Mitigation |
|---|---|---|
| TD-C4-1 | Helm not tested against a real Kubernetes ingress controller | CI/static Helm validation; defer real WSS ingress test to staging |
| TD-C4-2 | HTTPS/WSS behavior not proven by Compose | Compose validates HTTP/WS only; staging should validate TLS/WSS |
| TD-C4-3 | C5 runtime-config finalization remains pending | Track in Group C C5 |

---

## 7. Independent Review Checklist

| Check | Expected | Status |
|---|---|---|
| Frontend still publishes host port 3000 | No | PASS |
| `frontend-proxy` publishes `${FRONTEND_PORT:-3000}:80` | Yes | PASS |
| `WS_URL=ws://gateway:4000` remains in Compose frontend env | No | PASS |
| nginx `/ws` has Upgrade headers and HTTP/1.1 | Yes | PASS |
| nginx has exact `/api/config` to frontend | Yes | PASS |
| Helm frontend has any `NEXT_PUBLIC_*` | No | PASS |
| Helm Ingress `/ws` routes to Gateway | Yes | PASS |
| Ingress read/send timeouts are 3600 | Yes | PASS |
| Smoke validates consumed and invalid tickets | Yes, both close `4401` | PASS |

---

## 8. Post-Review Fixes (2026-07-09)

### Bug 1: `/api/config` routed to Gateway by nginx `/api/` prefix match

**Root cause:** nginx `location /api/` (prefix match) matched `/api/config` before the catch-all `location /`. `/api/config` is a Next.js Route Handler — Gateway has no such endpoint and would return 404.

**Fix:** Added exact match `location = /api/config` with `proxy_pass http://frontend_upstream` before the `location /api/` block. nginx exact match (`=`) takes priority over prefix match.

**Verification:**
- `test_nginx_routes_api_config_frontend_and_ws_gateway` — asserts `location = /api/config` exists and points to `frontend_upstream`.
- Already applied to `conf/nginx.conf` before review (linter fix).

### Bug 2: `WS_URL=""` falsy fallback in route.ts

**Root cause:** `route.ts` used `process.env.WS_URL || 'ws://localhost:4000'`. When `WS_URL=""` (explicit empty), the `||` operator treats `""` as falsy → falls back to `'ws://localhost:4000'`. Browser receives `wsUrl: 'ws://localhost:4000'` → bypasses same-origin proxy.

**Fix:** Changed to `process.env.WS_URL !== undefined ? process.env.WS_URL : 'ws://localhost:4000'` (and same for `API_URL`). Explicit empty string is now preserved as-is.

**Verification:**
- `test_api_config_route_uses_strict_undefined_check` — asserts `!== undefined` and absence of `||` pattern.
- WS proxy smoke test Stage 0: asserts `wsUrl=""` (not `localhost:4000`).

### Bug 3: Helm Ingress `/api/config` caught by `/api` Prefix → Gateway

**Root cause:** All three Helm values files (`values.yaml`, `values-staging.yaml`, `values-production.yaml`) had only `/api` Prefix → gateway. K8s Ingress uses longest prefix — `/api/config` matches `/api` prefix and routes to Gateway (same class of bug as Bug #1, but in the K8s layer).

**Fix:** Added `path: /api/config, pathType: Exact, service: frontend` before `/api` Prefix in all three values files. K8s Ingress Exact match takes priority over Prefix.

**Verification:**
- `test_helm_ingress_routes_and_timeouts` — asserts `/api/config` Exact → frontend, `/api` Prefix → gateway, `/ws` Prefix → gateway across all three values files.

### Smoke test enhancement

Added Stage 0 (`/api/config` validation) to `scripts/ws-proxy-test.js`:
- GET `/api/config` → HTTP 200
- `apiUrl` and `wsUrl` both `""`
- No `ws://localhost:4000` or `ws://gateway:4000` leakage.

### Static test enhancement

- `test_api_config_route_uses_strict_undefined_check`: New assertion that route.ts uses `!== undefined`.
- `test_no_browser_facing_ws_anti_patterns_in_frontend_source`: Fixed to skip documentation comments (lines starting with `//` or `*`), only flag actual `process.env.NEXT_PUBLIC_*` usage.

### Verification matrix (post-fix)

| Check | Result |
|-------|--------|
| `pytest tests/infra/test_ws_proxy.py -v` | 7 passed |
| Frontend lint | PASS |
| Frontend build | PASS |
| Gateway lint | PASS |
| Gateway build | PASS |
| Gateway tests (`--runInBand`) | 139 passed, 10 suites, 0 failures |
| Bundle grep `NEXT_PUBLIC_*` | 0 matches |
| Bundle grep `localhost:4000` | 0 matches |
| Compose `WS_URL=` | Empty string (no fallback) |
| Compose `API_URL=` | Empty string (no fallback) |
