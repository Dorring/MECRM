# Group F F4 Self-Review

**Date:** 2026-07-12
**Branch:** main (direct commit, F4 is the exit blocker)
**Baseline:** F1 `main@ffef4fb` -- agents: 737.5 MB, 9 layers, 45.8s build
**Status:** PENDING CI VERIFICATION
**Commit:** `main@6a85e60`
**F-B1 (agents multi-stage):** IMPLEMENTED

## F4 Scope Checklist

| Step | Description | Status |
|---|---|---|
| F4-M1 | Multi-stage FROM (builder + runner) | DONE |
| F4-M2 | build-essential in builder only | DONE |
| F4-M3 | No build toolchain in runner (gcc, g++, make, binutils, libc-dev, headers) | DONE |
| F4-M4 | USER app (uid 1001, non-root) | DONE |
| F4-M5 | COPY /root/.local from builder to /home/app/.local | DONE |
| F4-M6 | PATH includes /home/app/.local/bin | DONE |
| F4-M7 | PYTHONPATH includes /app/src | DONE |
| F4-M8 | CMD remains python -m orchestrator.main | DONE |
| F4-M9 | Healthcheck status-code aware (httpx r.is_success) | DONE |
| F4-M10 | .dockerignore still excludes .env / .env.* | DONE |
| F4-M11 | Selective COPY (src/ + sitecustomize.py, no tests, scripts, COPY . .) | DONE |
| F4-M12 | F2 regression: syntax directive + cache mount preserved | DONE |

## Multi-Stage Design

```
            builder                          runner
    ┌──────────────────┐           ┌──────────────────────┐
    │ python:3.11-slim  │           │ python:3.11-slim     │
    │                    │           │                      │
    │ apt install        │           │ NO apt install       │
    │   build-essential │           │                      │
    │                    │           │ addgroup/adduser app │
    │ pip install --user │           │                      │
    │   -r requirements  │──COPY──>> │ COPY --from=builder  │
    │                    │  /root/  │   /root/.local ->    │
    │                    │  .local  │   /home/app/.local   │
    └──────────────────┘           │                      │
                                   │ COPY src/ /app/src/  │
                                   │ COPY sitecustomize   │
                                   │                      │
                                   │ chown app:app        │
                                   │ USER app             │
                                   │ ENV PATH, PYTHONPATH │
                                   │ HEALTHCHECK (httpx)  │
                                   │ CMD orchestrator.main│
                                   └──────────────────────┘
```

## Runtime Toolchain Absence Evidence

Static verification (F4-M3 tests):
- No `build-essential` in runner lines
- No `gcc`, `g++`, `make`, `binutils` in runner lines
- No `libc-dev` or `python*-dev` in runner lines
- No `apt-get install` in runner lines at all

The runner stage is a fresh `python:3.11-slim` with zero apt packages added.
All Python dependencies come from the builder's `pip install --user` output,
copied via `COPY --from=builder /root/.local /home/app/.local`.

## Replay-Service Compatibility

- `replay-service` (compose) uses the same agents Docker image, overriding CMD
  to `uvicorn replay.api:app --host 0.0.0.0 --port 5011`
- `COPY src/ /app/src/` covers `src/replay/` (api.py, db.py, event_ingestor.py,
  metrics.py, models.py, read_model_projector.py, replay_service.py,
  snapshot_store.py)
- `uvicorn` is installed by pip in builder (part of `uvicorn[standard]` in
  requirements.txt) and copied to runner via `/root/.local` -> `/home/app/.local`
- `PATH=/home/app/.local/bin:$PATH` makes `uvicorn` executable
- `sitecustomize.py` is copied to `/app/`; it adds `/app/src` to sys.path and
  conditionally adds `core_services/src/` (but `core_services/` is NOT in the
  image -- this import path will fail, consistent with previous behavior)

## What Changed vs F3

| Aspect | F3 (before) | F4 (after) |
|---|---|---|
| Stages | 1 (single-stage) | 2 (builder + runner) |
| build-essential | In runtime image | Only in builder (discarded) |
| COPY scope | `COPY . .` (everything) | `COPY src/` + `sitecustomize.py` |
| Root user | Yes (root) | No (app, uid 1001) |
| apt-get in runner | `apt-get install build-essential` | No apt-get at all |
| Pip install target | System site-packages | `--user` to /root/.local |
| Runtime packages path | System `/usr/local/lib/...` | `/home/app/.local/lib/...` |
| tests/ in image | Yes (via COPY . .) | No (selective COPY) |
| scripts/ in image | Yes (via COPY . .) | No (selective COPY) |
| .env exclusion | Yes (.dockerignore) | Yes (unchanged) |

## Baseline vs Expected Improvement

