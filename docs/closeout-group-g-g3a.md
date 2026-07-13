# Group G G3a Closeout Evidence

**Date:** 2026-07-14
**PR:** #43 -- feat(helm): add G3a Kubernetes migration job
**Status:** MERGED / STABILIZED
**Tag:** `hardening-group-g-g3a-stabilized` -> `66eb85b`

---

## Scope

G3a adds a Helm pre-upgrade Kubernetes migration Job, resolving the G-H6 production blocker.

### Delivered

1. `deploy/helm/enterprise-crm/templates/migration-job.yaml` -- new Job template
   - Hook: `pre-install,pre-upgrade` with `hook-weight: "-5"`
   - Delete policy: `before-hook-creation,hook-succeeded`
   - `restartPolicy: Never`, `backoffLimit: 2`, `activeDeadlineSeconds: 600`
   - Image: `database/Dockerfile.migrate` (self-contained, digest-pinned)
   - Env: `DATABASE_URL` (secretKeyRef) + `GATEWAY_DIR=/app` only
   - Security: `runAsNonRoot: true`, `runAsUser: 1000`

2. `database/Dockerfile.migrate` -- enhanced from docker-compose-only to K8s self-contained
   - COPYs `gateway/package*.json`, `gateway/prisma/`, `database/migrations/`, `scripts/migrate.sh`
   - Runs `apt-get upgrade -y` for OS CVE patching
   - Build context: repo root

3. CI digest deploy chain includes migrate:
   - Build & Push matrix: `migrate` (dockerfile: database/Dockerfile.migrate, context: .)
   - PR Security Scan matrix: `migrate`
   - aggregate-digests: `REQUIRED_PROJECTS="gateway frontend agents migrate"`
   - deploy-staging: reads `.migrate.image`/`.migrate.digest`, passes `--set-string migration.image.{repository,digest}`
   - deploy-production: same pattern

4. SBOM ordering fix:
   - SBOM generated as Pass 3 in Trivy step (before Pass 4 CRITICAL gate)
   - Both main build and PR security-scan jobs
   - `sbom-migrate.cdx.json` always produced even when gate fails

5. CVE fixes:
   - `apt-get upgrade -y` in Dockerfile.migrate (Debian OS CVEs)
   - handlebars 4.7.8 -> 4.7.9 in gateway/package-lock.json

### Deferred to G3b

- Real staging cluster validation (`KUBE_CONFIG_STAGING`)
- WebSocket WSS ingress validation
- Integration/smoke test K8s execution

---

## CI Evidence

| Artifact | Status |
|---|---|
| Build & Push (gateway, frontend, agents, migrate) | Green |
| Security Scan (gateway, frontend, agents, migrate) | Green |
| Trivy CRITICAL gate (all 4 images) | Passed |
| Helm Lint & Template (tag + digest mode) | Green |
| Tenant Isolation Proof | Green |
| CodeQL Analysis | Green |
| image-metrics-{project} | 4 artifacts produced |
| digest-{project} | 4 artifacts produced (gateway, frontend, agents, migrate) |
| trivy-results-{project}.sarif | 4 artifacts produced |
| trivy-results-{project}.json | 4 artifacts produced |
| sbom-{project}.cdx.json | 4 artifacts produced |
| aggregate-digests -> digest-map.json | All 4 projects assembled |
| Deploy to Staging | Skipped (no KUBE_CONFIG_STAGING) |
| Deploy to Production | Skipped (no KUBE_CONFIG_PRODUCTION) |
| PR #43 squash-merge | `66eb85b` on main |

---

## Tests

```
392 passed, 10 skipped, 10 subtests passed
```

### G3a-specific suites

- `tests/infra/test_group_g_g3_migration_job.py` -- 57 tests (G3-01 through G3-22)
- `tests/infra/test_group_g_g2_supply_chain.py` -- 24 tests (includes SBOM-before-gate assertions)
- `tests/infra/test_group_g_g2_codeql_dependabot.py` -- 22 tests
- `tests/infra/test_group_g_digest_pinning.py` -- 39 tests
- `tests/infra/test_group_e_helm_governance.py` -- passing

---

## Remaining Blocker

- **G3b**: Staging real-cluster validation requires `KUBE_CONFIG_STAGING`, `STAGING_API_URL`, `TEST_USER_EMAIL`, `TEST_USER_PASSWORD` secrets.
