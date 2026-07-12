# Group G G3a Self-Review — K8s Migration Job

**Date:** 2026-07-13
**Branch:** codex/group-d-d1-health-dependencies
**Status:** COMPLETED (G3a implementation; G3b staging validation pending)

---

## Scope

G3a delivers the Helm pre-upgrade migration Job (G-H6 blocker). It covers:

1. `deploy/helm/enterprise-crm/templates/migration-job.yaml` — new Job template
2. `deploy/helm/enterprise-crm/values.yaml` — migration section
3. `database/Dockerfile.migrate` — enhanced to be self-contained (no K8s volume mounts)
4. `.github/workflows/ci-cd.yml` — helm-lint digest-mode coverage for migration image
5. `docker-compose.yml` / `docker-compose.chaos.yml` — updated build context

### Out of scope (G3b)
- Real-cluster staging validation (requires `KUBE_CONFIG_STAGING`, `STAGING_API_URL`, `TEST_USER_EMAIL`, `TEST_USER_PASSWORD`)
- WebSocket WSS ingress validation
- Integration/smoke test K8s execution
- No business code changes
- No Trivy/CodeQL/Dependabot changes

---

## Design Decisions

### Migration Image: `database/Dockerfile.migrate` (not gateway)

| Factor | migrate image | gateway image |
|---|---|---|
| psql (raw SQL + advisory lock) | Yes | No |
| Prisma CLI (schema migrations) | Yes | Yes |
| Image size | ~300MB (node:20-bullseye-slim) | ~1GB (full app build) |
| Job startup time | Fast | Slow |
| Separation of concerns | Migration ≠ API server | Mixed |
| Docker-Compose parity | Same image | Different image |

**Decision**: Use `database/Dockerfile.migrate`. Enhanced from docker-compose-only (volume mounts for `/database/migrations/` and `/scripts/migrate.sh`) to self-contained (COPY both into image). Build context changed from `./gateway` to repo root so `gateway/`, `database/migrations/`, and `scripts/migrate.sh` are all available.

### Secret Management
- `DATABASE_URL` sourced exclusively from `secretKeyRef` → `migration.database.existingSecret` / `migration.database.urlKey`
- `POSTGRES_PASSWORD` also sourced from same secret (not plaintext)
- Non-sensitive env vars (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`) use `value:` (correct — they are not secrets)
- Default secret: `crm-postgresql-secret` (same as gateway/agents)

### Helm Hook Strategy
- `helm.sh/hook: pre-install,pre-upgrade` — runs before any Deployment changes
- `helm.sh/hook-weight: "-5"` — runs before other hooks (negative = early)
- `helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded` — cleanup after success, replace on re-run
- `restartPolicy: Never` — Job, not a long-running pod
- `backoffLimit: 2` — retry twice on failure
- `activeDeadlineSeconds: 600` — 10-minute timeout

### Digest Pinning
- Uses the existing `enterprise-crm.image` helper (digest preferred, tag fallback with `required()`)
- CI digest-mode helm-lint includes migration image in all 3 render steps (default, staging, production)
- `enterprise-crm/migrate@sha256:` prefix rejected (must be `ghcr.io/dorring/mecrm/migrate@sha256:`)

### Security Context
- `runAsNonRoot: true`
- `runAsUser: 1000` (node user — matches `node:20-bullseye-slim` base image)

---

## File Changes

| File | Change |
|---|---|
| `deploy/helm/enterprise-crm/templates/migration-job.yaml` | New — 69 lines |
| `deploy/helm/enterprise-crm/values.yaml` | +28 lines (migration section) |
| `database/Dockerfile.migrate` | Rewritten — self-contained (COPY gateway/, database/, scripts/) |
| `.github/workflows/ci-cd.yml` | +45/-7 lines (migration in all tag/digest-mode renders) |
| `docker-compose.yml` | Context `.` / dockerfile `database/Dockerfile.migrate` |
| `docker-compose.chaos.yml` | Context `.` / dockerfile `database/Dockerfile.migrate` |
| `tests/infra/test_group_g_g3_migration_job.py` | New — 46 tests (G3-01 through G3-16) |
| `tests/infra/test_compose_config.py` | Updated migrate context assertion |
| `tests/infra/test_group_f_image_optimization.py` | Updated 3 tests for new context/COPY paths |

---

## Verification

### Tests
```
379 passed, 10 skipped, 10 subtests passed
```

### Static Validation
- Template structure: Job kind, batch/v1 apiVersion, hook annotations, restartPolicy, secretKeyRef, image helper — all confirmed
- Values: migration.enabled=true, backoffLimit=2, activeDeadlineSeconds=600, digest="", existingSecret=crm-postgresql-secret
- Dockerfile: postgresql-client, migrate.sh, /database/migrations/ — all present
- CI: DIGEST_MG env, migrate repo/digest in all 3 digest renders, enterprise-crm/migrate rejection, tag-mode coverage

### `git diff --check`
Clean — no whitespace errors.

---

## G3b Prerequisites

Staging real-cluster validation requires these secrets:
- `KUBE_CONFIG_STAGING` — staging cluster kubeconfig
- `STAGING_API_URL` — staging API endpoint (for smoke tests)
- `TEST_USER_EMAIL` — test user for E2E auth flow
- `TEST_USER_PASSWORD` — test user password

G3b will validate:
1. Migration Job actually runs on `helm upgrade --install`
2. Job completes successfully (Prisma + raw SQL + RLS audit)
3. WebSocket `/ws` works after migration
4. Post-migration smoke tests pass
