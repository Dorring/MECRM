# Group G Preflight -- Global Hardening Closeout / Release Readiness

**Date:** 2026-07-12
**Baseline:** `main@54bfc31` (Group F fully closed)
**G1 Status:** COMPLETED / STABILIZED
**G1 Tag:** `hardening-group-g-g1-stabilized` -> `8690ee5`
**G1 CI:** CI/CD Pipeline #94 + Tenant Isolation Proof #94 all green

## Executive Summary

All six hardening groups (A-F) have closed with stabilization tags. The repository has
a comprehensive test suite (238 infra tests), a mature CI pipeline with 14 jobs, a Helm
chart with Bitnami subchart dependencies, and multi-stage Docker images with digest
tracking. However, several release-readiness gaps remain: digest-based deploys are still
deferred, no supply-chain security scanning exists, staging/production cluster validation
has weak coverage, and the frontend has zero test framework.

The most immediate gaps are **G1 (digest pinning)** and **G2 (supply-chain security scan)**,
both P1 blockers for any production release.

---

## 1. Tags / Closeout Consistency

### 1.1 Tag Inventory

| Tag | Target Commit | Date | Status |
|---|---|---|---|
| `hardening-group-b-stabilized` | `5c73e33` | 2026-07-06 | OLD — points to a hotfix-merge, not a closeout commit |
| `hardening-group-b-stabilized.1` | `9e44a64` | 2026-07-06 | CORRECT — points to Group B closeout commit |
| `hardening-group-c-c3-stabilized` | `2156097` | 2026-07-09 | CORRECT |
| `hardening-group-c-c4-stabilized` | `1f4287c` | 2026-07-09 | CORRECT |
| `hardening-group-c-stabilized` | `63f1935` | 2026-07-10 | CORRECT — Group C composite tag |
| `hardening-group-d-d1-stabilized` | `4336889` | 2026-07-10 | CORRECT |
| `hardening-group-d-d2-stabilized` | `4422593` | 2026-07-10 | CORRECT |
| `hardening-group-d-d3-stabilized` | `2014732` | 2026-07-11 | CORRECT |
| `hardening-group-e-e1e2-stabilized` | `d6d60cc` | 2026-07-11 | CORRECT |
| `hardening-group-f-f2-stabilized` | `d6d5568` | 2026-07-12 | CORRECT |
| `hardening-group-f-f3-stabilized` | `402a273` | 2026-07-12 | CORRECT |
| `hardening-group-f-f4-stabilized` | `ef591ff` | 2026-07-12 | CORRECT |
| `hardening-group-f-stabilized` | `ef591ff` | 2026-07-12 | CORRECT — Group F composite tag |

### 1.2 Tag Issues

| ID | Issue | Severity |
|---|---|---|
| **G-T1** | `hardening-group-b-stabilized` and `hardening-group-b-stabilized.1` both exist. The first tag (`5c73e33`) points to a hotfix merge commit, not the closeout commit (`9e44a64`). The `.1` suffix is a workaround for a retagged stabilization. This is a documentation/history artifact -- both tags exist on the remote and are immutable by convention; deleting a remote tag that other clones may have fetched is more disruptive than leaving it. Default: keep both. | Defer -- documentation/history issue; not a CI or deploy concern |
| **G-T2** | Groups A and D have no composite `-stabilized` tags. Group C (`hardening-group-c-stabilized` -> `63f1935`) and Group F (`hardening-group-f-stabilized` -> `ef591ff`) do. Inconsistency in tag naming convention. | Defer -- cosmetic only, no functional impact |
| **G-T3** | Group E has only `hardening-group-e-e1e2-stabilized`, no `hardening-group-e-stabilized`. If E is considered closed, the composite tag is missing. | Defer -- consistency item |

### 1.3 Closeout Docs vs Reality

| Self-review doc | Actual merge/closeout match |
|---|---|
| `self-review-group-f-f2.md` | `d6d5568` -- matches tag |
| `self-review-group-f-f3.md` | `402a273` -- matches tag |
| `self-review-group-f-f4.md` | `ef591ff` -- matches tag (updated with actual CI artifact data) |
| Earlier groups (B/C/D/E) | Not re-verified against current main; assumed correct from closeout docs |

---

## 2. CI/CD Release Readiness

### 2.1 Digest Pinning Status (E3 / Phase 4)

**Status: NOT DONE. Deploy still uses mutable `${{ github.sha }}` tags.**

