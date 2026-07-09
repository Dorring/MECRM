# Group D Preflight Investigation — Compose / Chaos / Health / Image Pinning

**Date:** 2026-07-10
**Status:** D1 in progress on `codex/group-d-d1-health-dependencies`
**Baseline:** `main@a55b7de` (Group C stabilized)
**Investigator:** Codex

---

## Executive Summary

Group D targets four hardening dimensions: Compose health dependency correctness, chaos test reliability, healthcheck design completeness, and image version pinning. The investigation found **7 Blocker** issues (must fix before Group D closure), **10 Should Fix** items (strongly recommended), and **8 Defer** items (acknowledged but out of scope).

---

## 1. Compose Health Dependencies

### 1.1 Dependency Matrix (Pre-D1 Baseline — Current Condition as of main@a55b7de)

| Service | Depends On | Current Condition | Recommended | Verdict |
|---------|-----------|-------------------|-------------|---------|
| **gateway** | postgres | `service_healthy` | — | ✅ Correct |
| | redis | `service_healthy` | — | ✅ Correct |
| | kafka-init | `service_completed_successfully` | — | ✅ Correct |
| | **opa** | **`service_started`** | `service_healthy` | ❌ **BLOCKER** |
| **agents** | postgres | `service_healthy` | — | ✅ Correct |
| | redis | `service_healthy` | — | ✅ Correct |
| | kafka-init | `service_completed_successfully` | — | ✅ Correct |
| | **weaviate** | **`service_started`** | `service_healthy` | ❌ **BLOCKER** |
| | **opa** | **`service_started`** | `service_healthy` | ❌ **BLOCKER** |
| **replay-service** | postgres | `service_healthy` | — | ✅ Correct |
| | kafka-init | `service_completed_successfully` | — | ✅ Correct |
| **smoke-test** | gateway | `service_healthy` | — | ✅ Correct |
| | postgres/redis/kafka-init | all correct | — | ✅ Correct |
| **ws-proxy-test** | frontend-proxy | `service_started` | — | ✅ Acceptable (only needs container) |
| | gateway/postgres/redis/kafka-init | all correct | — | ✅ Correct |
| **kafka-init** | kafka | `service_healthy` | — | ✅ Correct |
| **migrate** | postgres | `service_healthy` | — | ✅ Correct |

### 1.1a D1 Status Update (branch: `codex/group-d-d1-health-dependencies`)

The following blocker findings have been addressed or partially addressed:

| Finding ID | Description | Status | Notes |
|------------|-------------|--------|-------|
| **D-C-1** | OPA dep `service_started` → `service_healthy` (gateway + agents) | ✅ **Fixed** | Both sites changed |
| **D-C-2** | Weaviate dep `service_started` → `service_healthy` (agents) | ✅ **Fixed** | One line changed |
| **D-HC-1** | Frontend K8s probes → 404 `/api/health` | ✅ **Fixed** | New `frontend/src/app/api/health/route.ts` returns 200 |
| **D-HC-2** | nginx frontend-proxy no healthcheck | ✅ **Fixed** | `location = /health` in nginx.conf + Compose healthcheck |
| **D-HC-3** | Agents no Compose healthcheck | 🟡 **Partial** | Compose healthcheck added on `/health`; `/ready` endpoint deferred to follow-up |
| **D-HC-4** | Kafka no `start_period` | ✅ **Fixed** | `start_period: 60s` |
| **D-HC-5** | Postgres no `start_period` | ✅ **Fixed** | `start_period: 15s` |
| **D-HC-6** | Gateway Compose healthcheck params inconsistent | 🟡 **Analyzed** | Intentional — Compose cold-start tolerance needs more generous params than Dockerfile restart. Commented inline. |
| **D-HC-7** | Keycloak no healthcheck | ⏸️ **Deferred** | Requires `KC_HEALTH_ENABLED=true` + Docker verification; downgraded to Should Fix |

Static regression tests added in `tests/infra/test_group_d_health_dependencies.py` to lock these changes.

### 1.2 Key Findings

**Finding D-C-1 (BLOCKER): OPA dependency uses `service_started` (×2 sites)**

