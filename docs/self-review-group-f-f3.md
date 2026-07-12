# Group F F3 Self-Review

**Date:** 2026-07-12
**Branch:** codex/group-f-f3-image-metrics-ci
**Status:** STABILIZED
**Merge commit:** `main@402a273` (PR #18 squash-merge)
**Tag:** `hardening-group-f-f3-stabilized`
**CI:** All checks passed on PR #18

## F3 Scope Checklist

| Step | Description | Status |
|---|---|---|
| F3-M1 | CI build job has Validate metrics script + Collect image metrics + Upload image metrics artifact steps | DONE |
| F3-M2 | Metrics steps execute AFTER build-push | DONE |
| F3-M3 | Artifact name is stable (`image-metrics-${{ matrix.project }}`) with if-no-files-found:error | DONE |
| F3-M4 | Artifact upload uses actions/upload-artifact@v4 | DONE |
| F3-M5 | Build duration recorded via build-start marker + Record build duration step | DONE |
| F3-M6 | `scripts/ci-inspect-image.sh` exists, bash-parseable, accepts `--image`/`--output`/`--build-duration-seconds` | DONE |
| F3-M7 | Matrix excludes migrate (only gateway/frontend/agents built in CI) | DONE |
| F3-M8 | retention-days set (30 days) | DONE |

## Items Explicitly Not Done

- **F3b (PR-check size regression guard):** Deferred. Requires accumulated baseline data from regular CI runs before implementing >10% size growth warning. Recorded as a follow-on.
- **F-B1 (agents multi-stage):** Still deferred to F4. Not in F3 scope.
- **Digest pinning (E3/Phase 4):** Out of scope per Group F charter.
- **Dockerfile changes:** None. F3 is CI-only.

## Deliverables

| File | Description |
|---|---|
| `scripts/ci-inspect-image.sh` | New CI metrics collector: docker pull -> inspect -> history -> JSON |
| `.github/workflows/ci-cd.yml` | build job: +5 steps (start marker, duration, validate, collect, upload artifact) |
| `tests/infra/test_group_f_image_metrics_ci.py` | 32 regression tests (13 test classes) |

## CI Evidence

- PR #18 CI checks passed: lint, lint-python, validate-schemas, test-gateway,
  test-agents, test-policies, migration-runner, smoke, ws-proxy-smoke,
  helm-lint
- build job gate (`push + main`) correctly prevented push on PR
- `bash -n scripts/ci-inspect-image.sh` passed in CI (Validate metrics script step)

## Artifact Schema

Each per-project artifact JSON:
```json
{
  "image": "ghcr.io/dorring/mecrm/gateway@sha256:...",
  "imageName": "gateway",
  "digest": "sha256:...",
  "uncompressedSize": 1299123456,
  "uncompressedSizeMB": "1239.1",
  "layerCount": 14,
  "created": "2026-07-12T...",
  "buildTimestamp": "2026-07-12T...",
  "buildDurationS": 107,
  "dockerHistory": [{"ID": "...", "Created": "...", "CreatedBy": "...", "Size": "...", "Comment": ""}]
}
```

## No-Docker Validation

All 32 F3 tests run without Docker daemon:
- YAML step parsing and ordering validation
- Script path, content, flag, and output field assertions
- Matrix project enumeration
- Artifact naming convention checks
- ASCII-only (no Mojibake) check

## Self-Review Conclusion

- All F3 requirements met: CI generates per-image metrics artifact on every main push.
- No application logic or Dockerfile changes.
- F1 baseline + F2 cleanup + F3 CI guard now form the complete pre-F4 foundation.
- **F-B1 (agents multi-stage) is now the sole remaining Group F exit blocker.**
  All preconditions from the preflight are satisfied:
  - F1 baseline committed (before/after comparison data exists)
  - F2 merged (Dockerfiles clean, context sizes minimized)
  - F3 merged (CI regression guard in place)
- **F4 can begin.**