Evidence from `.github/workflows/ci-cd.yml`:
- **Deploy-staging** (line 753): `--set images.gateway.tag=${{ github.sha }}` etc.
- **Deploy-production** (line 816): Same pattern.
- The build job **does export the digest** (`steps.export-digest.outputs.digest` per matrix project), but since builds are per-matrix (3 parallel jobs), the digest is scattered across job outputs and not aggregated for deploy consumption.
- Code comments in deploy jobs state: "Phase 4 CI/CD: deploys SHOULD reference the immutable image digest... Tag-based deploy is retained for now."

| ID | Finding | Severity |
|---|---|---|
| **G-C1** | Deploy uses mutable tag `${{ github.sha }}` instead of image digest `sha256:...`. A retagged image (force push to same sha) or a cache-corrupt rebuild produces a different image for the same tag. Tags are NOT immutable -- two pushes of the same commit can produce different image digests if layers change. | **Blocker** for production release |
| **G-C2** | No digest-map aggregation step exists to collect 3 matrix job digests into one deploy-consumable artifact. The build matrix produces 3 independent digests; deploy needs all 3. | Should Fix (prerequisite for G-C1 fix) |

### 2.2 CI Job Inventory

The `.github/workflows/ci-cd.yml` has 14 jobs:

| # | Job | Trigger | Gate |
|---|---|---|---|
| 1 | lint (gateway, frontend) | push/PR | — |
| 2 | lint-python | push/PR | — |
| 3 | validate-schemas | push/PR | — |
| 4 | test-gateway | push/PR | needs lint |
| 5 | test-agents | push/PR | needs lint-python |
| 6 | migration-runner | push/PR | — |
| 7 | smoke (Compose + Smoke Test) | push/PR | needs test-gateway, test-agents, test-policies, migration-runner |
| 8 | ws-proxy-smoke | push/PR | needs lint, lint-python |
| 9 | test-policies | push/PR | — |
| 10 | helm-lint (lint + template) | push/PR | — |
| 11 | **build** (Build & Push) | **push to main only** | needs test-gateway, test-agents, test-policies, validate-schemas, helm-lint, smoke |
| 12 | deploy-staging | push to main only | needs build |
| 13 | integration-tests | push to main only | needs deploy-staging |
| 14 | deploy-production | push to main only | needs integration-tests |

### 2.3 Node.js 20 Deprecation

**Node.js 20 reaches EOL on 2026-04-30.** GitHub Actions has begun emitting deprecation warnings for `actions/setup-node@v4` with `node-version: "20"`.

Affected `actions/setup-node@v4` invocations in this repo:

| File | Lines | Count |
|---|---|---|
| `.github/workflows/ci-cd.yml` | 27, 126, 245, 315, 787 | 5 occurrences |
| `.github/workflows/tenant-isolation.yml` | 51 | 1 occurrence |
| **Total** | | **6 occurrences** |

All Dockerfiles also use `node:20-*` base images.

| ID | Finding | Severity |
|---|---|---|
| **G-C3** | Node.js 20 EOL 2026-04-30. All CI jobs and Docker base images pin to Node 20. At minimum, CI needs `node-version: "22"` (or `"lts/*"`). Dockerfiles need `node:22-*` base images. This is 7 months from now but should be planned. | Should Fix -- becomes Blocker by 2026-04 |

### 2.4 Security Scan / SBOM / Provenance

**No supply-chain security scanning is configured.**

Searched for: `trivy`, `sbom`, `provenance`, `cosign`, `syft`, `grype`, `codeql`, `snyk`, `dependabot`.

Results:
- **Dependabot:** NOT configured (no `.github/dependabot.yml`).
- **CodeQL / GitHub Security:** NOT configured (no `.github/workflows/codeql*.yml`).
- **Trivy / Grype / Syft / Cosign:** NOT configured. Found only in `.agent/` planning docs and vulnerability-scanner skill definitions (workflow templates, not active CI).
- **Docker image SBOM/provenance:** NOT enabled. `docker/build-push-action@v5` supports `provenance: true` and `sbom: true` -- not used.

| ID | Finding | Severity |
|---|---|---|
| **G-C4** | No container image vulnerability scanning (Trivy, Grype). Every push to main builds and pushes images to GHCR without scanning for known CVEs. | **Blocker** for production release |
| **G-C5** | No dependency vulnerability scanning (Dependabot, npm audit CI gate, pip audit CI gate). | Should Fix |
| **G-C6** | No SBOM generation or provenance attestation for container images. `docker/build-push-action@v5` has native support. | Should Fix |
| **G-C7** | No CodeQL or SAST scanning on source code. | Should Fix |

### 2.5 Image Size Regression Guard

The F3 CI metrics mechanism (`image-metrics-*` artifacts) collects per-image size, layer count, and docker history on every main push. However, there is no automated comparison or regression gate.