OPA defines a valid healthcheck that compiles all mounted Rego policies:
```yaml
test: ["CMD", "/opa", "eval", "--data", "/policies", "--fail", "--format=discard", "true"]
interval: 15s, timeout: 5s, retries: 5, start_period: 5s
```
Both `gateway` (line 70) and `agents` (line 200) depend on OPA with `condition: service_started`. This means the gateway can begin serving requests before OPA policies are loaded and validated. An OPA startup failure (e.g., bad Rego syntax) would go undetected.

**Fix:** Change both sites to `condition: service_healthy`. Two lines, low risk (Compose-only change; OPA healthcheck already exists).

**Finding D-C-2 (BLOCKER): Weaviate dependency uses `service_started`**

Weaviate defines a readiness healthcheck:
```yaml
test: ["CMD-SHELL", "wget -qO- http://localhost:8080/v1/.well-known/ready || exit 1"]
interval: 15s, timeout: 5s, retries: 10, start_period: 30s
```
`agents` (line 198) depends on Weaviate with `condition: service_started`. The agents orchestrator may have internal retry, but Compose-level blocking is the correct pattern — it avoids wasting CPU cycles on repeated Weaviate connection failures during boot.

**Fix:** Change line 198 to `condition: service_healthy`. One line, low risk (Compose-only change; Weaviate healthcheck already exists).

**Finding D-C-3 (Should Fix): Gateway does not depend on `migrate`**

The `migrate` service (profile-gated) runs Prisma + raw SQL migrations. Gateway starts independently. In the default flow (`docker compose up` without `--profile migrate`), migrations are run separately — this is by design. However, if someone runs `docker compose --profile migrate up`, gateway could start before tables exist, causing startup errors.

**Fix:** Either document that migrate must always run before main stack, or add `migrate: condition: service_completed_successfully` to gateway's depends_on. The simpler fix is documentation + a note in the compose file.

**Finding D-C-4 (Defer): `frontend-proxy` and `frontend` use bare `depends_on`**

These default to `service_started`, which is acceptable: nginx handles upstream failures at the HTTP layer (502), and Next.js can start before the gateway. Strengthening to `service_healthy` would eliminate a brief startup window where API calls fail, but the impact is negligible.

### 1.3 kafka-init and migrate Are Correct

- **kafka-init**: One-shot container (`entrypoint: ["bash", "/scripts/kafka-init.sh"]`). Exits after creating topics. All dependents correctly use `service_completed_successfully`. ✅
- **migrate**: One-shot container (`command: ["bash", "/scripts/migrate.sh"]`). Exits after Prisma + SQL + RLS enforcement. Behind `--profile migrate`. ✅

---

## 2. Chaos Tests

### 2.1 Trigger Configuration

| Trigger | Active? | Guard |
|---------|---------|-------|
| `schedule: "0 3 * * *"` (daily 3AM UTC) | Yes | `github.ref == 'refs/heads/main'` |
| `workflow_dispatch` | Yes | None |

### 2.2 chaos-migrations Exit 2 Root Cause

The `chaos-migrations` service in `docker-compose.chaos.yml` (lines 84-110) runs an **inline shell script** that only applies raw SQL files via `psql`. It does **NOT** run `npx prisma migrate deploy`. Key differences from the unified `scripts/migrate.sh`:

| Aspect | `scripts/migrate.sh` (unified) | `chaos-migrations` (inline) |
|--------|-------------------------------|---------------------------|
| Prisma migrations | ✅ `npx prisma migrate deploy` | ❌ Not run |
| Raw SQL ordering | Fixed order (00→12) | Same order but skips 02, applies last |
| Advisory lock | ✅ Session-level lock (key 405011) | ❌ No locking |
| Schema drift audit | ✅ RLS enforcement check | ❌ No audit |
| Error handling | Structured logging | `set -e` only |

**Exit code 2** likely means one of the SQL files failed because the Prisma-managed tables (`users`, `roles`, etc.) don't exist. The `02-rls-policies.sql` file contains `ALTER TABLE ... FORCE ROW LEVEL SECURITY` and `GRANT ... TO crm_app` — these reference Prisma-created tables.

### 2.3 Chaos Infrastructure Drift

`docker-compose.chaos.yml` duplicates service definitions from the main `docker-compose.yml`:

| Service | Main Compose | Chaos Compose | Drift? |
|---------|-------------|---------------|--------|
| OPA | `openpolicyagent/opa:latest` | `openpolicyagent/opa:0.55.0` | **Yes — version mismatch** |
| Prometheus | `prom/prometheus:latest` | `prom/prometheus:latest` | Same |
| Grafana | `grafana/grafana:latest` | `grafana/grafana:latest` | Same |
| Kafka | `KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"` | `"true"` | **Yes — behavior difference** |

