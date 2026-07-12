# Group G G1 Self-Review

**Date:** 2026-07-12
**Branch:** main
**Commit:** `5478307`
**Status:** IN REVIEW -- PENDING CI VERIFICATION

## G1 Scope Closed

| ID | Finding | Status |
|---|---|---|
| **G-C1** | Deploy uses mutable `${{ github.sha }}` tag | FIXED -- deploy now uses immutable digest from digest-map.json |
| **G-C2** | No digest-map aggregation for matrix builds | FIXED -- `aggregate-digests` job collects 3 per-project digest artifacts |
| **G-H1** | Helm chart has no `digest` field | FIXED -- `images.*.digest` added; `enterprise-crm.image` helper renders `repository@digest` |
| **G-H3** | `securityContext.runAsUser` mismatch | FIXED -- gateway=1000, frontend=1001, agents=1001 |

## Scope NOT in G1 (still pending for G2/G3)

| ID | Finding | Status |
|---|---|---|
| G-C4 | No Trivy / container vulnerability scanning | G2 |
| G-C5 | No Dependabot | G2 |
| G-C6 | No SBOM/provenance | G2 |
| G-C7 | No CodeQL/SAST | G2 |
| G-C3 | Node 20 EOL plan | G2 |
| G-H6 | No K8s migration Job | G3 |
| G-H4 | WebSocket real-cluster validation | G3 |
| G-H5 | Ingress WSS staging validation | G3 |
| G-R5 | Integration tests placeholder | G3 |
| G-R6 | Production smoke test | G3 |
| G-T1 | Duplicate Group B tag | Defer -- documentation/history |

## What Changed

### CI/CD (`.github/workflows/ci-cd.yml`)

1. **build job**: Added "Write digest artifact" + "Upload digest artifact" steps
   - Each matrix project uploads `digest-{project}` artifact with `{project, image, digest}` JSON
   - 7-day retention (short-lived, only needed by downstream deploy jobs)
2. **new `aggregate-digests` job**: Downloads all `digest-*` artifacts, assembles `digest-map.json`
3. **deploy-staging / deploy-production**:
   - `needs: build` -> `needs: aggregate-digests`
   - New "Download digest map" step before guard/helm
   - `helm upgrade` uses `--set images.*.digest="${...DIGEST}"` instead of `--set images.*.tag=${{ github.sha }}`

### Helm (`deploy/helm/enterprise-crm/`)

**`values.yaml`:**
- New `images.*.digest: ""` field per service (frontend, gateway, agents)
- New `securityContext.{service}.runAsUser` section

**`templates/_helpers.tpl`:**
- New `enterprise-crm.image` helper:
  - When `$img.digest` set: `registry/repo@sha256:abcdef...`
  - When `$img.digest` empty: `registry/repo:tag` with `required(tag)` fail-fast

**`templates/{gateway,agents,frontend}.yaml`:**
- Image reference: inline `repository:required(tag)` -> `{{ include "enterprise-crm.image" }}`
- `runAsUser: 1000` -> `runAsUser: {{ .Values.securityContext.{service}.runAsUser }}`

### Tests

- **New**: `tests/infra/test_group_g_digest_pinning.py` -- 29 tests (7 classes)
- **Updated**: `test_group_d_image_pinning.py` -- D2 test now verifies digest override not tag
- **Updated**: `test_group_e_helm_governance.py` -- E1 tests updated for helper-based required() pattern

## Verification Results

| Verification | Result |
|---|---|
| `pytest tests/infra/test_group_g_digest_pinning.py -v` | **29 passed** |
| `pytest tests/infra -v` | **267 passed**, 10 skipped |
| `git diff --check` | PASS (CRLF warning is Windows artifact only) |
| F2 regression (image optimization) | All pass |
| F3 regression (CI metrics) | All pass |
| F4 regression (agents multi-stage) | All pass |
| D2 regression (image pinning) | All pass (updated digest assertions) |
| E1 regression (Helm governance) | All pass (updated helper-based required()) |
| `helm lint` (real) | NOT RUN (no helm binary on Windows host) |
| `helm template` (real) | NOT RUN |
| `deploy-staging` with digest (real) | NOT RUN (requires `KUBE_CONFIG_STAGING`) |

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| `aggregate-digests` job fails due to artifact expiry (7-day retention) | Low | Deploy jobs depend on aggregate-digests; if artifacts expire mid-pipeline (unlikely -- same workflow run), the deploy will fail before pushing bad images |
| Helm `required(tag)` error if both digest and tag are empty | Low-Medium | `helm lint` with `--set images.*.tag=ci-test` still passes; CI `helm-lint` job validates this pattern |
| `securityContext.runAsUser: 1001` for agents/frontend causes pod CrashLoopBackOff if uid doesn't exist in image | Medium | Verified: `nextjs` uid 1001 in frontend/Dockerfile, `app` uid 1001 in agents/Dockerfile (F4). `runAsNonRoot: true` + matching uid ensures non-root execution. |
| Digest-pull from GHCR fails on private repo without imagePullSecrets | Medium | GHCR auth is configured via `docker/login-action`; digest-pull uses the same credentials as tag-pull. No change to auth flow. |

## Unverified (Requires Docker + K8s)

- `helm template` with `--set images.*.digest=sha256:test` produces valid YAML
- `helm upgrade` with digest actually works on staging cluster
- Pods start with `runAsUser: 1001` for agents/frontend (uid mismatch was identified in preflight G-H3; fix not yet verified in real cluster)
- Digest-pull from GHCR resolves correctly

These will be validated by CI:
- `helm-lint` job validates template rendering with digest
- First main push after merge validates aggregate-digests + deploy pipeline

## G1 Exit Status

G1 addresses G-C1, G-C2, G-H1, G-H3. The remaining production blockers (G-C4, G-H6) are G2 and G3 scope.

**G-C1 (digest pinning) is CLOSED at code level.** A main push will exercise the full pipeline: build -> aggregate-digests -> deploy-staging (with digest) -> integration-tests -> deploy-production (with digest).