| ID | Finding | Severity |
|---|---|---|
| **G-C8** | Image metrics are collected as artifacts but no PR-check job compares them against the baseline. F3b (size regression warning) was deferred. | Should Fix |

---

## 3. Helm / Kubernetes Readiness

### 3.1 Chart Inventory

| File | Purpose |
|---|---|
| `Chart.yaml` | Chart metadata + 4 Bitnami subchart dependencies (postgresql 14.0.0, redis 18.0.0, kafka 26.0.0, keycloak 17.0.0) |
| `values.yaml` | Default values: tag is intentionally empty (`required` fails if not `--set`) |
| `values-staging.yaml` | Staging overrides |
| `values-production.yaml` | Production overrides |
| `templates/gateway.yaml` | Gateway Deployment + Service + HPA + PDB |
| `templates/agents.yaml` | Agents Deployment + Service + HPA + PDB |
| `templates/frontend.yaml` | Frontend Deployment + Service + HPA + PDB |
| `templates/ingress.yaml` | NGINX Ingress with TLS, routing / /api/ /ws |
| `templates/networkpolicy.yaml` | NetworkPolicy for pod-to-pod traffic |
| `templates/serviceaccount.yaml` | ServiceAccount |
| `templates/_helpers.tpl` | Shared template functions |

### 3.2 Image Tag Governance

**Assessment: Strong but tag-only, no digest support.**

- `values.yaml`: `images.*.tag` is intentionally empty. Templates use `required` to fail-fast if CI doesn't set it. This prevents accidental `latest` deploys -- **good practice**.
- Templates: image reference format is `repository:tag` only. No digest field in `values.yaml` or templates.
- To switch to digest-based deploys, templates need a `digest:` field in values, and the deploy command must use `--set images.*.digest=sha256:...` instead of `--set images.*.tag=...`.

| ID | Finding | Severity |
|---|---|---|
| **G-H1** | Helm chart has no `digest` field in `images.*`. Only `tag` is supported. | Should Fix (prerequisite for G-C1) |
| **G-H2** | Bitnami subchart versions (postgresql 14.0.0, redis 18.0.0, kafka 26.0.0, keycloak 17.0.0) need periodic version audits. These are stable release channels but may have security updates. | Should Fix |
| **G-H3** | `securityContext.runAsUser: 1000` in template but F4 agents image uses uid 1001. If `node` user (uid 1000) does not exist in the agents image, the pod will fail to start. | **Blocker** -- needs verification or fix |

### 3.3 Ingress / WebSocket Routing

Ingress routes:
- `/` → frontend (API config, static assets)
- `/api/config` → frontend (Exact match before Prefix `/api`)
- `/api` → gateway (API Gateway)
- `/ws` → gateway (WebSocket upgrade)

WebSocket proxy routing has CI-level smoke test (`ws-proxy-smoke` job) and 7 static tests in `test_ws_proxy.py`. The nginx-based `frontend-proxy` in Compose has been smoke-tested. However, **no real Kubernetes Ingress Controller validation** has been performed.

| ID | Finding | Severity |
|---|---|---|
| **G-H4** | WebSocket routing through Ingress to Gateway has static tests + Compose smoke test, but zero real-cluster validation (actual NGINX Ingress Controller + TLS termination). | Should Fix -- staging validation needed |
| **G-H5** | Ingress `/ws` through NGINX Ingress Controller with TLS (WSS) has not been validated on a real cluster. The values.yaml already includes `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` and `proxy-send-timeout: "3600"`, which should cover WebSocket idle timeouts. If staging validation reveals dropped connections, additional annotations (e.g., `nginx.ingress.kubernetes.io/websocket-services`) may be needed -- but that is a validation outcome, not a confirmed configuration gap. | Staging validation item -- not a confirmed defect |

### 3.4 Migration Job / Rollback

- **Compose-level:** `migrate` service uses `scripts/migrate.sh` with advisory lock. Works well locally.
- **Kubernetes-level:** No migration Job template in the Helm chart. No Helm hook (pre-upgrade, post-install) for database migrations.
- **Rollback:** No rollback migration strategy documented. No `helm rollback`-aware migration logic.

| ID | Finding | Severity |
|---|---|---|
| **G-H6** | No Kubernetes migration Job in Helm chart. `migrate.sh` exists and works in Compose, but it is not templated as a K8s Job/Hook. | **Blocker** for production release |
| **G-H7** | No documented rollback strategy for database migrations. If `helm rollback` reverts the application but not the DB schema, the stack is broken. | Should Fix |

### 3.5 Security Context Inconsistency

| Workload | Template `runAsUser` | Dockerfile USER | Match? |
|---|---|---|---|
| gateway | 1000 | `node` (uid 1000 on node:20-bullseye) | OK |
| agents | 1000 | `app` (uid 1001) | **MISMATCH -- G-H3** |
| frontend | 1000 | `nextjs` (uid 1001) | **MISMATCH** |