### 2.4 Chaos Test Current State

- The last report artifact (`reports/chaos/chaos-recovery-report.json`) is dated ~2026-01-24, predating the baseline commit by 5 months.
- The `requirements.txt` in the workflow installs Python packages but the chaos tests import from the agents source tree (`from resilience.circuit_breaker import CircuitBreaker`). Any refactoring breaks these imports silently.
- The CI workflow uses Python 3.11 but compiled `.pyc` files indicate Python 3.12 was used locally.
- All metrics queries in the last report returned empty `[]` arrays — no useful signal from scheduled runs.

### 2.5 Recommendations

**Finding D-CH-1 (BLOCKER): chaos-migrations should reuse unified `migrate.sh`**

Replace the inline `psql` loop with the same `bash /scripts/migrate.sh` used by main CI and the migrate profile. This eliminates schema drift between chaos and main stacks.

**Finding D-CH-2 (Should Fix): Remove schedule trigger**

Daily 3AM runs waste CI minutes (45 min timeout, full stack startup). The tests are gated by `CHAOS_TESTS_ENABLED=true` and are designed for intentional, manual activation. The schedule provides no signal (metrics queries return empty) and adds maintenance burden.

**Recommendation:** Keep `workflow_dispatch` only. Remove `schedule` trigger.

**Finding D-CH-3 (Should Fix): Pin OPA version in chaos compose**

`docker-compose.chaos.yml` pins OPA to `0.55.0` while main compose and CI use `0.70.0`. Pin to `0.70.0` for consistency, or better yet, extract the OPA service definition into a shared compose include.

**Finding D-CH-4 (Defer): Extract shared compose services**

The chaos compose duplicates postgres, redis, kafka, opa definitions. Using Docker Compose `include` or a shared base file would eliminate drift. Defer to a broader compose refactor (Group F/G).

### 2.6 Answer: Should Chaos Continue Scheduled?

**No.** The chaos workflow should be `workflow_dispatch` only. Rationale:
1. Chaos tests are inherently destructive and slow (~15-30 min for full stack + fault injection).
2. They add no signal on schedule (metrics queries all return empty).
3. They are not a gate for any downstream CI job.
4. The tests are explicitly designed for intentional activation (`CHAOS_TESTS_ENABLED=true`).
5. CI minutes are wasted on a pre-production project.

The chaos test code itself (6 test files in `agents/tests/chaos/`) should be preserved and made functional — they test real resilience patterns (circuit breaker, kill switch, idempotent reprojection) that the main pipeline does not cover.

---

## 3. Healthcheck Design

### 3.1 Service-by-Service Audit

| Service | Compose Healthcheck? | K8s Probe? | Endpoint/Command | Gap? |
|---------|---------------------|------------|------------------|------|
| **Postgres** | Yes (`pg_isready`) | N/A (Bitnami subchart) | `pg_isready -U crm_user -d enterprise_crm` | No `start_period` |
| **Redis** | Yes (`redis-cli ping`) | N/A (Bitnami subchart) | `redis-cli ping` | No `start_period` |
| **Kafka** | Yes (`kafka-broker-api-versions`) | N/A (Bitnami subchart) | Broker API version check | No `start_period` (Kafka can take 30-60s) |
| **kafka-init** | N/A (one-shot) | N/A | Exit code | ✅ Correct |
| **migrate** | N/A (one-shot) | N/A | Exit code | ✅ Correct |
| **OPA** | ✅ Yes | ❌ No K8s template | `opa eval --data /policies` | Not consumed by dependents (see §1) |
| **Weaviate** | ✅ Yes | ❌ No K8s template | `/v1/.well-known/ready` | Not consumed by dependents (see §1) |
| **Gateway** | ✅ Yes | ✅ Both probes | `/health` (liveness), `/ready` (readiness) | Compose params inconsistent with Dockerfile |
| **Frontend (Next.js)** | ❌ **None** | ✅ Both probes → **404** | K8s probes point to `/api/health` | **Endpoint doesn't exist** |
| **Frontend-proxy (nginx)** | ❌ **None** | N/A (Ingress-level) | — | **No healthcheck** |
| **Agents** | ❌ **None** | ✅ Both probes → same `/health` | `/health` returns `{"status":"healthy"}` | No `/ready`; no dep checks |
| **Replay-service** | ✅ Yes | ❌ No K8s template | `/health` via httpx | No `/ready` |
| **Keycloak** | ❌ **None** | N/A (Bitnami subchart) | — | No healthcheck |
| **Smoke-test / ws-proxy-test** | N/A (one-shot) | N/A | Exit code | ✅ Correct |

