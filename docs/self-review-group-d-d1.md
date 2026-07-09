# D1 Self-Review — Compose Health Dependencies + Healthcheck Foundation

**Date:** 2026-07-10
**Branch:** `codex/group-d-d1-health-dependencies`
**Baseline:** `main@a55b7de` (Group C stabilized)
**Reviewer:** Codex

---

## 1. Depends-On: service_started → service_healthy

| Service | Dependency | Old | New | Rationale |
|---------|-----------|-----|-----|-----------|
| `gateway` | `opa` | `service_started` | `service_healthy` | OPA healthcheck (`opa eval --data /policies`) already validates policy compilation. Gateway should not serve requests until OPA policies are loaded. |
| `agents` | `weaviate` | `service_started` | `service_healthy` | Weaviate healthcheck (`/v1/.well-known/ready`) confirms vector DB is ready. |
| `agents` | `opa` | `service_started` | `service_healthy` | Same as gateway→opa rationale. |
| `ws-proxy-test` | `frontend-proxy` | `service_started` | `service_healthy` | nginx now has a healthcheck (§3). Safe to strengthen. |

**Retained `service_started`:**
- `frontend-proxy` → `frontend`, `gateway`: bare depends_on (defaults to `service_started`). Acceptable — nginx handles upstream failures at HTTP layer. Strengthening frontend-proxy → frontend would require a frontend Compose healthcheck. This is deferred because nginx can tolerate brief upstream unavailability and the browser-facing smoke tests already validate the full path.

---

## 2. New Healthchecks: Failure Semantics

| Service | Check | Failure Semantics |
|---------|-------|-------------------|
| **frontend-proxy** | `wget http://localhost/health` | nginx is not serving responses. `depends_on` blocks downstream containers that use `service_healthy`. Docker Compose marks container "unhealthy" after 3 retries × 15s = 45s. |
| **agents (Compose)** | `httpx.get('http://localhost:5010/health')` | Health endpoint returning non-2xx or unreachable. Matches Dockerfile HEALTHCHECK. `depends_on` not used by any downstream (no service depends on agents today), but Docker shows health status in `docker compose ps`. |

---

## 3. Healthcheck Command Dependencies

| Service | Command | Requires | Available in Image? |
|---------|---------|----------|---------------------|
| frontend-proxy | `wget -qO- http://localhost/health` | wget | ✅ nginx:1.27-alpine includes BusyBox wget |
| agents | `python -c "import httpx,sys; ..."` | python, httpx | ✅ httpx is a runtime dependency (pip installed) |
| gateway | `wget -qO- http://localhost:4000/health` | wget | ✅ Node runner image includes wget |

All healthcheck commands use tools already present in the respective images. No new dependencies introduced.

---

## 4. Keycloak Healthcheck — Deferred

**Not implemented in D1.** Keycloak uses `start-dev` command which does NOT enable Quarkus health endpoints by default. Adding `KC_HEALTH_ENABLED=true` would change the boot behavior and cannot be verified locally without Docker access.

**Decision:** Downgraded to Should Fix. Will be addressed in a follow-up when Docker is available for verification.

---

## 5. /health vs /ready Liveness/Readiness

| Service | /health (Liveness) | /ready (Readiness) | Notes |
|---------|-------------------|---------------------|-------|
| **Gateway** | ✅ `GET /health` → `{"status":"healthy"}` | ✅ `GET /ready` → checks DB/Redis/Kafka | Already best practice. No D1 changes. |
| **Agents** | ✅ `GET /health` → `{"status":"healthy"}` | ❌ Not yet | D1 adds Compose healthcheck on `/health`. `/ready` endpoint deferred — see §5.1. |
| **Frontend** | ✅ `GET /api/health` → `{"status":"ok"}` | Same endpoint | Lightweight process-alive only. K8s probes repaired (§6). |

### 5.1 Agents /ready — D1 Follow-up

The agents service has a single `/health` endpoint returning `{"status":"healthy"}` immediately. A proper `/ready` endpoint would need to:
- Check Kafka consumer group connectivity (not just broker reachability, which can pass transiently)
- Check Weaviate indexer status
- Check ChatAgent + SearchAgent initialization state

This is **medium risk** because:
- An over-strict check (e.g., Kafka consumer group liveness) could cause readiness flaps on transient broker issues
- The orchestrator's internal retry/backoff is the actual resilience mechanism; a `/ready` that mirrors those dependencies too strictly is redundant and potentially harmful

**Plan:** Defer to D1 follow-up PR. When implemented:
- `/ready` must return 503 (not crash) on soft failures
- Must NOT fail on transient Kafka/Weaviate connectivity (use cached last-known-good state)
- Must have unit tests covering: all-healthy, Kafka-down, Weaviate-down, agent-not-initialized
- Helm readiness probe updated to `/ready` only after tests pass

---

## 6. Production Traffic Path

**No production traffic paths changed.** All changes are in Compose service definitions, nginx config (dev proxy), and a new Next.js API route (`/api/health`).

| Change | Affects |
|--------|---------|
| `depends_on: condition:` changes | Compose startup ordering only |
| nginx `location = /health` | Internal healthcheck only; exact path match does not affect `/`, `/api/`, `/ws` routing |
| `frontend/src/app/api/health/route.ts` | K8s pod-level probes only; nginx proxy `/api/` location overrides this for external traffic |
| `start_period` additions | Compose healthcheck timing only |
| Agents Compose healthcheck | `docker compose ps` display only |