---

## 4. Runtime Reliability Backlog

### 4.1 Kafka DLQ / Poison Message Handling

- **Outbox retry:** `outbox_events` table has `retry_count`, `next_attempt_at`, `dead_lettered_at` columns. The retry infrastructure exists in the database schema.
- **Consumer DLQ:** Kafka consumer DLQ handling is NOT implemented as a standalone processor. `.env.example` has `KAFKA_DLQ_RETENTION_MS` configured. The `.agent/` directory references DLQ handling in skill definitions, but application-level DLQ consumer code is absent.
- **Poison message recovery:** No automated poison message replay or dead-letter queue consumer exists in the application layer.

| ID | Finding | Severity |
|---|---|---|
| **G-R1** | DLQ infrastructure exists at DB schema + env var level, but no DLQ consumer/processor is implemented. Poison messages can accumulate in dead-letter with no automated recovery. | Should Fix |
| **G-R2** | No Kafka consumer retry/backoff configuration in the gateway consumer code. If a message fails processing, the consumer either commits and loses it or loops infinitely. | Should Fix |

### 4.2 Agents `/ready` Endpoint

- **Gateway** has a `/ready` endpoint (`gateway/src/index.ts:141`) that checks revocation subscriber readiness. Kubernetes `readinessProbe` uses it.
- **Agents** has a `/health` endpoint (`orchestrator/main.py:430`) but **no `/ready` endpoint**. Kubernetes `readinessProbe` in `agents.yaml` (line 67) uses `/health` for both liveness and readiness probes.

| ID | Finding | Severity |
|---|---|---|
| **G-R3** | agents `readinessProbe` uses `/health` instead of `/ready`. A healthy agent may not be ready to process traffic (Kafka consumer not yet subscribed, DB connection pool not warm). | Should Fix |

### 4.3 Keycloak Healthcheck

- **docker-compose.yml**: `keycloak` service has NO `healthcheck` block. Other services (`postgres`, `redis`, `kafka`) have healthchecks. Keycloak depends only on `postgres: service_healthy` but downstream services do not wait for Keycloak to be healthy.
- **Helm chart**: Keycloak is a Bitnami subchart; its healthcheck depends on the chart's own probe configuration.

| ID | Finding | Severity |
|---|---|---|
| **G-R4** | Compose `keycloak` service has no healthcheck. Gateway may start before Keycloak is ready to issue tokens, causing transient auth failures. | Should Fix |

### 4.4 Production Smoke Test / Staging Integration Test

**Staging integration test** (`tests/integration/health.test.js`) is a **placeholder**: 45 lines that test `/health` endpoint only. It does NOT test:
- Tenant registration + user login
- Lead CRUD + list with 401 check
- WebSocket ticket exchange + connect
- Agent assignment workflow

**Production smoke test** (Compose `smoke-test` profile via `scripts/smoke-test.sh`) is more comprehensive but runs only in Compose, not against staging/production.

The CI `integration-tests` job runs after deploy-staging and skips cleanly if `STAGING_API_URL` is not configured. When secrets ARE configured, it only runs `health.test.js` which is a single endpoint hit.

| ID | Finding | Severity |
|---|---|---|
| **G-R5** | Integration tests on staging are a placeholder (45-line health check only). No real smoke test runs against the deployed staging cluster. | Should Fix |
| **G-R6** | No production smoke test post-deploy. `deploy-production` includes a Slack notification but no automated validation that the deployed stack actually works. | Should Fix |

---

## 5. Frontend Quality Backlog

### 5.1 Test Framework Gap

- **No test framework is configured for frontend.** `frontend/package.json` has no `jest`, `vitest`, `@testing-library/react`, or any test runner dependency. There are no `.test.ts`, `.test.tsx`, `.spec.ts`, or `.spec.tsx` files.
- `gateway/` uses Jest with coverage. `agents/` uses pytest with coverage. Frontend has zero coverage.

| ID | Finding | Severity |
|---|---|---|
| **G-F1** | Frontend has no test framework (Vitest/Jest/RTL). Zero unit, integration, or E2E tests. | Should Fix -- becomes Blocker for production release if any frontend logic is user-facing |

### 5.2 Auth Runtime Functions Without Tests

Three critical auth modules have no test coverage:

| Module | Lines | Complexity | Risk Without Tests |
|---|---|---|---|
| `lib/api.ts` | ~400+ | High -- token lifecycle, cookie refresh, CSRF, fetch wrapper, API client | Auth breakage = all API calls fail silently |
| `lib/runtime-config.ts` | 72 | Medium -- API/WS URL resolution, priority chain | Wrong URL = broken API calls |
| `hooks/useWebSocket.tsx` | 369 | High -- ticket exchange, reconnect policy, 4401 auth retry, permanent failure detection | WS breakage = no real-time updates |

| ID | Finding | Severity |
|---|---|---|
| **G-F2** | `api.ts`, `runtime-config.ts`, `useWebSocket.tsx` are the three most critical client-side modules and have zero tests. A regression in token refresh or WS reconnect would not be caught until users report it. | Should Fix |

### 5.3 TODO UI (Product Backlog -- Not a Hardening Blocker)

- Notification center (no notification UI)
- Create forms for leads/deals/tickets (list pages exist, create forms do not)
- Agent management console (agents page exists as placeholder)

These are product feature gaps, not hardening items. Marked as **Defer -- Product backlog**.

---

## 6. Docker / Image Follow-Up

### 6.1 Agents Requirements Runtime/Dev Split (F-D7)

`agents/requirements.txt` includes test dependencies (`pytest`, `pytest-asyncio`, `pytest-cov`) that are now installed in the builder stage and copied into the runtime image. This adds unnecessary bytes (~50-80MB for pytest + plugins + coverage).

| ID | Finding | Severity |
|---|---|---|
| **G-D1** | Test dependencies in runtime image. Split `requirements.txt` into `requirements.in` (runtime) and `requirements-dev.in` (test) per F-D7. | Should Fix |

### 6.2 Gateway libssl1.1 Removal (F-D1)

`gateway/Dockerfile` line 5: `apt-get install -y --no-install-recommends openssl libssl1.1`. `node:20-bullseye` already ships `libssl3`. The `libssl1.1` package may be unused.

| ID | Finding | Severity |
|---|---|---|
| **G-D2** | `libssl1.1` possibly unused in gateway builder. Requires `ldd` verification against built artifacts. | Should Fix |

### 6.3 Cold-Start / Health-Ready Time Metrics (F-D6)

The F1 `collect-image-metrics.sh` has cold-start measurement capability (`COLLECT_COLD_START=0` to skip) but it was never executed with Docker available. No cold-start metrics exist.

| ID | Finding | Severity |
|---|---|---|
| **G-D3** | No cold-start or health-ready time metrics for any service. Autoscaling and deployment timing depend on these numbers. | Should Fix |

### 6.4 Image Size Regression Guard (F3b / G-C8)

Same as G-C8 -- recorded in CI section for visibility. Duplicate reference for completeness.

---

## 7. Findings Summary

### Blocker (Production Release Gate)

| ID | Finding | Phase |
|---|---|---|
| **G-C1** | Deploy uses mutable `${{ github.sha }}` tag, not digest. Tags are not immutable. | **CLOSED (G1)** |
| **G-C4** | No container image vulnerability scanning (Trivy). All images pushed to GHCR unscanned. | G2 |
| **G-H3** | Helm `securityContext.runAsUser: 1000` mismatches agents image uid 1001 and frontend image uid 1001. Pods may fail to start. | **CLOSED (G1)** |
| **G-H6** | No Kubernetes migration Job in Helm chart. Database schema cannot be deployed to K8s. | G3 |

### Should Fix

| ID | Finding | Phase |
|---|---|---|
| **G-C2** | No digest-map aggregation for matrix build outputs. | **CLOSED (G1)** |
| **G-C3** | Node.js 20 EOL 2026-04-30. Plan migration to Node 22. | G2 |
| **G-C5** | No Dependabot or automated dependency vulnerability scanning. | G2 |
| **G-C6** | No SBOM/provenance for container images. | G2 |
| **G-C7** | No CodeQL/SAST on source code. | G2 |
| **G-C8** | No image size regression PR-check gate (F3b deferred). | G4 |
| **G-H1** | Helm chart has no `digest` field for images. | **CLOSED (G1)** |
| **G-H2** | Bitnami subchart versions need periodic audit. | G2 |
| **G-H4** | WebSocket/Ingress routing has no real-cluster validation. | G3 |
| **G-H5** | Ingress WSS has no real-cluster validation; existing timeout annotations are likely sufficient. | G3 |
| **G-H7** | No DB migration rollback strategy documented. | G3 |
| **G-R1** | DLQ consumer/processor not implemented. | G4 |
| **G-R2** | No retry/backoff in Kafka consumer error handling. | G4 |
| **G-R3** | agents `readinessProbe` uses `/health` (should use `/ready`). | G4 |
| **G-R4** | Compose `keycloak` service has no healthcheck. | G4 |
| **G-R5** | Integration tests on staging are a placeholder (health check only). | G3 |
| **G-R6** | No production smoke test post-deploy. | G3 |
| **G-F1** | Frontend has no test framework. | G5 |
| **G-F2** | Critical auth modules (`api.ts`, `runtime-config.ts`, `useWebSocket.tsx`) have no tests. | G5 |
| **G-D1** | Test dependencies in agents runtime image (requirements split). | G4 |
| **G-D2** | `libssl1.1` possibly unused in gateway builder. | G4 |
| **G-D3** | No cold-start/health-ready time metrics. | G4 |