### 3.2 Critical Gaps

**Finding D-HC-1 (BLOCKER): Frontend K8s probes target non-existent `/api/health`**

`deploy/helm/enterprise-crm/templates/frontend.yaml` defines:
```yaml
livenessProbe:
  httpGet: { path: /api/health, port: 3000 }
readinessProbe:
  httpGet: { path: /api/health, port: 3000 }
```
No such API route exists in the Next.js application. The `/api/` prefix is proxied to the gateway by nginx at the Ingress level, but inside the pod, the Next.js server handles requests directly — `/api/health` returns 404. This causes **false liveness failures** and pod restarts in K8s.

**Fix options:**
- Add `src/app/api/health/route.ts` returning `Response.json({ status: 'ok' })`
- Or change probe path to `/api/config` (already works, returns runtime config)

**Finding D-HC-2 (BLOCKER): nginx frontend-proxy has no healthcheck (Compose)**

As the single browser entry point, nginx should have a basic healthcheck. Without one, `depends_on: service_started` is the best available condition, and Docker cannot detect nginx failures.

**Fix:** Add to `conf/nginx.conf`:
```nginx
location /health {
  return 200 'ok';
  add_header Content-Type text/plain;
}
```
And add to Compose:
```yaml
healthcheck:
  test: ["CMD", "wget", "-qO-", "http://localhost/health"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 5s
```

**Finding D-HC-3 (BLOCKER): Agents has no Compose healthcheck, no `/ready` endpoint**

The agents orchestrator has significant startup dependencies (Kafka consumer groups, Weaviate indexers, ChatAgent, SearchAgent). Currently:
- Compose: no `healthcheck` block at all (only Dockerfile HEALTHCHECK)
- K8s: both liveness and readiness probes point to `/health` which returns `{"status":"healthy"}` immediately, before any agent initialization completes

**Fix:**
- Add Compose healthcheck block
- Add a `/ready` endpoint that checks Kafka connectivity + Weaviate readiness + agent initialization status
- Point K8s readiness probe to `/ready`; keep liveness on `/health`

### 3.3 Other Gaps

**Finding D-HC-4 (Should Fix): Kafka has no `start_period`**

Kafka (KRaft mode) can take 30-60 seconds to complete controller election. Without `start_period`, the healthcheck starts immediately, potentially triggering false retries. Add `start_period: 60s`.

**Finding D-HC-5 (Should Fix): Postgres has no `start_period`**

Alpine Postgres boots in ~5-10s. Add `start_period: 15s` to prevent premature healthcheck failures during initial startup.

**Finding D-HC-6 (Should Fix): Gateway Compose healthcheck params inconsistent**

| Source | Interval | Timeout | Retries | Start Period |
|--------|----------|---------|---------|-------------|
| Dockerfile | 30s | 10s | 3 | 5s |
| Compose | 15s | 5s | 10 | 30s |

Compose `retries: 10` at `interval: 15s` = up to 150s before marking unhealthy — far too generous. Align with Dockerfile: `interval: 30s, timeout: 10s, retries: 3, start_period: 5s`.

**Finding D-HC-7 (Should Fix): Keycloak has no healthcheck**

Keycloak (Quarkus distribution) exposes `/health/ready` natively. Add:
```yaml
healthcheck:
  test: ["CMD", "wget", "-qO-", "http://localhost:8080/health/ready"]
  interval: 30s
  timeout: 10s
  retries: 5
  start_period: 30s
```

**Finding D-HC-8 (Defer): Observability sidecars lack healthchecks**

postgres-exporter, redis-exporter, kafka-exporter, kafka-ui, prometheus, grafana, loki all have no healthchecks. These are not in the request path — low priority.

### 3.4 Readiness vs Liveness Summary

