# Group F Preflight -- Docker Image Size & Build Optimization Survey

**Date:** 2026-07-11
**Baseline:** `main@e41a3fa` (Group E fully closed)
**Status:** F1/F2/F3 COMPLETED / STABILIZED
**F2 Tag:** `hardening-group-f-f2-stabilized` -> `d6d5568` (PR #17 squash-merge)
**F3 Tag:** `hardening-group-f-f3-stabilized` -> `402a273` (PR #18 squash-merge)
**F-B1 (agents multi-stage):** PENDING, deferred to F4

## Executive Summary

The repo builds four Docker images (three application + one migration runner) via
`docker compose build`. The CI pipeline builds and pushes the three application
images to GHCR with BuildKit + GitHub Actions cache. Image sizes are not
currently tracked as CI artifacts, and several Dockerfiles carry dead weight:
single-stage agents retaining `build-essential` in runtime, missing `.env`
exclusions in `.dockerignore`, and a migration build context that includes the
entire repo. This preflight catalogs current state, classifies findings, and
proposes a phased F1->F2->F3->F4 plan.

**F1 must always come first.** No Dockerfile changes are permitted before the
baseline metrics are captured and committed to `docs/baseline-group-f.md`.

## Survey Scope

- 4 Dockerfiles: `gateway/Dockerfile`, `frontend/Dockerfile`, `agents/Dockerfile`, `database/Dockerfile.migrate`
- 4 `.dockerignore` files: root, `gateway/`, `frontend/`, `agents/`
- CI Build & Push job (`.github/workflows/ci-cd.yml`, lines 588-660)
- Build contexts as declared in `docker-compose.yml` and `docker-compose.chaos.yml`
- Out of scope: digest pinning (E3/Phase 4), registry policy, Helm release strategy, application logic changes

---

## 1. Dockerfile Inventory

### 1.1 `gateway/Dockerfile`

| Attribute | Value |
|---|---|
| Base image | `node:20-bullseye` (both stages) |
| Stages | 2 (builder -> runner) |
| Context | `./gateway` |
| `.dockerignore` | `gateway/.dockerignore` |
| Layer instructions | builder: 3 COPY, 3 RUN; runner: 3 COPY, 3 RUN |
| HEALTHCHECK | Yes: `node -e "fetch('http://localhost:4000/health')"` |
| Non-root user | No (runs as root; `node` user exists on base image) |

**Layout assessment:** The classic `COPY package*.json + npm ci` -> `COPY . . + npm run build`
layering is correct for layer caching. Prisma is generated in both builder and runner
stages (intentional -- production `node_modules` is installed fresh in runner with
`--omit=dev`, so `npx prisma generate` must re-run).

**Findings:**

| ID | Finding | Severity |
|---|---|---|
| GW-1 | No `--mount=type=cache` for npm. Local builds re-download all packages each run. CI uses `type=gha` cache and is not affected. | Minor |
| GW-2 | `openssl libssl1.1` installed via apt. `node:20-bullseye` ships `libssl3`. The `libssl1.1` package may be leftover from an older base image. | Minor |
| GW-3 | Three blank lines (lines 6-8) between `rm -rf /var/lib/apt/lists/*` and `COPY package*.json`. Cosmetic only, no build impact. | Trivial |
| GW-4 | Runs as root in production. The base image has a `node` user (uid 1000); the runner stage should switch to it after COPYs and before CMD. Requires chown on `/app` to ensure readability. | Should Fix |

### 1.2 `frontend/Dockerfile`

| Attribute | Value |
|---|---|
| Base image | `node:20-alpine` (all 3 stages) |
| Stages | 3 (deps -> builder -> runner) |
| Context | `./frontend` |
| `.dockerignore` | `frontend/.dockerignore` |
| Layer instructions | deps: 1 COPY, 1 RUN; builder: 2 COPY, 1 RUN; runner: 2 COPY, 2 RUN |
| HEALTHCHECK | **MISSING** |
| Non-root user | Yes: `nextjs` (uid 1001) |

**Layout assessment:** This is the exemplar Dockerfile in the repo. Three-stage build
with Next.js standalone output means the runner stage contains only the minimal
dependency graph, not the full `node_modules`. `USER nextjs` drops privileges.

**Findings:**

| ID | Finding | Severity |
|---|---|---|
| FE-1 | No HEALTHCHECK. Gateway and agents both have one; its absence is a gap for Compose `depends_on: service_healthy` chains. | Should Fix |
| FE-2 | No `--mount=type=cache` for npm. Same note as gateway (local builds only). | Minor |
| FE-3 | Two separate `ENV` instructions (lines 31-32). Combine to save one layer. | Trivial |
| FE-4 | `addgroup` + `adduser` on separate RUN lines (lines 18-19). Combine into one RUN. | Trivial |

### 1.3 `agents/Dockerfile`

| Attribute | Value |
|---|---|
| Base image | `python:3.11-slim` |
| Stages | **1 (single-stage) -- Group F exit blocker** |
| Context | `./agents` |
| `.dockerignore` | `agents/.dockerignore` |
| Layer instructions | 2 COPY, 2 RUN |
| HEALTHCHECK | Yes: `python -c "import httpx,sys; ..."` |
| Non-root user | No (runs as root) |

**Layout assessment:** Single-stage means `build-essential` (gcc, g++, make, binutils,
libc-dev, linux-headers-amd64 -- typically 200-400MB) are installed for pip package
compilation and then **retained in the final runtime image**. There is no
`apt-get purge -y build-essential && apt-get autoremove -y` step. `COPY . .` at
line 15 copies everything not excluded by `.dockerignore`, including `tests/`,
`scripts/`, `conftest.py`, `pytest.ini`, etc.

**This is a Group F exit blocker (F-B1).** The Group F branch MUST NOT be merged
until the agents image is converted to multi-stage. However, implementation MUST
happen AFTER F1 baseline metrics are captured, so there is a before/after comparison.

**Findings:**

| ID | Finding | Severity |
|---|---|---|
| AG-1 | Single-stage build. `build-essential` retained in runtime (est. 200-400MB). `COPY . .` includes `tests/`, `scripts/`, and other non-runtime files in the image. This is the largest size optimization opportunity in the repo. | **Blocker** (Group F exit gate; implement in F4 after F1 baseline) |
| AG-2 | `agents/.dockerignore` missing `.env` and `.env.*`. Secrets could leak into the image if `.env` files are present in the build context. | **Blocker** (must fix in F2 regardless of phase ordering) |
| AG-3 | No `--mount=type=cache` for pip. Local builds re-download wheels each run. | Minor |
| AG-4 | `agents/.dockerignore` missing `tests/`, `.mypy_cache/`, `.ruff_cache/`, `test_output.txt`, `conftest.py`, `pytest.ini`. These inflate the build context and (with single-stage `COPY . .`) the final image. | Should Fix |
| AG-5 | Runs as root in production. | Should Fix |

### 1.4 `database/Dockerfile.migrate`

| Attribute | Value |
|---|---|
| Base image | `node:20-bullseye-slim` |
| Stages | 1 (single-stage, acceptable for a one-shot tool image) |
| Context | `.` (repo root) -- **largest effective context** |
| `.dockerignore` | Root `.dockerignore` only (no `database/.dockerignore`) |
| Layer instructions | 1 RUN (apt), 2 COPY, 1 RUN (npm ci) |
| HEALTHCHECK | N/A (one-shot, brief-lived) |
| Non-root user | N/A (one-shot) |

**Layout assessment:** The `slim` variant is correct for a migration tool.
`postgresql-client` is installed for `psql` in raw SQL migrations. The comments
correctly explain why `--omit=dev` cannot be used (Prisma CLI must be version-pinned
from `package-lock.json`). Single-stage is acceptable for a one-shot tool.

The real issue is the **build context is the entire repo root** (declared in
`docker-compose.yml` as `context: .`). Even with the root `.dockerignore`, the
context still includes `agents/src/`, `docs/`, `scripts/`, `schemas/`, `assets/` --
none of which the migrate Dockerfile COPYs.

**Findings:**

| ID | Finding | Severity |
|---|---|---|
| MG-1 | Build context is the entire repo root. Effective context ~145MB for a Dockerfile that only COPYs `gateway/package*.json` and `gateway/prisma`. | Should Fix |
| MG-2 | No `--mount=type=cache` for npm. | Minor |

---

## 2. `.dockerignore` Coverage Audit

### 2.1 Root `.dockerignore`

**Patterns (18 rules):**
```
.git/  .github/  .agents/  .claude/  .codex/
**/node_modules/  **/.next/  **/coverage/  **/__pycache__/
**/.pytest_cache/  **/*.py[cod]  **/*.tsbuildinfo
.env  .env.*  logs/  tmp/  reports/  backups/
```

**Gaps:**

| Missing pattern | Impact |
|---|---|
| `dist/` (or `**/dist/`) | Gateway `dist/` build output sent to migrate context |
| `docs/` | ~400KB of markdown sent to migrate context |
| `tests/` (or `**/tests/`) | Test directories across all services |
| `**/.mypy_cache/` | Python mypy cache |
| `**/.ruff_cache/` | Python ruff cache |
| `Dockerfile*` | Self-referential (tiny, best practice) |
| `docker-compose*.yml` | Compose files (tiny, best practice) |
| `assets/` | Root-level assets directory |

### 2.2 `gateway/.dockerignore`

**Patterns (9 rules):** `node_modules/ dist/ coverage/ logs/ *.log *.tsbuildinfo .env .env.* .nyc_output/`

**Gaps:**

| Missing pattern | Impact |
|---|---|
| `tests/` | Gateway test files sent to all gateway-family builds |
| `jest.config.js`, `jest.durability.config.js` | Test runner configs |
| `Dockerfile` | Self-referential (tiny) |

### 2.3 `frontend/.dockerignore`

**Patterns (9 rules):** `node_modules/ .next/ coverage/ dist/ logs/ *.log *.tsbuildinfo .env .env.*`

Well-covered. Minor gap: `Dockerfile` not excluded (tiny, best practice).

### 2.4 `agents/.dockerignore`

**Patterns (11 rules):** `__pycache__/ *.py[cod] .pytest_cache/ .coverage htmlcov/ .venv/ venv/ logs/ *.log dist/ build/`

**Critical gaps:**

| Missing pattern | Impact |
|---|---|
| **`.env` `.env.*`** | **SECRETS LEAK RISK.** `.env` files could be baked into the image. |
| `tests/` | Test files copied into production image via `COPY . .` |
| `test_output.txt` | Log file copied into context |
| `conftest.py` `pytest.ini` | Test support files |
| `.mypy_cache/` | Python type-checking cache |
| `.ruff_cache/` | Python lint cache |
| `scripts/` | Agent utility scripts (may or may not be runtime-needed) |

---

## 3. CI Build & Push Audit

**Workflow:** `.github/workflows/ci-cd.yml`, job `build` (lines 588-660)

### 3.1 Current State

| Aspect | Status |
|---|---|
| Build tool | `docker/setup-buildx-action@v3` with `driver: docker-container` |
| Builder | `docker/build-push-action@v5` |
| Cache source | `type=gha` (GitHub Actions cache backend) |
| Cache target | `type=gha,mode=max` (caches all layers, not just final stage) |
| Push | `true` (on main push only) |
| Matrix | `gateway`, `frontend`, `agents` (3 parallel builds) |
| Metadata | `docker/metadata-action@v5` -- tags: `sha`, `branch`, `semver` |
| Digest export | Yes: `steps.build-push.outputs.digest` exported to `GITHUB_OUTPUT` |
| Migrate image | Not built in CI (only built locally via Compose) |

### 3.2 Gaps

| ID | Finding | Severity |
|---|---|---|
| CI-1 | No image size, layer count, or build duration recorded as CI artifacts. Without a baseline, size regressions are invisible. | Should Fix |
| CI-2 | Migrate image not built in CI. Not a blocker (it's one-shot), but an inconsistency. | Defer |
| CI-3 | Local builds vs CI builds use different cache backends. CI uses `type=gha`; local `docker compose build` uses local layer cache. No `--mount=type=cache` for npm/pip in Dockerfiles, so local builds are slower than they could be. | Minor |
| CI-4 | Deploy uses mutable `${{ github.sha }}` tags instead of digests. Already tracked as E3 (Phase 4). | Out of scope |

---

## 4. Build Context Size Estimates

Measured on Windows 11 host. The `.dockerignore` files exclude some directories from
the actual Docker daemon context; the "effective context" column is what Docker
actually receives after `.dockerignore` filtering.

| Service | Context dir | Raw dir size | Key excludes (via `.dockerignore`) | Effective context (est.) |
|---|---|---|---|---|
| `frontend` | `./frontend` | ~807K | `node_modules/`, `.next/` | ~700K |
| `gateway` | `./gateway` | ~6.1M | `node_modules/`, `dist/` | ~5.5M |
| `agents` | `./agents` | ~108M | `__pycache__/`, `.venv/`, `build/`, `dist/` | ~105M |
| `migrate` | `.` (root) | ~149M | `.git/`, `**/node_modules/`, `**/.next/`, pycache | ~145M |

The migrate context at ~145MB effective is dominated by `agents/src/` (~108M). Since
`database/Dockerfile.migrate` only COPYs files under `gateway/`, the rest of the
repo in the context is pure waste.

---

## 5. Findings Summary

### Blocker

| ID | Finding | Impact | Phase |
|---|---|---|---|
| **F-B1** | `agents/Dockerfile` is single-stage. `build-essential` (gcc, g++, make, binutils, dev headers) retained in runtime image. `COPY . .` includes `tests/`, `scripts/`, `conftest.py`, `pytest.ini`. Estimated waste: 200-400MB build tools + test files. | Production agents image is significantly larger than necessary. This is a **Group F exit blocker**: the branch must not merge until multi-stage conversion is done. However, implementation MUST happen in F4, AFTER F1 baseline metrics are captured, so there is a before/after comparison. | F4 (after F1 baseline) |
| **F-B2** | `agents/.dockerignore` missing `.env` and `.env.*`. | Secrets (.env files) could be baked into the Docker image if present in the build context. | F2 (zero risk, fix immediately after F1) |

### Should Fix

| ID | Finding | Phase |
|---|---|---|
| **F-S1** | `frontend/Dockerfile` has no HEALTHCHECK. | F2 |
| **F-S2** | No `--mount=type=cache` for npm/pip in any Dockerfile. Local builds slower than necessary. | F2 |
| **F-S3** | `database/Dockerfile.migrate` build context is repo root (`.`). ~145MB context for a Dockerfile that only needs `gateway/package*.json` and `gateway/prisma`. | F2 |
| **F-S4** | `agents/.dockerignore` missing `tests/`, `.mypy_cache/`, `.ruff_cache/`, `test_output.txt`, `conftest.py`, `pytest.ini`. | F2 |
| **F-S5** | Root `.dockerignore` missing `dist/`, `docs/`, `tests/`, `**/.mypy_cache/`, `**/.ruff_cache/`, `assets/`. | F2 |
| **F-S6** | CI `build` job does not record image size, layer count, or build duration as artifacts. | F3 |
| **F-S7** | `gateway/Dockerfile` runner stage runs as root. | F2 |
| **F-S8** | `agents/Dockerfile` runs as root. | F4 (combined with multi-stage conversion) |

### Defer

| ID | Finding | Reason for deferral |
|---|---|---|
| F-D1 | `gateway/Dockerfile` installs `libssl1.1`. `node:20-bullseye` ships `libssl3`. | Needs verification that the app does not link against libssl1.1. |
| F-D2 | `npx prisma generate` runs in both builder and runner stages. | Intentional -- runner has fresh production `node_modules`, so the Prisma client must be regenerated. |
| F-D3 | `database/Dockerfile.migrate` not built/pushed in CI. | Migration is a one-shot tool; pushing it to a registry only matters if there is a Kubernetes migration Job. |
| F-D4 | `ENV` combination (FE-3) and `addgroup`+`adduser` combination (FE-4) in frontend Dockerfile. | Cosmetic -- saves 1-2 layers, negligible impact. Roll into next edit pass. |
| F-D5 | Gateway Dockerfile three blank lines (GW-3). | Cosmetic only. |
| F-D6 | No cold-start / health-ready time measurement in CI. | Requires a running Compose stack; better suited for a Phase 4/5 performance lab. |
| F-D7 | `requirements.txt` includes `pytest`, `pytest-asyncio`, etc. (test dependencies). | Splitting requirements into `requirements.in` and `requirements-dev.in` is a larger dependency-management change. Recorded as a follow-on optimization point. |

---

## 6. Phased Implementation Plan

### F1 -- Baseline Metrics Collection

**Goal:** Establish a repeatable, scripted measurement protocol. Capture current
numbers before ANY Dockerfile changes. No code changes in this phase.

**Actions:**
1. Create `scripts/collect-image-metrics.sh` (Linux/CI) that:
   - Runs `docker compose build --no-cache` for each service (or all at once)
   - Records build duration with `time`
   - Captures build context size by archiving context to `/dev/null` and measuring
   - For each image, runs `docker image inspect` and extracts: `.Size`, layer count (`len .RootFS.Layers`), creation timestamp
   - For each image, runs `docker history --no-trunc --human <image>` and saves to file
   - Optionally: runs `docker compose up -d --wait` and records `docker compose ps` health-ready timestamps
   - Writes all data to `docs/baseline-group-f.md` as a structured markdown table
2. Optionally create `scripts/collect-image-metrics.ps1` for Windows developer workstations (nice-to-have; the `.sh` script is the authoritative one for CI).
3. Run the script locally or on a CI runner. Commit `docs/baseline-group-f.md` and the script(s).
4. The baseline report MUST include these columns per image:
   - Image name, tag, image ID
   - Uncompressed size (MB)
   - Layer count
   - Full `docker history` output
   - Build context size (MB)
   - Build duration (seconds)
   - Optional: cold-start / health-ready time

**Deliverable:** `scripts/collect-image-metrics.sh`, `docs/baseline-group-f.md`.

**Risk:** None (read-only measurements).

**Exit criteria:** Baseline report committed. All subsequent phases compare against it.

---

### F2 -- Low-Risk Dockerfile / Context Cleanup

**Goal:** Fix the Blockers and Should Fix items that have no impact on application
behavior. Every change in F2 must be independently verifiable via
`docker compose up --wait` and existing test suites.

**Actions (ordered by risk, lowest first):**

#### F2a -- `agents/.dockerignore` secret leak fix (F-B2)

Change `agents/.dockerignore`: add `.env` and `.env.*`.

Risk: zero. Build context only; no image content change.

#### F2b -- `agents/.dockerignore` test/cache exclusions (F-S4)

Change `agents/.dockerignore`: add `tests/`, `.mypy_cache/`, `.ruff_cache/`,
`test_output.txt`, `conftest.py`, `pytest.ini`, `scripts/`, `.env`, `.env.*`.

Note: `scripts/` exclusion is conservative. If any script under `scripts/` is
imported at runtime, it can be re-included via an explicit `!scripts/runtime_needed.py`
negation. Default to excluding the directory and restoring only what is needed.

Risk: zero (context-only; `COPY . .` will pick up fewer files, but the single-stage
image will still include whatever `.dockerignore` passes through -- the real fix
for image content is the multi-stage conversion in F4).

#### F2c -- Root `.dockerignore` gap fill (F-S5)

Change root `.dockerignore`: add `dist/`, `docs/`, `tests/`, `**/.mypy_cache/`,
`**/.ruff_cache/`, `assets/`, `Dockerfile*`, `docker-compose*.yml`.

Risk: zero. Only affects the migrate build context, and `database/Dockerfile.migrate`
COPYs none of these paths.

#### F2d -- Migrate context narrowing (F-S3)

**Precise plan:**

1. **`docker-compose.yml`** (lines 517-519) -- change migrate service build section FROM:
   ```yaml
   build:
     context: .
     dockerfile: database/Dockerfile.migrate
   ```
   TO:
   ```yaml
   build:
     context: ./gateway
     dockerfile: ../database/Dockerfile.migrate
   ```

2. **`docker-compose.chaos.yml`** (`chaos-migrations` service) -- same change.

3. **`database/Dockerfile.migrate`** -- change COPY paths FROM:
   ```dockerfile
   COPY gateway/package*.json ./
   COPY gateway/prisma ./prisma
   ```
   TO:
   ```dockerfile
   COPY package*.json ./
   COPY prisma ./prisma
   ```

4. **Runtime volumes are UNCHANGED.** `scripts/migrate.sh` and `database/migrations`
   remain mounted as read-only volumes at `/scripts/migrate.sh` and
   `/database/migrations` respectively. These are runtime mounts, not build-time
   COPYs, so they are unaffected by the context change.

5. **Verification:**
   ```bash
   docker compose --profile migrate run --rm migrate
   ```

Expected outcome: migrate build context drops from ~145MB to ~5.5MB (the
`gateway/` directory after `.dockerignore` filtering). Build time should improve
proportionally.

Risk: low. The path changes are mechanical. The Dockerfile's own comments already
note it only needs `gateway/package*.json` and `gateway/prisma`. Runtime volumes
are unchanged.

#### F2e -- Frontend HEALTHCHECK (F-S1)

Add to `frontend/Dockerfile` runner stage (before CMD, after EXPOSE):
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://localhost:3000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
```

Uses Node built-in `fetch` (available in Node 20), not a system tool like `wget`
or `curl`. This is consistent with the gateway HEALTHCHECK pattern and avoids
dependency on Alpine's package set.

Risk: low. Adds a health endpoint call identical in pattern to the gateway
HEALTHCHECK that already works.

#### F2f -- BuildKit cache mounts (F-S2)

**IMPORTANT:** Each Dockerfile that uses `RUN --mount=type=cache` MUST begin with:
```dockerfile
# syntax=docker/dockerfile:1.7
```

This directive enables the `--mount=type=cache` syntax. Without it, older
Docker/BuildKit versions will reject the Dockerfile. The `# syntax=` line is
backward-compatible: BuildKit >= 0.16 (Docker 23+) supports it; legacy
(non-BuildKit) Docker daemons will fail, but the project already requires
BuildKit (CI uses `docker/setup-buildx-action@v3`).

**Changes:**

- `gateway/Dockerfile` (builder `npm ci` and runner `npm ci`): add
  `--mount=type=cache,target=/root/.npm` to each `RUN npm ci` line.
- `frontend/Dockerfile` (deps stage `npm ci`): add
  `--mount=type=cache,target=/root/.npm` to `RUN npm ci`.
- `agents/Dockerfile` (`pip install`): add
  `--mount=type=cache,target=/root/.cache/pip` to `RUN pip install`.
- `database/Dockerfile.migrate` (`npm ci`): add
  `--mount=type=cache,target=/root/.npm` to `RUN npm ci`.

Example after change:
```dockerfile
# syntax=docker/dockerfile:1.7
FROM node:20-bullseye AS builder
...
RUN --mount=type=cache,target=/root/.npm npm ci
```

Risk: low. The cache mount is additive metadata -- it does not change the image
content, only speeds up repeated local builds. CI already uses `type=gha` cache
and will ignore the mount (or benefit from it on cache-miss rebuilds).

#### F2g -- Non-root user for gateway (F-S7)

Change `gateway/Dockerfile` runner stage (after the last COPY, before CMD):
```dockerfile
RUN chown -R node:node /app
USER node
```

The `node` user (uid 1000) is pre-created in `node:20-bullseye`. The `chown`
ensures `/app/dist` and `/app/node_modules` (including the Prisma client
generated there) are readable by `node`. If `chown` adds measurable build time,
scope it to only the paths that need write access at runtime (the gateway app
should not need to write to `/app` in production; `chown` may be omitted if
read-only access is sufficient -- verify with `docker compose up --wait`).

**Verification:** `docker compose up -d --wait gateway` must show gateway healthy.
If HEALTHCHECK fails after the USER switch, investigate permission errors on
`/app/dist`, `/app/node_modules/.prisma`, or log directories.

Risk: low-medium. The frontend already runs as non-root with a similar pattern.
The gateway HEALTHCHECK provides immediate feedback on permission issues.

#### F2 Verification

After all F2 changes:
```bash
docker compose build --no-cache
docker compose up -d --wait postgres redis kafka kafka-init opa gateway frontend frontend-proxy
docker compose --profile migrate run --rm migrate
docker compose --profile test run --rm test-gateway
pytest tests/infra/test_group_d_health_dependencies.py tests/infra/test_group_d_chaos_cleanup.py tests/infra/test_group_d_image_pinning.py tests/infra/test_group_e_helm_governance.py -q
```

**Deliverable:** PR with updated Dockerfiles and `.dockerignore` files. PR description
includes before/after size comparison using F1 baseline data.

---

### F3 -- CI Metrics Artifact / Regression Guard

**Status:** STABILIZED
**PR:** #18 (squash-merge)
**Merge commit:** `main@402a273`
**Tag:** `hardening-group-f-f3-stabilized` -> `402a273`
**CI:** All checks passed on PR #18

**Goal:** Make image size and build performance visible in every CI run.

**Implemented:**
1. New script `scripts/ci-inspect-image.sh`:
   - Pulls just-pushed image by digest, runs `docker image inspect` and `docker history`
   - Outputs JSON artifact with: image, imageName, digest, uncompressedSize,
     uncompressedSizeMB, layerCount, created, buildTimestamp, buildDurationS,
     dockerHistory
   - Supports `--build-duration-seconds` flag for CI duration injection
2. CI `build` job (`.github/workflows/ci-cd.yml`) updated with 5 new steps:
   - Mark build start time (epoch in /tmp)
   - Build and push (unchanged)
   - Record build duration (end - start seconds)
   - Validate metrics script (`bash -n scripts/ci-inspect-image.sh`)
   - Collect image metrics (per matrix project)
   - Upload artifact (`image-metrics-gateway`, `image-metrics-frontend`, `image-metrics-agents`)
3. Artifact configuration:
   - Name: `image-metrics-${{ matrix.project }}`
   - Retention: 30 days
   - `if-no-files-found`: error
4. 32 regression tests in `tests/infra/test_group_f_image_metrics_ci.py` covering:
   - Step presence, ordering (after build-push), artifact naming, upload-action version,
     retention days, build duration recording, matrix project validation,
     script static analysis, bash syntax, no-mojibake, no push guard on individual steps

**Artifacts produced per main push:**
- `image-metrics-gateway`
- `image-metrics-frontend`
- `image-metrics-agents`

**Not implemented (F3b deferred):**
- PR-check job comparing metrics against main baseline (>10% size growth warning). This
  can be added later when baseline data from regular CI runs is accumulated.

**Risk:** Low (additive CI steps, no change to build output).

---

### F4 -- Agents Multi-Stage Conversion + Deeper Pruning

**Goal:** Close the Group F exit blocker (F-B1). Convert the agents image from
single-stage to multi-stage, eliminating `build-essential` from the runtime image.

This phase MUST only begin after:
- F1 baseline is committed (before/after comparison data exists)
- F2 is merged (Dockerfiles are clean, context sizes are minimized)
- F3 is merged (CI regression guard is in place)

#### F4a -- Agents multi-stage Dockerfile

**Precise plan:**

Current single-stage structure:
```dockerfile
FROM python:3.11-slim
RUN apt-get install build-essential
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "-m", "orchestrator.main"]
```

Target multi-stage structure:
```dockerfile
# syntax=docker/dockerfile:1.7

# --- builder stage: compiles native extensions ---
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --user -r requirements.txt


# --- runner stage: runtime only ---
FROM python:3.11-slim

# Create non-root user
RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 app

# Copy pip-installed packages from builder
COPY --from=builder /root/.local /home/app/.local

# Copy application source (runtime-only files)
COPY src/ /app/src/
COPY sitecustomize.py /app/

# Set Python path to include user-local packages
ENV PYTHONPATH=/app/src:/home/app/.local/lib/python3.11/site-packages
ENV PATH=/home/app/.local/bin:$PATH

WORKDIR /app
RUN chown -R app:app /app /home/app/.local
USER app

EXPOSE 5010

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import httpx,sys; r=httpx.get('http://localhost:5010/health'); sys.exit(0 if r.is_success else 1)"

CMD ["python", "-m", "orchestrator.main"]
```

Key design decisions:
- **Builder** installs `build-essential`, compiles native Python extensions via
  `pip install --user`, and is discarded after the build.
- **Runner** installs NO `build-essential`. It copies only the compiled
  `site-packages` from builder and the application source (`src/`,
  `sitecustomize.py`).
- **`tests/` is NOT copied.** The runner stage only COPYs `src/` and
  `sitecustomize.py`, so test files never enter the runtime image regardless of
  `.dockerignore` state.
- **`requirements.txt` still includes test dependencies** (`pytest`,
  `pytest-asyncio`, etc.). These will be compiled and installed by the builder
  and copied into the runner. This is non-ideal but acceptable for this phase.
  Splitting `requirements.txt` into `requirements.in` (runtime) and
  `requirements-dev.in` (test/tooling) is recorded as a follow-on optimization
  (F-D7).
- **Non-root user `app`** (uid 1001) owns `/app` and `/home/app/.local`.
  Consistent with the frontend pattern (`nextjs` uid 1001).
- **`PYTHONPATH`** includes both `/app/src` (application code) and the pip
  user-local site-packages path. The exact site-packages path is
  `python3.11/site-packages` for Python 3.11; if the base image Python version
  changes, this path must be updated.
- **`scripts/` is not copied.** The current Dockerfile does `COPY . .` which
  includes `scripts/`. The multi-stage explicitly only copies `src/` and
  `sitecustomize.py`. If any script under `scripts/` is imported at runtime,
  add an explicit `COPY scripts/<needed>.py /app/scripts/` line.

**Verification:**
```bash
# Rebuild and check size reduction
docker compose build --no-cache agents
docker image ls --format "table {{.Repository}}\t{{.Size}}" | grep agents

# Verify the agents service starts and passes healthcheck
docker compose up -d --wait agents

# Verify the replay-service also starts (same image)
docker compose up -d --wait replay-service

# Run the existing Python test suite inside a dev container or locally
pytest agents/tests -v
```

Risk: medium. The multi-stage conversion changes the filesystem layout
(`/home/app/.local` vs system `site-packages`). Python imports, entry points,
and the HEALTHCHECK may break if paths are incorrect. This is why F4 must only
proceed after F1 (baseline), F2 (cleanup), and F3 (guard) are in place.

#### F4b -- Deeper pruning (optional, scoped by F1/F2 metrics)

Only pursue if metrics justify it:
- Evaluate `python:3.11-slim` vs `python:3.11-alpine` for runner stage.
- Evaluate `node:20-slim` alternative to `node:20-bullseye` for gateway runner.
- Audit heavy Python/Node dependencies via `pipdeptree` / `npx depcheck`.
- Remove `libssl1.1` from gateway builder if `ldd` confirms it's unused (F-D1).

---

## 7. Risk Assessment

| Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|
| Multi-stage conversion (F-B1) breaks Python imports or healthcheck | Low | Medium | Verify with `docker compose up --wait` + pytest. Front-loaded with F1/F2/F3 guards. |
| Non-root USER switch (F-S7) breaks file permissions | Low | Medium | `chown -R` before USER; HEALTHCHECK catches failures immediately. |
| Missing `libssl1.1` causes OpenSSL link errors (F-D1) | Low | Medium | Only remove after verifying with `ldd` on the built app; defer to F4b. |
| Migrate context narrowing (F2d) breaks COPY paths | Low | Low | Mechanical path change; `docker compose --profile migrate run --rm migrate` catches. |
| CI size regression guard (F3) false-positives on routine dependency updates | Medium | Low | Allow explicit exemptions; start as a warning, not a hard fail. |
| `# syntax=docker/dockerfile:1.7` breaks non-BuildKit builds | Low | Medium | Project already requires BuildKit (CI uses buildx v3). Document the requirement. |

---

## 8. Recommended Execution Order

```
F1 (baseline metrics -- scripts + docs/baseline-group-f.md)
  |
  v
F2a (agents .dockerignore .env fix -- BLOCKER, zero risk)
  |
  v
F2b (agents .dockerignore test/cache exclusions -- zero risk)
  |
  v
F2c (root .dockerignore gap fill -- zero risk)
  |
  v
F2d (migrate context narrowing -- low risk, mechanical change)
  |
  v
F2e (frontend HEALTHCHECK -- low risk)
  |
  v
F2f (BuildKit cache mounts -- low risk, additive)
  |
  v
F2g (gateway non-root USER -- low-medium risk, HEALTHCHECK-gated)
  |
  v
F3  (CI metrics artifact -- additive, low risk)
  |
  v
F4a (agents multi-stage conversion -- medium risk, Group F exit gate)
  |
  v
F4b (deeper pruning -- optional, metrics-driven)
```

**Parallel opportunities:**
- F2a, F2b, F2c can be a single commit (all `.dockerignore` changes).
- F2e, F2f can be a single commit (all Dockerfile additive changes).
- F3 is independent of F2 and can start in parallel once F1 metrics are captured.

---

## 9. Verification Plan (Post-Implementation)

After each phase, run:

```bash
# Build all images
docker compose build --no-cache

# Verify services start healthy
docker compose up -d --wait postgres redis kafka kafka-init opa gateway frontend frontend-proxy
docker compose --profile migrate run --rm migrate

# Check image sizes
docker image ls --format "table {{.Repository}}\t{{.Size}}\t{{.Tag}}"

# Run existing test suites
pytest tests/infra/test_group_d_health_dependencies.py tests/infra/test_group_d_chaos_cleanup.py tests/infra/test_group_d_image_pinning.py tests/infra/test_group_e_helm_governance.py -q

# CI: verify helm-lint and build jobs still pass
```

---

## 10. Explicit Exclusions

As stated in the task scope, this preflight does NOT:
- Change application logic (no source code changes to gateway/agents/frontend business logic)
- Implement digest pinning (E3 / Phase 4 CI/CD)
- Introduce a container registry policy
- Modify the Helm release strategy or chart templates
- Enable Ollama by default
- Split `requirements.txt` into runtime/dev (recorded as F-D7 for future consideration)