### Defer

| ID | Finding | Reason |
|---|---|---|
| G-T1 | Duplicate Group B tag (`hardening-group-b-stabilized` + `.1`) | Documentation/history artifact; deleting remote tags is more disruptive than keeping them. Default: retain both. |
| G-T2 | Inconsistent composite tag naming (A/D missing) | Cosmetic, no functional impact |
| G-T3 | Group E missing composite tag | Cosmetic |
| TODO UI (notifications, create forms, agent console) | Missing product features | Product backlog, not hardening |

---

## 8. Recommended Next Implementation Order

```
G0 (doc) --> G1 (digest) --> G2 (security) --> G3 (cluster) --> G4 (reliability) --> G5 (frontend)
```

| Phase | Title | Depends On | Blocker? | Needs K8s/Secrets? |
|---|---|---|---|---|
| **G0** | Preflight doc cleanup | — | No | No |
| **G1** | Digest Pinning + Helm UID Fix | G0 | **CLOSED** -- `main@8690ee5`, tag `hardening-group-g-g1-stabilized` | — |
| **G2** | Supply-Chain Security Scan | G1 (needs digest ref for Trivy) | **Yes** (G-C4) | No -- CI-only |
| **G3** | Migration Job + Real-Cluster Validation | G1 (needs digest deploy) + G2 (scanned images) | **Yes** (G-H6) | Yes -- `KUBE_CONFIG_STAGING`, `STAGING_API_URL`, `TEST_USER_EMAIL`, `TEST_USER_PASSWORD` |
| **G4** | Reliability Backlog | G3 (real cluster validates readiness probes) | No | No (except readiness probe testing) |
| **G5** | Frontend Test Framework | None (parallelizable with G4) | No | No |

Rationale for ordering:
- **G0 first**: trivial doc fix, no code, sets the stage.
- **G1 before G2**: Trivy scans images by digest; until digest-based deploy works, the scanned digest may not match the deployed image.
- **G2 before G3**: staging should only run scanned images.
- **G3 before G4/G5**: readiness probes and smoke tests need a real cluster to validate; G4/G5 are independent of each other.

### G0 -- Preflight Doc Cleanup Only

**Goal:** Commit the Group G preflight document. Zero code changes.

**Scope:**
- Commit `docs/preflight-group-g.md` to main.
- No Dockerfile, CI workflow, Helm chart, or application code changes.

**Risk:** None (documentation only).
**Docker required:** No.
**K8s required:** No.
**Secrets required:** No.

**Verification:**
```bash
git diff --check
git log --oneline -1
```

---

### G1 -- Digest Pinning + Helm UID Fix

**Goal:** Fix the three items directly linked to deploy immutability and security context correctness.

**Scope:**
- G-C1 + G-C2: Implement digest-based deploy. Add a digest-map aggregation step that collects all 3 matrix digests into a single artifact. Update deploy-staging and deploy-production to use `--set images.*.digest=sha256:...` instead of `--set images.*.tag=...`.
- G-H1: Add `digest` field to `images.*` in Helm values + templates. Template image reference becomes `repository@digest` when digest is set, falling back to `repository:tag`.
- G-H3: Fix `securityContext.runAsUser` mismatch. Update Helm templates to use uid 1001 for agents and frontend (or document why 1000 works).

**Explicitly NOT in G1:**
- Tag cleanup (G-T1 is Defer -- documentation/history).
- Trivy/security scan (that is G2).
- K8s migration Job (that is G3).
- Real-cluster validation (that is G3).

**Risk:** Low (deploy changes) to Medium (if Kubernetes secrets/permissions are not configured for digest-pull from GHCR).
**Docker required:** Yes (to test digest-pull).
**K8s required:** Yes (staging cluster to verify deploy works with digests).
**Secrets required:** `KUBE_CONFIG_STAGING`.

**Verification:**
```bash
helm lint deploy/helm/enterprise-crm --set images.gateway.digest=sha256:test
helm template deploy/helm/enterprise-crm --set images.*.digest=sha256:test --set images.*.tag=""
docker run --rm ghcr.io/dorring/mecrm/gateway@sha256:<real-digest> node -e "console.log('ok')"
```