| Service | Liveness | Readiness | Separate? | Correct? |
|---------|----------|-----------|-----------|----------|
| Gateway | `/health` | `/ready` | ✅ Yes | ✅ Best practice |
| Frontend (K8s) | `/api/health` (404) | `/api/health` (404) | ❌ Same, broken | ❌ Endpoint missing |
| Agents (K8s) | `/health` | `/health` | ❌ Same | ❌ No dep check |

---

## 4. Image Version Pinning

### 4.1 `latest` Tags — Full Inventory

#### docker-compose.yml (9 `latest` tags)

| # | Service | Image | Current | Recommended Pin |
|---|---------|-------|---------|-----------------|
| 1 | postgres-exporter | `prometheuscommunity/postgres-exporter` | `latest` | `v0.15.0` |
| 2 | redis-exporter | `oliver006/redis_exporter` | `latest` | `v1.62.0` |
| 3 | kafka-exporter | `danielqsj/kafka-exporter` | `latest` | `v1.7.0` |
| 4 | kafka-ui | `provectuslabs/kafka-ui` | `latest` | `v1.0.0` |
| 5 | **opa** | **`openpolicyagent/opa`** | **`latest`** | **`0.70.0`** |
| 6 | ollama | `ollama/ollama` | `latest` | `0.3.0` (profile-gated) |
| 7 | prometheus | `prom/prometheus` | `latest` | `v2.53.0` |
| 8 | grafana | `grafana/grafana` | `latest` | `11.1.0` |
| 9 | loki | `grafana/loki` | `latest` | `3.0.0` |

#### docker-compose.chaos.yml (2 `latest` tags)

| # | Service | Image | Current | Recommended Pin |
|---|---------|-------|---------|-----------------|
| 1 | prometheus | `prom/prometheus` | `latest` | `v2.53.0` |
| 2 | grafana | `grafana/grafana` | `latest` | `11.1.0` |

#### Helm values.yaml (3 `latest` tags)

| # | Image | Current | Notes |
|---|-------|---------|-------|
| 1 | frontend | `enterprise-crm/frontend:latest` | CI overrides with `${{ github.sha }}` at deploy |
| 2 | gateway | `enterprise-crm/gateway:latest` | Same |
| 3 | agents | `enterprise-crm/agents:latest` | Same |

### 4.2 Risk Classification

**Finding D-IV-1 (BLOCKER): OPA uses `:latest` in main compose**

OPA is a critical authz component. A breaking OPA update could silently change policy evaluation behavior. CI already pins to `0.70.0` in the OPA test job — the compose should match.

**Fix:** Pin to `openpolicyagent/opa:0.70.0`.

**Finding D-IV-2 (Should Fix): 8 remaining `:latest` tags in docker-compose.yml**

All are observability/utility services. Risk is low (not in auth/data path) but `:latest` means `docker compose pull` can silently change versions across developer machines, causing "works on my machine" issues.

**Fix:** Pin all to current stable releases (see table above).

**Finding D-IV-3 (Should Fix): CI deploys use mutable SHA tags instead of digests**

`ci-cd.yml` exports immutable digests from `docker/build-push-action` (lines 629-638) but the staging/production deploy steps use `--set images.X.tag=${{ github.sha }}` which is a mutable tag reference. The CI file itself has comments acknowledging this gap.

**Fix:** Pass `sha256:...` digests to `--set` instead of tag references. Requires aggregating the 3 build matrix digests into a single deploy input.

**Finding D-IV-4 (Should Fix): Chaos OPA pinned to older version (0.55.0 vs 0.70.0)**

`docker-compose.chaos.yml` pins OPA to `0.55.0` while CI tests run against `0.70.0`. This means chaos tests validate policy evaluation against a different OPA version than CI.

**Finding D-IV-5 (OK): Base images already pinned**

All Dockerfiles use pinned base images: `node:20-bullseye`, `node:20-alpine`, `python:3.11-slim`, `node:20-bullseye-slim`. No `latest` in any Dockerfile. ✅

**Finding D-IV-6 (OK): Infrastructure images already pinned**

`postgres:16-alpine`, `redis:7-alpine`, `confluentinc/cp-kafka:7.5.0`, `semitechnologies/weaviate:1.23.0`, `quay.io/keycloak/keycloak:23.0`, `nginx:1.27-alpine`, `alpine:3.19` — all pinned to at least major.minor. ✅

### 4.3 Digest Pinning (Defer)