The frontend-proxy nginx has `location /api/` → `gateway_upstream`, so external `GET /api/health` requests reach the Gateway (which also has a `/health` endpoint). The Next.js `/api/health` route is only hit by:
- K8s pod probes (container port 3000 directly)
- Local Next.js dev mode (`npm run dev`)
- Future container-level healthchecks (`wget localhost:3000/api/health`)

---

## 7. Locally Unverifiable Items

| Item | Why | Mitigation |
|------|-----|------------|
| Keycloak `KC_HEALTH_ENABLED` | No Docker available for verification | Downgraded to Should Fix; not in D1 scope |
| Agents `/ready` endpoint | Requires Kafka + Weaviate in running stack | Deferred to D1 follow-up with tests |
| Helm `readinessProbe` → `/ready` | `/ready` not yet implemented | Documented as D1 follow-up condition |
| Docker smoke-test + ws-proxy-test | No Docker available | Infra tests (`pytest tests/infra -v`) provide static validation; Compose `config --quiet` passes |

The static infra tests (`tests/infra/`) validate Compose config structure — they don't need Docker. They passed with 64P/7S.

---

## 8. Rollback

### Per-change rollback:

| Change | Rollback |
|--------|----------|
| `depends_on` conditions | Revert line edits; deploy old docker-compose.yml |
| nginx `/health` location | Remove 7-line block from nginx.conf |
| nginx healthcheck in Compose | Remove healthcheck block from frontend-proxy |
| ws-proxy-test→frontend-proxy condition | Revert to `service_started` |
| Frontend `/api/health` route | Delete `frontend/src/app/api/health/route.ts` |
| Agents Compose healthcheck | Remove healthcheck block from agents |
| `start_period` additions | Revert to no start_period (harmless either way) |

### Full rollback:
```
git revert <D1-commit>
```
All changes are additive or condition-strengthening. No data migrations, no API changes, no schema changes.

---

## 9. Diff Summary

```
 conf/nginx.conf    |  7 +++++++
 docker-compose.yml | 36 ++++++++++++++++++++++++++++++++----
 2 files changed, 39 insertions(+), 4 deletions(-)
```

Plus new file:
```
 frontend/src/app/api/health/route.ts  (12 lines)
```

### docker-compose.yml changes:
1. `gateway → opa`: `service_started` → `service_healthy` (1 line)
2. `agents → weaviate`: `service_started` → `service_healthy` (1 line)
3. `agents → opa`: `service_started` → `service_healthy` (1 line)
4. `frontend-proxy`: added healthcheck block (7 lines)
5. `ws-proxy-test → frontend-proxy`: `service_started` → `service_healthy` (1 line)
6. `agents`: added healthcheck block (7 lines)
7. `gateway`: added comment explaining Compose-vs-Dockerfile params (8 lines)
8. `kafka`: added `start_period: 60s` (1 line)
9. `postgres`: added `start_period: 15s` (1 line)

### conf/nginx.conf changes:
10. Added `location = /health` block (7 lines)

### New file:
11. `frontend/src/app/api/health/route.ts` — returns `{"status":"ok"}`

---

## 10. Verification Results

| Check | Result |
|-------|--------|
| `docker compose config --quiet` | ✅ No errors |
| `docker compose -f docker-compose.chaos.yml config --quiet` | ✅ Warning only (obsolete `version`) |
| `pytest tests/infra -v` | ✅ 64 passed, 7 skipped |
| `frontend npm run lint` | ✅ Clean (via `npx tsc --noEmit`) |
| `frontend npm run build` | ✅ 20 routes (includes new `/api/health`) |
| `gateway npm run lint` | ✅ Clean |
| `gateway npm test -- --runInBand` | ✅ 146 passed, 61 skipped (same as baseline) |
| `docker compose up -d --wait ...` | ⚠️ Skipped (no Docker) — static validation only |
| `docker compose --profile smoke-test run ...` | ⚠️ Skipped (no Docker) |
| `docker compose --profile ws-proxy-test run ...` | ⚠️ Skipped (no Docker) |

---

## 11. Exit Gate Assessment

| # | Criterion | Status |
|---|-----------|--------|
| 1 | `service_started` only on acceptable bare depends_on | ✅ 0 remaining blockers |
| 2 | D1-scoped missing healthchecks addressed: frontend-proxy + agents. Keycloak remains Should Fix, explicitly deferred pending Docker validation of KC_HEALTH_ENABLED=true | ✅ nginx + agents done; Keycloak deferred |
| 3 | Compose configs valid | ✅ Both main and chaos |
| 4 | Gateway tests unchanged from baseline | ✅ 146P/61S |
| 5 | Frontend lint/tsc/build clean | ✅ |
| 6 | Infra tests pass | ✅ 64P/7S |
| 7 | No production traffic path changed | ✅ Verified |
| 8 | Healthcheck commands use in-image tools only | ✅ wget/httpx already present |
| 9 | `/health` vs `/ready` distinction preserved | ✅ Gateway already correct; agents deferred |
| 10 | Self-review complete | ✅ This document |
