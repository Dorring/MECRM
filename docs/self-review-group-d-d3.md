# D3 Self-Review — Chaos Cleanup

**Date:** 2026-07-10
**Branch:** `codex/group-d-d3-chaos-cleanup` (deleted — squash-merged as #13)
**Baseline:** `main@c13a523` (D2 stabilized)
**Status:** ✅ **MERGED / STABILIZED**
**Merge commit:** `main@2014732` (squash-merge PR #13)
**Tag:** `hardening-group-d-d3-stabilized`
**CI:** All checks passed + Chaos Tests (Isolated) manual dispatch passed

---

## 1. Problem Summary

The chaos test pipeline had three reliability issues:

| # | Issue | Root cause | Impact |
|---|-------|-----------|--------|
| 1 | `schedule` trigger `0 3 * * *` produced silent exit 2 | Inline raw-SQL migration runner in `chaos-migrations` skipped Prisma entirely. Prisma-managed tables (`users`, `roles`, etc.) didn't exist; `02-rls-policies.sql` hit `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on missing tables. | Nightly scheduled runs always failed with no diagnostics surfaced. |
| 2 | No diagnostics on failure | Workflow had no `docker compose ps` or service log steps. | Root cause invisible from Actions UI. |
| 3 | `agents→opa: service_started` | D1 fixed this in main compose but chaos compose was missed. | Agents could start before OPA policies compiled. |

## 2. Changes

### 2.1 — `docker-compose.chaos.yml`: Unified migration runner

**Old `chaos-migrations`:**
```yaml
image: postgres:16-alpine        # no Node, no Prisma, no npx
entrypoint: ["/bin/sh", "-lc"]
command: inline for-loop over /migrations/*.sql (psql only, skips 02 then applies last)
```

**New `chaos-migrations` (aligned with main compose `migrate` service):**
```yaml
build:                            # database/Dockerfile.migrate (Node + pinned Prisma + postgresql-client)
  context: .
  dockerfile: database/Dockerfile.migrate
command: ["bash", "/scripts/migrate.sh"]
volumes:
  - ./database/migrations:/database/migrations:ro
  - ./scripts/migrate.sh:/scripts/migrate.sh:ro
environment:
  - GATEWAY_DIR=/app
  # ... POSTGRES_*, DATABASE_URL
```

Key design points:
- `database/Dockerfile.migrate` is a dedicated migration runner: `node:20-bullseye-slim` + `postgresql-client` + `npm ci` (pinned Prisma CLI). It is lighter than the full gateway image and does not bundle the app build.
- `migrate.sh` derives `REPO_ROOT` from its own path: `/scripts/migrate.sh` → `REPO_ROOT=/`. No `REPO_ROOT` env override is needed.
- No `env_file: .env` — `migrate.sh` loads `.env` internally via `source "${REPO_ROOT}/.env"`.

### 2.2 — `docker-compose.chaos.yml`: OPA dependency condition

`agents→opa` changed from `service_started` to `service_healthy` to match D1 fix in main compose.

### 2.3 — `.github/workflows/chaos-tests.yml`: Schedule removal + diagnostics

**Removed:**
- `schedule: 0 3 * * *` trigger — no more silent nightly failures
- `github.ref == 'refs/heads/main'` guard — chaos is now manual-only

**Retained:**
- `workflow_dispatch: {}` — still triggerable on demand

**Added:**
- Always-on diagnostics: `docker compose ps`, `chaos-migrations logs`, `postgres logs`
- Failure-only diagnostics: `agents logs`, `replay-service logs`, `kafka logs`

### 2.4 — `docs/preflight-group-d.md`: Stale D2 status fix

D2 section header changed from "in progress on codex/group-d-d2-image-pinning" to "merged/stabilized as #12, tag hardening-group-d-d2-stabilized".

## 3. Not Changed (Out of Scope for D3)

| Item | Reason |
|------|--------|
| Kafka topic auto-create in chaos compose | Main compose uses `kafka-init` (one-shot container via `confluentinc/cp-kafka:7.5.0` + `scripts/kafka-init.sh`). Chaos compose has no kafka-init equivalent. Adding one would require the same image + script mount which adds a step that isn't needed for chaos tests (chaos test envs set `auto.create.topics.enable=true` on Kafka). Not a D3 concern. |
| OPA version | Already `0.70.0` from D2. Confirmed with regression test. |
| D2 image pins | Chaos observability images already pinned in D2. No changes. |
| Helm / CI digest | Out of scope for all of Group D. |
| Dockerfiles / image size | Group F. |
| chaos-migrations → agents/replay `service_started` on kafka/redis/postgres | These already use `service_healthy` (inherited from D1 era chaos compose). No change needed. |

## 4. Verification Results

| Check | Result |
|-------|--------|
| `pytest tests/infra/test_group_d_chaos_cleanup.py -v` | ✅ **13 passed** |
| `pytest tests/infra/test_group_d_image_pinning.py -v` | ✅ 10 passed (no D2 regression) |
| `pytest tests/infra/test_group_d_health_dependencies.py -v` | ✅ 21 passed (no D1 regression) |
| `docker compose -f docker-compose.chaos.yml config --quiet` | ✅ Warning only (obsolete `version`) |
| `docker compose --profile smoke-test config --quiet` | ✅ No errors |
| Chaos workflow trigger audit | ✅ No `schedule`, `workflow_dispatch` retained |
| Chaos compose OPA | ✅ `0.70.0` |
| Chaos compose agents→opa | ✅ `service_healthy` |
| `docker compose -f docker-compose.chaos.yml up -d --build` | ⚠️ **Skipped** — no Docker daemon available |
| `docker compose -f docker-compose.chaos.yml run chaos-migrations` | ⚠️ **Skipped** — no Docker |

### Why Docker verification is skipped

Docker Desktop is not running on this host. `docker compose config --quiet` confirms YAML is structurally valid but does not exercise the build or the migration runner. The unified `migrate.sh` is the same script verified in CI via `docker compose --profile migrate run migrate` on every PR push — but chaos-specific validation (build + run on chaos infra) is deferred to PR CI / manual `workflow_dispatch` on the CI runner.

## 5. Diff Summary

```
 docker-compose.chaos.yml                     | 41 ++++++++++++--------
 .github/workflows/chaos-tests.yml            | 51 ++++++++++++++++++++------
 tests/infra/test_group_d_chaos_cleanup.py    | 196 ++++++++++++++++++++ (new)
 docs/preflight-group-d.md                    |   2 +-
 docs/self-review-group-d-d3.md               | 100+ lines (new)
 5 files changed
```