For critical data-path infrastructure, full digest pinning would prevent supply-chain attacks and accidental tag mutations. Recommend digest pinning for Postgres, Redis, Kafka — but this is a Defer item (requires operational process for digest rotation).

---

## 5. CI / Local Verification Paths

### 5.1 Verification Commands (Post-Implementation)

After Group D changes are applied, verify with:

```bash
# 1. Compose config validation (no syntax errors)
docker compose config > /dev/null
docker compose -f docker-compose.chaos.yml config > /dev/null

# 2. Healthcheck validation (all services become healthy)
docker compose up -d --wait postgres redis kafka opa weaviate keycloak
docker compose ps  # all should show "healthy"

# 3. Dependency chain verification
docker compose up -d --wait gateway agents replay-service
# gateway should wait for opa (healthy), not just started
# agents should wait for weaviate (healthy) + opa (healthy)

# 4. Full smoke test
docker compose --profile migrate run --rm migrate
docker compose --profile smoke-test run --rm smoke-test

# 5. WS proxy smoke
docker compose --profile ws-proxy-test run --rm ws-proxy-test

# 6. Chaos (workflow_dispatch only)
# Trigger manually via GitHub UI: Actions → Chaos Engineering → Run workflow

# 7. Gateway/Frontend tests (unchanged)
cd gateway && npm test
cd frontend && npm run lint && npx tsc --noEmit && npm run build

# 8. Image pinning verification
grep ':latest' docker-compose.yml docker-compose.chaos.yml
# Should return 0 results after Group D
```

### 5.2 CI Pipeline Impact

| Job | Impact |
|-----|--------|
| `smoke` | Benefits from correct health waits → faster startup |
| `ws-proxy-smoke` | Benefits from nginx healthcheck |
| `test-gateway` | Unchanged |
| `test-agents` | Unchanged |
| `chaos-tests` | Schedule removed; still runs on workflow_dispatch |

### 5.3 Verification Gaps (Acknowledged)

These are NOT Group D scope but are documented for awareness:
- No pre-commit hooks (Husky/lint-staged)
- No root-level `npm test` or `make test`
- Staging integration tests are placeholder (health check only)
- No production post-deploy smoke test
- Event projection (Outbox) not validated in Compose smoke test
- No frontend unit/component/E2E tests (TD-C3-4 deferred)

---

## 6. Risk Classification

### BLOCKER (must fix before Group D closure)

| ID | Finding | Section | Impact |
|----|---------|---------|--------|
| **D-C-1** | OPA dep: `service_started` → `service_healthy` (gateway + agents) | §1.2 | Gateway/agents start before OPA policies load; undetected policy failures |
| **D-C-2** | Weaviate dep: `service_started` → `service_healthy` (agents) | §1.2 | Agents start before Weaviate is ready; wasted retry cycles |
| **D-HC-1** | Frontend K8s probes → `/api/health` (404) | §3.2 | False liveness failures → pod restarts in K8s |
| **D-HC-2** | nginx frontend-proxy no healthcheck | §3.2 | Edge proxy has no health signal for depends_on or Docker |
| **D-HC-3** | Agents no Compose healthcheck, no `/ready` | §3.2 | Startup dependencies unchecked; K8s routes traffic to unready pods |
| **D-CH-1** | chaos-migrations doesn't reuse `migrate.sh` | §2.5 | Schema drift between chaos and main stacks; exit 2 failures |
| **D-IV-1** | OPA `:latest` in main compose | §4.2 | Breaking OPA update could change policy evaluation silently |

### Should Fix (strongly recommended)