---

### G2 -- CI Supply-Chain / Security Scan

**Goal:** Add Trivy container scanning, Dependabot, SBOM/provenance, CodeQL, and Node 20 migration plan to CI pipeline. This is additive CI configuration -- no application logic changes.

**Scope:**
- G-C4: Add Trivy scan step to build job (after push, scan the pushed image by digest). Fail on CRITICAL/HIGH CVEs. Upload SARIF report to GitHub Security tab.
- G-C5: Add Dependabot config (`.github/dependabot.yml`) for npm (gateway, frontend) and pip (agents). Enable `npm audit` / `pip-audit` CI step.
- G-C6: Enable `provenance: true` and `sbom: true` on `docker/build-push-action@v5`.
- G-C7: Add CodeQL analysis workflow (GitHub's `github/codeql-action`).
- G-C3: Assess and document Node.js 22 migration plan with timeline (not block-scoped to G2 implementation, but the migration plan doc must be written).
- G-H2: Audit Bitnami subchart versions for known CVEs.

**Explicitly NOT in G2:**
- Digest pinning (that is G1).
- Migration Job or real-cluster validation (that is G3).

**Risk:** Low (additive CI steps). False-positive CVEs may need suppression config.
**Docker required:** Yes.
**K8s required:** No.
**Secrets required:** No (GitHub-native tools).

**Verification:**
```bash
# Trivy
trivy image ghcr.io/dorring/mecrm/agents@sha256:<digest>
# SBOM
docker buildx build --sbom=true --provenance=true ...
```

---

### G3 -- Migration Job + Staging Real-Cluster Validation

**Goal:** Validate the deployed stack on a real Kubernetes cluster with actual traffic. This phase requires KUBE_CONFIG_STAGING and staging secrets -- it cannot be completed purely locally.

**Scope:**
- G-H6: Create a Kubernetes migration Job in the Helm chart (using the migrate Dockerfile image). Add as a `helm.sh/hook: pre-upgrade` so migrations run before new pods roll out.
- G-H4 + G-H5: Deploy to staging cluster, verify WebSocket routing through NGINX Ingress Controller with TLS (WSS upgrade). Confirm existing timeout annotations are sufficient. Add WebSocket-specific annotations only if validation reveals dropped connections.
- G-R5: Expand integration tests beyond health check. Add authenticated smoke test (tenant registration + login + lead CRUD + WS connect).
- G-R6: Add post-deploy smoke test to production deploy job (curl the health endpoint, verify WebSocket upgrade).
- G-H7: Document rollback strategy for database migrations.

**Risk:** Medium-High. Requires a working Kubernetes cluster with secrets configured.
**Docker required:** Yes.
**K8s required:** Yes (staging cluster).
**Secrets required:** `KUBE_CONFIG_STAGING`, `STAGING_API_URL`, `TEST_USER_EMAIL`, `TEST_USER_PASSWORD`.

**Verification:**
```bash
kubectl get pods -n crm-staging
kubectl logs job/enterprise-crm-migrate -n crm-staging
curl -k https://staging.crm.example.com/api/health
wscat -c wss://staging.crm.example.com/ws
```

---

### G4 -- Reliability Backlog

**Goal:** Address DLQ, readiness probes, healthchecks, requirements split, cold-start metrics.

**Scope:**
- G-R1 + G-R2: Implement DLQ consumer/processor (Poison Message Handler). Add retry/backoff to Kafka consumers. This may be a significant code change -- scope appropriately.
- G-R3: Add `/ready` endpoint to agents (mirror gateway pattern). Update K8s `readinessProbe` to use it.
- G-R4: Add Keycloak healthcheck to docker-compose.yml. Add `KEYCLOAK_URL` health check in gateway.
- G-R5 + G-R6: (Shared with G3).
- G-C8: Implement F3b image size regression guard as PR-check job.
- G-D1: Split `agents/requirements.txt` into `requirements.in` + `requirements-dev.in`.
- G-D2: Verify and remove `libssl1.1` from gateway builder (requires `ldd` verification).
- G-D3: Collect cold-start metrics in CI using `scripts/collect-image-metrics.sh`.

**Risk:** Low-Medium. DLQ implementation may touch application logic.
**Docker required:** Yes.
**K8s required:** No (except for readiness probe testing).
**Secrets required:** No.

---

### G5 -- Frontend Test Framework

**Goal:** Establish frontend test infrastructure and cover critical auth modules.

**Scope:**
- G-F1: Install Vitest + React Testing Library. Configure `frontend/vitest.config.ts`. Add `npm test` script.
- G-F2: Write tests for `lib/api.ts` (token lifecycle, cookie refresh, CSRF, error handling), `lib/runtime-config.ts` (priority chain, fallback), `hooks/useWebSocket.tsx` (ticket exchange, reconnect policy, 4401 handling, permanent failure detection).
- Add a CI job `test-frontend` to run Vitest on every PR.

**Risk:** Low. Test-only additions, no application logic change.
**Docker required:** No.
**K8s required:** No.
**Secrets required:** No.

**Verification:**
```bash
cd frontend && npm test -- --run
```

---

## 9. Self-Review

### Production Blocker Classification

| ID | Is it really a production blocker? | Reasoning |
|---|---|---|
| **G-C1** (tag-based deploy) | **YES -- production blocker.** Mutable tag deploys mean two pushes of the same commit can produce different images. In a production incident, you cannot be certain which image is running. Requires `KUBE_CONFIG_STAGING` to test. |
| **G-C4** (no container scanning) | **YES -- production blocker.** Pushing unscanned images to a registry that feeds production deploys is a security incident waiting to happen. Can be implemented and verified purely in CI (no K8s/secrets needed). |
| **G-H3** (uid mismatch) | **YES -- production blocker, but needs verification.** If K8s `runAsUser: 1000` does not match the user in the image, the container will fail to start. This may already be broken or Kubernetes may silently remap. Verification requires a real cluster (`KUBE_CONFIG_STAGING`). |
| **G-H6** (no K8s migration Job) | **YES -- production blocker.** You cannot deploy to Kubernetes without a database migration mechanism. The Compose `migrate` service has no K8s equivalent. Requires `KUBE_CONFIG_STAGING` to test. |

### Release Quality Gap Classification

These are important for production operations but do not prevent an initial deployment:

| ID | Issue | Category |
|---|---|---|
| G-C3 | Node.js 20 EOL plan | Timeline risk (2026-04-30); not urgent today |
| G-C5 | Dependabot config | Nice to have for ongoing dependency hygiene |
| G-C6 | SBOM/provenance | Best practice; adds supply-chain transparency |
| G-C7 | CodeQL | Best practice; finds bugs, not a deploy gate |
| G-C8 | Image size regression guard | Operational visibility; no deploy impact |
| G-H2 | Bitnami version audit | Important but can be done as part of G2 |
| G-H4/G-H5 | WSS real-cluster validation | Needs staging cluster; deferred to G3 |
| G-H7 | Rollback strategy doc | Process/documentation, not code |
| G-R1-R6 | Reliability backlog | Operational; can be added incrementally post-deploy |
| G-F1/G-F2 | Frontend tests | Quality; no deploy impact |
| G-D1-G-D3 | Docker follow-up | Optimization; no deploy impact |

### What Requires K8s/Secrets (Cannot Be Done Purely in CI)

| Item | Why |
|---|---|
| G-C1 (digest deploy) | Must verify `helm upgrade` with digest works against a real cluster |
| G-H3 (uid fix verification) | Must verify pods actually start with corrected `runAsUser` |
| G-H6 (K8s migration Job) | Must test Helm hook execution on a real cluster |
| G-H4/G-H5 (WSS validation) | Must test TLS WebSocket upgrade against real Ingress Controller |
| G-R5/G-R6 (integration/smoke tests) | Must run against live staging API |

### What Can Be Verified Purely in CI

| Item | How |
|---|---|
| G-C4 (Trivy scan) | `trivy image` in CI job |
| G-C5 (Dependabot) | `.github/dependabot.yml` + GitHub-native scheduling |
| G-C6 (SBOM/provenance) | `provenance: true` + `sbom: true` in build-push-action |
| G-C7 (CodeQL) | `github/codeql-action` workflow |
| G-C3 (Node 22 plan) | Documentation, no cluster dependency |
| G-H1 (Helm digest field) | `helm lint` + `helm template` in CI |
| G-F1/G-F2 (frontend tests) | Vitest in CI, no Docker/K8s dependency |
| G-D1 (requirements split) | `pip install -r requirements.in` in Docker build |
| G-D2 (libssl1.1 removal) | `ldd` verification in Docker build step |
| G-D3 (cold-start metrics) | Compose `up -d --wait` timing in CI |

### What This Assessment Does NOT Cover

- Keycloak realm/SSO configuration for production (out of scope)
- SSL certificate provisioning for production domain
- DNS configuration
- Kubernetes cluster provisioning / node pool sizing
- Ollama/GPU node configuration
- Production secret management (Vault, Sealed Secrets)
- Backup/DR strategy validation against production data volume
- GDPR compliance audit for production data
- Load testing / capacity planning
- SLO/SLA definition

These are operational concerns that belong to a separate production readiness checklist, not the hardening closeout.