- **Baseline (F1):** agents 737.5 MB, 9 layers, 45.8s build
- **Expected:** Significant reduction from:
  - Dropping build-essential (~200-400MB including gcc, g++, make, binutils, dev headers)
  - Selective COPY (tests/, scripts/, conftest.py, pytest.ini, caches no longer in image)
- **Actual new size:** NOT YET MEASURED (Docker unavailable on Windows host).
  Will be captured by CI build artifact `image-metrics-agents` on next main push.

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Native Python packages (cryptography, orjson, etc.) need shared libs at runtime | Medium | builder installs the same `python:3.11-slim` base; compiled extensions link against system libs present in both builder and runner (libssl, libc, etc.). The `--user` install copies only `site-packages`; system-level shared libs are NOT copied and are NOT needed because the runner's own `python:3.11-slim` base includes them. |
| Selective COPY misses runtime file | Medium | Static test verifies all 8 runtime packages exist under `src/` (orchestrator, agents, intelligence, replay, governance, policy, projections, resilience, schema). `sitecustomize.py` is explicitly copied. Healthcheck would fail if imports are broken. |
| Non-root permission issues (logs, pid files) | Low | `chown -R app:app /app /home/app/.local` before USER switch. Gateway already uses same non-root pattern without issues. |
| uvloop/build-essential removal breaks uvicorn | Low | `uvicorn[standard]` depends on `uvloop` which has a pure-Python fallback. If uvloop fails to load due to missing compiled extension, uvicorn falls back to asyncio event loop automatically. |
| Replay-service CMD override fails | Low | `uvicorn` binary is in `/home/app/.local/bin`, which is in PATH. `replay.api:app` is under `src/replay/api.py`, covered by `COPY src/`. |

## Rollback Plan

Revert `agents/Dockerfile` to F3 version (single-stage with build-essential):
```bash
git checkout hardening-group-f-f3-stabilized -- agents/Dockerfile
```

## Verification Results

| Verification | Result |
|---|---|
| pytest tests/infra/test_group_f_agents_multistage.py -v | 37 passed |
| pytest tests/infra -v | 236 passed, 10 skipped |
| git diff --check | PASS (CRLF warning is Windows platform artifact only) |
| F2 regression (cache mount, syntax, dockerignore) | 30/30 F2 tests still pass |
| F3 regression (CI metrics steps) | 32/32 F3 tests still pass |
| D1-D3/E regression (all existing infra tests) | All pass |
| docker compose build agents | NOT RUN (Docker unavailable on Windows host) |
| docker compose up -d --wait agents | NOT RUN (Docker unavailable) |
| docker compose up -d --wait replay-service | NOT RUN (Docker unavailable) |
| docker run <image> sh -lc "which gcc; which make" | NOT RUN (Docker unavailable) |
| Build artifact image-metrics-agents (new size) | PENDING next main push |

## Unverified Items (Require Docker or CI)

- Actual image size reduction from dropping build-essential
- Runtime healthcheck passes (agents + replay-service)
- `uvicorn replay.api:app` CMD override works
- No shared library loading errors for compiled Python extensions
- Non-root user does not cause permission errors on file writes

These will be validated by:
- PR CI (compose config, smoke test)
- Main push Build & Push artifact `image-metrics-agents`

## Items Explicitly Not Done

- **requirements.txt split:** Not in F4 scope. Test dependencies (pytest,
  pytest-asyncio, pytest-cov) are installed by builder and copied to runner.
  This adds unnecessary bytes to the runtime image but is a follow-on
  optimization (F-D7), not the F4 exit blocker.
- **Python base image evaluation (alpine vs slim):** Deferred to F4b optional
  deeper pruning.
- **Ollama enablement:** Out of scope.
- **Business logic or import changes:** None. `src/` source files untouched.
- **core_services/ dependency:** `sitecustomize.py` references
  `core_services/src/`, but that path is NOT in the agents Docker image. This
  matches the F3 behavior -- `core_services/` is a separate layer and was never
  baked into the agents image.

## F-B1 Status

**F-B1 (agents multi-stage) is IMPLEMENTED.** The Group F exit blocker is
resolved from a code perspective. Final confirmation requires CI validation
(Docker build + healthcheck + image-metrics artifact).

## Self-Review Conclusion

- All 9 runtime packages under `src/` are covered by `COPY src/ /app/src/`.
- `sitecustomize.py` is explicitly copied for sys.path setup.
- `uvicorn` (replay-service) and `orchestrator.main` entrypoints are preserved.
- No build toolchain in runner stage (verified by 9 static tests).
- Non-root user with uid 1001, chown before USER (verified by 3 static tests).
- F2 and F3 regression guards pass (cache mount, syntax directive, dockerignore,
  CI metrics steps).
- 37 new F4 tests, 236 total infra tests, all pass.
- **F-B1 can be marked closed** subject to CI Docker build verification.