| ID | Finding | Section |
|----|---------|---------|
| **D-C-3** | Gateway doesn't depend on `migrate` (documentation gap) | §1.2 |
| **D-CH-2** | Remove chaos schedule trigger (keep workflow_dispatch) | §2.5 |
| **D-CH-3** | Pin chaos OPA to 0.70.0 (match CI) | §2.5 |
| **D-HC-4** | Kafka no `start_period` | §3.3 |
| **D-HC-5** | Postgres no `start_period` | §3.3 |
| **D-HC-6** | Gateway Compose healthcheck params inconsistent with Dockerfile | §3.3 |
| **D-HC-7** | Keycloak no healthcheck | §3.3 |
| **D-IV-2** | 8 remaining `:latest` in docker-compose.yml | §4.2 |
| **D-IV-3** | CI deploys use mutable SHA tags, not digests | §4.2 |
| **D-IV-4** | Chaos OPA pinned to 0.55.0 (drift from CI's 0.70.0) | §4.2 |

### Defer (acknowledged, out of scope)

| ID | Finding | Target |
|----|---------|--------|
| **D-C-4** | frontend-proxy/frontend bare depends_on → service_healthy | Group E/F (negligible impact) |
| **D-CH-4** | Extract shared compose services (chaos drift) | Group F/G (compose refactor) |
| **D-HC-8** | Observability sidecar healthchecks | Group F (non-critical) |
| **D-IV-5** | Digest pinning for Postgres/Redis/Kafka | Post-Group D (requires process) |
| **TD-D-1** | No pre-commit hooks | Backlog |
| **TD-D-2** | No root-level test runner | Backlog |
| **TD-D-3** | Staging integration tests are placeholder | Group E/F |
| **TD-D-4** | No production smoke test | Group E/F |

---

## 7. D1/D2/D3 Implementation Plan

### D1 — Compose Health + Healthcheck Foundation (est. 5-6 files)

**Goal:** Fix all dependency conditions, add missing healthchecks, align params.

**Files:**
- `docker-compose.yml` — 3 condition changes + 4 new healthchecks + 2 param fixes
- `conf/nginx.conf` — add `location = /health` block
- `frontend/src/app/api/health/route.ts` (NEW) — health endpoint for K8s probes
- `agents/src/orchestrator/main.py` — optionally add `/ready` endpoint
- `gateway/src/tests/` — optionally add `/ready` tests
- `deploy/helm/.../templates/agents.yaml` — optionally update readiness probe

**Changes:**
1. OPA dep: `service_started` → `service_healthy` (gateway, agents) — **low risk**
2. Weaviate dep: `service_started` → `service_healthy` (agents) — **low risk**
3. Add nginx `location = /health` + Compose healthcheck — **low risk**
4. Add `frontend/src/app/api/health/route.ts` — **low risk**
5. Add agents Compose healthcheck (existing `/health` endpoint) — **low risk**
6. Optionally add agents `/ready` endpoint — **medium risk** (application code change; needs tests)
7. Add Kafka `start_period: 60s`, Postgres `start_period: 15s` — **low risk**
8. Keycloak healthcheck — **conditional** (downgrade to Should Fix if KC_HEALTH_ENABLED cannot be verified locally)

**Risk classification:**
- Compose dependency/healthcheck changes: **low risk** (only development tooling; no production code paths)
- Agents `/ready` endpoint: **medium risk** (application code change in Python; requires unit/integration tests; must not fail on transient Kafka/Weaviate issues)
- Helm probe updates: **medium risk** if `/ready` is not fully tested (defer to D1 follow-up)

**Exit criteria:**
- `docker compose config --quiet` valid
- `docker compose -f docker-compose.chaos.yml config --quiet` valid
- `pytest tests/infra -v` passes
- `docker compose up -d --wait` succeeds for all services
- `docker compose ps` shows all "healthy"
- `docker compose --profile smoke-test run --rm smoke-test` passes
- `docker compose --profile ws-proxy-test run --rm ws-proxy-test` passes
- Gateway lint + test unchanged (147P/0F)
- Frontend lint + tsc + build unchanged

**Rollback risk:**
- Compose changes: revert file edits, no data migration needed
- Frontend `/api/health` route: delete file, no other code depends on it
- Agents `/ready`: revert the endpoint addition; K8s probes unaffected if not updated
- All changes are additive or condition-strengthening; none weaken existing behavior

---

### D2 — Image Pinning (est. 3 files)

**Goal:** Eliminate all `:latest` tags; fix digest-based deploys.

**Files:**
- `docker-compose.yml` — 9 tag pins
- `docker-compose.chaos.yml` — 2 tag pins + OPA version bump
- `.github/workflows/ci-cd.yml` — digest-based deploy (if feasible without cluster access)

**Changes:**
1. Pin all 9 `:latest` in docker-compose.yml to current stable versions
2. Pin 2 `:latest` in docker-compose.chaos.yml
3. Bump chaos OPA from `0.55.0` to `0.70.0`
4. (Optional) Wire build digests into deploy steps — **may be Deferred if no cluster access**

**Exit criteria:**
- `grep ':latest' docker-compose.yml docker-compose.chaos.yml` returns 0
- `docker compose pull` succeeds with pinned tags
- CI smoke + ws-proxy-smoke unchanged
- OPA 0.70.0 policies compile (same as CI)

**Rollback risk:** Minimal (tags are pull-only; no build changes). If a pinned tag is unavailable on Docker Hub, revert to previous tag.

---

### D3 — Chaos Cleanup (est. 2 files)

**Goal:** Fix chaos-migrations, remove schedule, align versions.

**Files:**
- `docker-compose.chaos.yml` — replace inline migration with migrate.sh runner
- `.github/workflows/chaos-tests.yml` — remove schedule trigger

**Changes:**
1. Replace `chaos-migrations` inline shell script with `bash /scripts/migrate.sh`
   - Requires: add `migrate` service to chaos compose (or a dedicated migration runner image)
   - Alternative: build a minimal image with `node`, `npm`, `postgresql-client` that can run both Prisma and psql
2. Remove `schedule: cron: "0 3 * * *"` trigger; keep `workflow_dispatch`
3. Pin OPA to `0.70.0`

**Exit criteria:**
- `docker compose -f docker-compose.chaos.yml config` valid
- `docker compose -f docker-compose.chaos.yml run --rm chaos-migrations` exits 0
- Chaos workflow triggers only on `workflow_dispatch`

**Rollback risk:** Low. Chaos workflow is not a CI gate. If migration runner change breaks, revert to inline script but document the Prisma dependency.

---

## 8. Exit Criteria (Group D)

| # | Criterion | Phase |
|---|-----------|-------|
| 1 | `grep 'condition: service_started' docker-compose.yml` returns only ws-proxy-test→frontend-proxy | D1 |
| 2 | All long-running services have healthchecks (nginx, agents, keycloak) | D1 |
| 3 | `docker compose up -d --wait` completes with all services healthy | D1 |
| 4 | `grep ':latest' docker-compose.yml docker-compose.chaos.yml` returns 0 | D2 |
| 5 | Gateway lint/build/test unchanged (147P/0F) | D1-D3 |
| 6 | Frontend lint/tsc/build unchanged | D1 |
| 7 | CI smoke + ws-proxy-smoke green | D1-D3 |
| 8 | Chaos workflow triggers only on `workflow_dispatch` | D3 |
| 9 | chaos-migrations uses unified `migrate.sh` path | D3 |
| 10 | Frontend K8s probes point to existing endpoint | D1 |

---

## Appendix A: File Inventory

| File | Role |
|------|------|
| `docker-compose.yml` | Main service topology (16 services, 4 profiles, 649 lines) |
| `docker-compose.chaos.yml` | Chaos stack topology (10 services, 193 lines) |
| `conf/nginx.conf` | Frontend-proxy nginx configuration |
| `.github/workflows/chaos-tests.yml` | Chaos engineering workflow |
| `.github/workflows/ci-cd.yml` | Main CI/CD pipeline (14 jobs) |
| `scripts/migrate.sh` | Unified migration runner |
| `scripts/smoke-test.sh` | Auth + CRUD smoke test |
| `scripts/ws-proxy-test.js` | WS proxy E2E smoke test |
| `deploy/helm/enterprise-crm/templates/frontend.yaml` | Frontend K8s deployment (probes at lines 50-63) |
| `deploy/helm/enterprise-crm/templates/gateway.yaml` | Gateway K8s deployment (probes at lines 66-79) |
| `deploy/helm/enterprise-crm/templates/agents.yaml` | Agents K8s deployment (probes at lines 57-70) |
| `deploy/helm/enterprise-crm/values.yaml` | Default Helm values |
| `gateway/Dockerfile` | Gateway multi-stage build |
| `agents/Dockerfile` | Agents Python build |
| `frontend/Dockerfile` | Frontend Next.js build |
| `agents/src/orchestrator/main.py` | Agents startup + health endpoints |
| `gateway/src/index.ts` | Gateway startup + health/ready endpoints |

## Appendix B: Group B/C Invariant Preservation

Group D does **not** modify:
- Any auth logic (login, register, refresh, logout, ws-ticket, /me)
- Any CSRF or origin validation
- Any WebSocket ticket exchange or proxy routing
- Any JWT claim contract or token revocation
- Any RLS policies or OPA Rego rules
- Any Kafka topic configuration or consumer groups
- Any Helm service/ingress definitions (except frontend probe path fix)
