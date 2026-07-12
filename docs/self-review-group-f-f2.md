# Group F F2 Self-Review

**Date:** 2026-07-12
**Branch:** codex/group-f-f1-baseline-record
**Baseline:** ffef4fb (baseline collected from CI runner)
**Status:** STABILIZED
**Merge commit:** `main@d6d5568` (PR #17 squash-merge)
**Tag:** `hardening-group-f-f2-stabilized`
**CI:** All checks passed on PR #17 (helm-lint, build, compose, smoke, ws-proxy-test)

## F2 Scope Checklist

| Step | Description | Status |
|---|---|---|
| F2a | agents/.dockerignore: add .env, .env.* (F-B2 secret leak fix) | DONE |
| F2b | agents/.dockerignore: add tests/, .mypy_cache/, .ruff_cache/, test_output.txt, conftest.py, pytest.ini, scripts/ (F-S4) | DONE |
| F2c | Root .dockerignore: add dist/, docs/, tests/, assets/, **/.mypy_cache/, **/.ruff_cache/, Dockerfile*, docker-compose*.yml (F-S5) | DONE |
| F2d | Narrow migrate build context: . to ./gateway in both compose files; adjust Dockerfile COPY paths (F-S3) | DONE |
| F2e | frontend/Dockerfile: add HEALTHCHECK with Node fetch (F-S1) | DONE |
| F2f | All 4 Dockerfiles: add BuildKit cache mounts (--mount=type=cache for npm/pip) + # syntax=docker/dockerfile:1.7 (F-S2) | DONE |
| F2g | gateway/Dockerfile: runner stage uses USER node with chown (F-S7) | DONE |
| F-D4 | frontend/Dockerfile: combine ENV PORT+HOSTNAME, combine addgroup+adduser in one RUN | DONE |
| F-B1 | Agents multi-stage conversion | NOT DONE (F4 scope) |

## Items Explicitly Not Done

- **F-B1 (agents multi-stage):** This is the Group F exit blocker but belongs to F4. The preflight explicitly states: "implementation MUST happen in F4, AFTER F1 baseline metrics are captured." The single-stage agents image with build-essential retained is still present and will be addressed in F4.
- **F-B2 (gateway non-root for builder):** Only the runner stage was changed. Compilers (builder stage) run as root -- standard practice.
- **F-D1 (libssl1.1 removal):** Deferred to F4b. Needs ldd verification.
- **Digest pinning (E3/Phase 4):** Out of scope per Group F charter.
- **requirements.txt split (F-D7):** Recorded as follow-on optimization point for F4b.

## Baseline Status

- Baseline file `docs/baseline-group-f.md` is INTACT and not overwritten.
- Status: COLLECTED from CI runner (host: runnervm5mmn9, Docker 28.0.4).
- Key metrics before F2 changes:
  - gateway: 1238.5 MB, 14 layers, 107.4s build
  - frontend: 207.2 MB, 9 layers, 47.5s build
  - agents: 737.5 MB, 9 layers, 45.8s build
  - migrate: 596.1 MB, 10 layers, 82.8s build
  - migrate context (raw tar): 60.8 MB
- F2 changes are additive/context-shrinking; they should either reduce or not change image sizes.

## Risk Assessment Per Change

| Change | Risk | Rollback | Verified By |
|---|---|---|---|
| agents/.dockerignore: add .env, .env.* | Zero -- context-only filter | Remove lines from .dockerignore | Static test (F-B2) |
| agents/.dockerignore: add tests/, caches, scripts/ | Low -- these files should not be imported at runtime via COPY; if any script is imported, `scripts/` exclusion may cause missing file; recovery: add `!scripts/<file>` negation | Remove `scripts/` line from .dockerignore | Static test (F-S4) |
| root/.dockerignore: add dist/, docs/, tests/, assets/, caches | Zero -- these paths are not COPY'd by any Dockerfile using root context | Remove lines | Static test (F-S5) |
| gateway/.dockerignore keeps jest config files | Low -- keeps tiny test config files in builder context; production runner image is unaffected because it copies only package/prisma/dist | Re-add ignore only if test-gateway no longer runs npm test inside Docker builder target | Static test: gateway Jest configs not excluded |
| Migrate context narrowing (. to ./gateway, COPY path adjustments) | Low-Medium -- if COPY paths are wrong, migrate image won't build; if context misses files, `docker compose build migrate` fails | Revert context to `.`, revert COPY paths to `gateway/` prefix | Static test (F-S3); CI docker compose build |
| Frontend HEALTHCHECK | Low -- adds HTTP request; if /api/health is missing, container shows unhealthy but doesn't crash | Remove HEALTHCHECK | Static test (F-S1); CI compose up --wait |
| BuildKit cache mounts + # syntax= | Low -- # syntax=docker/dockerfile:1.7 requires BuildKit; CI already uses buildx v3; non-BuildKit builds will fail (documented requirement) | Remove # syntax= line and --mount= flags | Static test (F-S2) |
| Gateway USER node + chown | Medium -- permission errors if /app/dist or node_modules are not readable; HEALTHCHECK catches immediately | Remove chown and USER lines | Static test (F-S7); CI compose up --wait gateway |
| Frontend ENV combine + addgroup/adduser combine | Zero -- cosmetic, saves one layer each | Revert to separate lines | Static test (F-D4) |

## Build Context / .dockerignore Mis-Exclusion Risk

| Path Excluded | Risk of Breaking Build | Mitigation |
|---|---|---|
| `agents/scripts/` (F2b) | Low risk -- these are utility scripts, not imported as Python modules by the orchestrator. If `scripts/` contains runtime-needed files, the build will fail with ModuleNotFoundError. | Can add `!scripts/<needed_file>` negation. |
| `agents/tests/` (F2b) | Zero risk -- tests are never imported at runtime. |
| `root docs/` (F2c) | Zero risk -- no Dockerfile COPYs docs/. |
| `root dist/` (F2c) | Zero risk -- migrate Dockerfile no longer references this path. |
| `root tests/` (F2c) | Zero risk -- no Dockerfile COPYs tests/ from root context. |
| `root assets/` (F2c) | Zero risk -- no Dockerfile COPYs assets/. |
| `gateway/jest.config.js` and `gateway/jest.durability.config.js` | Must remain included -- Dockerized `test-gateway` builds the `builder` target and runs `npm test`; excluding Jest config makes Jest parse `.ts` tests as plain JavaScript. | Guarded by F2 regression test. |

## Local Verification

| Verification | Result |
|---|---|
| git diff --check | PASS |
| pytest tests/infra/test_group_f_image_optimization.py -v | 29 passed |
| pytest tests/infra -v | 163 passed, 11 skipped |
| docker compose config --quiet | NOT RUN (local Docker CLI lacks Compose v2 / config access denied) |
| docker compose -f docker-compose.chaos.yml config --quiet | NOT RUN (local Docker CLI lacks Compose v2 / config access denied) |
| docker compose build gateway frontend agents migrate | NOT RUN (Docker unavailable) |
| MojiBake check (all changed files) | PASS -- no Unicode chars found in Dockerfiles or dockerignore files |

## Unverified Items (Require Docker)

The following can only be verified on a Docker-capable host or CI runner:
- `docker compose config --quiet` (both main and chaos compose)
- `docker compose build gateway frontend agents migrate`
- `docker compose --profile migrate run --rm migrate`
- Actual image size before/after comparison against baseline

These were validated by the PR #17 CI pipeline (helm-lint, build, compose, smoke, ws-proxy-test all passed).

## Self-Review Conclusion

- All F2 changes are static/config-only -- no business logic modified, no application source code touched.
- All Should Fix items from the preflight are addressed except F-B1 (agents multi-stage, reserved for F4).
- Baseline is preserved and not overwritten by F2 changes.
- Regression tests cover all F2 requirements plus backward compatibility with D1/D2/D3/E tests.
- **PR #17 squash-merged.** CI passed all checks (helm-lint, build, compose, smoke, ws-proxy-test).
- **Tagged:** `hardening-group-f-f2-stabilized` -> `d6d5568`.
- **Branches cleaned:** `codex/group-f-f1-baseline-record` deleted (remote + local).
