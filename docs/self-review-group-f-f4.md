# Group F F4 Self-Review

**Date:** 2026-07-12
**Branch:** main (direct F4 commit path)
**Baseline:** F1 `main@ffef4fb` -- agents: 737.5 MB, 9 layers, 45.8s build
**Status:** PENDING CI DOCKER VERIFICATION
**F-B1 (agents multi-stage):** IMPLEMENTED

## F4 Scope Checklist

| Step | Description | Status |
|---|---|---|
| F4-M1 | Multi-stage FROM (builder + runner) | DONE |
| F4-M2 | build-essential in builder only | DONE |
| F4-M3 | No build toolchain in runner (gcc, g++, make, binutils, libc-dev, headers) | DONE |
| F4-M4 | USER app (uid 1001, non-root) | DONE |
| F4-M5 | COPY /root/.local from builder to /home/app/.local with app ownership | DONE |
| F4-M6 | PATH includes /home/app/.local/bin | DONE |
| F4-M7 | PYTHONPATH includes /app/src | DONE |
| F4-M8 | CMD remains python -m orchestrator.main | DONE |
| F4-M9 | Healthcheck status-code aware (httpx r.is_success) | DONE |
| F4-M10 | .dockerignore still excludes .env / .env.* | DONE |
| F4-M11 | Selective COPY (src/ + sitecustomize.py, no tests, scripts, COPY . .) | DONE |
| F4-M12 | F2 regression: syntax directive + usable cache mount preserved | DONE |
| F4-M13 | Runtime source covers orchestrator and replay-service packages | DONE |

## Multi-Stage Design

```text
builder:
  FROM python:3.11-slim
  apt-get install build-essential
  pip install --user -r requirements.txt

runner:
  FROM python:3.11-slim
  addgroup/adduser app uid 1001
  COPY --from=builder --chown=app:app /root/.local /home/app/.local
  COPY --chown=app:app src/ /app/src/
  COPY --chown=app:app sitecustomize.py /app/
  ENV HOME=/home/app
  ENV PATH=/home/app/.local/bin:$PATH
  ENV PYTHONPATH=/app/src
  USER app
  HEALTHCHECK via httpx status check
  CMD python -m orchestrator.main
```

## Runtime Toolchain Absence Evidence

Static verification checks:

- No `build-essential` in runner lines.
- No `gcc`, `g++`, `make`, `binutils` in runner lines.
- No `libc-dev` or `python*-dev` in runner lines.
- No `apt-get install` in runner lines.

The runner stage is a fresh `python:3.11-slim` with zero apt packages added.
All Python dependencies come from the builder's `pip install --user` output,
copied via `COPY --from=builder --chown=app:app /root/.local /home/app/.local`.

## Review Fixes Applied

| Issue | Fix |
|---|---|
| `RUN chown -R app:app ...` added a separate metadata layer | Replaced with `COPY --chown=app:app` for dependencies and source |
| `pip install --no-cache-dir` disabled the F2 BuildKit pip cache mount | Removed `--no-cache-dir`; cache remains in the builder cache mount, not in the final runner image |
| Self-review contained mojibake box-drawing output | Rewrote this document in clean ASCII |

## Replay-Service Compatibility

- `replay-service` uses the same agents Docker image and overrides CMD to
  `uvicorn replay.api:app --host 0.0.0.0 --port 5011`.
- `COPY --chown=app:app src/ /app/src/` covers `src/replay/`.
- `uvicorn` is installed by pip in builder (`uvicorn[standard]`) and copied
  into `/home/app/.local`.
- `PATH=/home/app/.local/bin:$PATH` makes `uvicorn` executable.
- `sitecustomize.py` is copied to `/app/` and preserves sys.path setup.

## What Changed vs F3

| Aspect | F3 (before) | F4 (after) |
|---|---|---|
| Stages | 1 (single-stage) | 2 (builder + runner) |
| build-essential | In runtime image | Only in builder (discarded) |
| COPY scope | `COPY . .` (everything) | `COPY --chown src/` + `sitecustomize.py` |
| Root user | root | app (uid 1001) |
| apt-get in runner | `apt-get install build-essential` | No apt-get at all |
| Pip install target | System site-packages | `--user` to `/root/.local`; BuildKit pip cache active |
| Runtime packages path | System `/usr/local/lib/...` | `/home/app/.local/lib/...` |
| tests/ in image | Yes (via COPY . .) | No (selective COPY) |
| scripts/ in image | Yes (via COPY . .) | No (selective COPY) |

## Baseline vs Expected Improvement

- **Baseline (F1):** agents 737.5 MB, 9 layers, 45.8s build.
- **Expected:** reduction from removing build-essential and excluding tests/scripts/tooling from the runtime image.
- **Actual new size:** pending CI Build & Push artifact `image-metrics-agents`.

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Native Python package needs shared runtime library | Medium | Builder and runner use the same `python:3.11-slim` base; CI Docker build/healthcheck must confirm imports |
| Selective COPY misses runtime file | Medium | Static test verifies required packages under `src/`; replay package is explicitly covered |
| Non-root permission issue | Low | Dependencies and source are copied with `--chown=app:app` before `USER app` |
| Replay-service CMD override fails | Low | `uvicorn` is on PATH and `replay.api` is under copied `src/replay/` |

## Verification Results

| Verification | Result |
|---|---|
| `pytest tests/infra/test_group_f_agents_multistage.py -v` | 39 passed |
| `pytest tests/infra -v` | 237 passed, 11 skipped |
| `git diff --check` | PASS |
| F2 regression | cache mount, syntax directive, dockerignore checks pass |
| F3 regression | CI metrics checks pass |
| `docker compose build agents` | NOT RUN locally (Docker unavailable) |
| `docker compose up -d --wait agents` | NOT RUN locally |
| `docker compose up -d --wait replay-service` | NOT RUN locally |
| Build artifact `image-metrics-agents` | PENDING next CI Docker build |

## Unverified Items Requiring Docker or CI

- Actual image size reduction.
- Agents healthcheck passes.
- Replay-service `uvicorn replay.api:app` starts.
- No shared library loading errors for compiled Python dependencies.
- Non-root user has sufficient runtime permissions.

## Items Explicitly Not Done

- **requirements.txt split:** Deferred. Test dependencies are still copied into runtime; this is follow-on pruning, not the F4 exit blocker.
- **Alpine/distroless evaluation:** Deferred.
- **Ollama enablement:** Out of scope.
- **Business logic changes:** None.

## F-B1 Status

**F-B1 (agents multi-stage) is implemented.** Final close requires CI Docker
build plus `image-metrics-agents` artifact confirmation after the review-fix
commit lands.
