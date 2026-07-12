# Group G G1 Self-Review

**Date:** 2026-07-12
**Branch:** main (direct-to-main, no PR)
**Commit:** `main@8690ee5`
**Tag:** `hardening-group-g-g1-stabilized` -> `8690ee5`
**Status:** STABILIZED
**CI:** CI/CD Pipeline #94 + Tenant Isolation Proof #94 all green

## G1 Scope Closed

| ID | Finding | Status |
|---|---|---|
| **G-C1** | Deploy uses mutable `${{ github.sha }}` tag | CLOSED -- deploy uses immutable digest + full repository from digest-map.json |
| **G-C2** | No digest-map aggregation for matrix builds | CLOSED -- `aggregate-digests` job with defensive validation |
| **G-H1** | Helm chart has no `digest` field | CLOSED -- `images.*.digest` + `enterprise-crm.image` helper |
| **G-H3** | `securityContext.runAsUser` mismatch | CLOSED -- gateway=1000, frontend=1001, agents=1001 |

## Direct-to-Main Rationale

G1 was committed directly to main (no PR branch) because:
- All changes were additive to CI workflow + Helm chart -- no application logic touched
- 272 infra tests pass locally (identical to CI)
- CI/CD Pipeline #94 validates all changes: helm-lint (tag-mode + digest-mode), build, smoke, deploy job dry-runs
- Tenant Isolation Proof #94 validates Helm rendering + OPA version consistency

## Commits

| Commit | Description |
|---|---|
| `5478307` | `feat(g1): implement digest pinning + Helm UID fix` |
| `412b7fb` | `docs(g1): add Group G G1 self-review` |
| `908427b` | `fix(g1): set full repository from digest-map, add Helm digest-mode CI` |
| `8690ee5` | `polish(g1): unify digest params to --set-string, add staging/prod digest grep assertions` |

## Digest Deploy Fix Summary

### Blocker 1 -- repository from digest-map

Deploy jobs now extract both `image` and `digest` from `digest-map.json`:

```bash
GW_IMAGE=$(jq -r '.gateway.image' /tmp/digest-map/digest-map.json)
GW_DIGEST=$(jq -r '.gateway.digest' /tmp/digest-map/digest-map.json)
helm upgrade ... \
  --set-string images.gateway.repository="${GW_IMAGE}" \
  --set-string images.gateway.digest="${GW_DIGEST}"
```

Template renders: `ghcr.io/dorring/mecrm/gateway@sha256:...`
NOT: `enterprise-crm/gateway@sha256:...`

### Blocker 2 -- Helm CI digest-mode coverage

helm-lint job has 4 digest-mode steps:
- `Lint chart (digest mode)`
- `Render default template (digest mode)` -- with ghcr.io assertion + enterprise-crm/ rejection
- `Render staging template if present (digest mode)` -- with ghcr.io assertion + enterprise-crm/ rejection
- `Render production template if present (digest mode)` -- with ghcr.io assertion + enterprise-crm/ rejection

All digest params use `env:` block (avoids YAML escaping issues with sha256: prefix).

### All digest params use --set-string

`--set` was replaced with `--set-string` for ALL `images.*.digest=` parameters across lint, template, deploy-staging, and deploy-production. This prevents Helm from interpreting `sha256:` as a number/boolean type.

### aggregate-digests defensive validation

- `set -euo pipefail`
- Existence check for all 3 `digest-{project}.json` files
- `sha256:` format validation via regex
- Project field validation
- `jq empty` JSON parse check

## Verification Results

| Verification | Result |
|---|---|
| `pytest tests/infra -q` | **277 passed, 10 skipped** |
| `pytest tests/infra/test_group_g_digest_pinning.py -q` | **39 passed** (10 test classes) |
| `pytest tests/infra/test_group_d_image_pinning.py tests/infra/test_group_e_helm_governance.py tests/infra/test_group_g_digest_pinning.py -q` | **72 passed, 3 skipped** |
| `git diff --check` | Clean |
| F2 regression (image optimization) | All pass |
| F3 regression (CI metrics) | All pass |
| F4 regression (agents multi-stage) | All pass |
| D1/D2/D3 regression (chaos/OPA) | All pass |
| E1 regression (Helm governance, updated) | All pass |
| CI/CD Pipeline #94 | All green (14 jobs) |
| Tenant Isolation Proof #94 | All green |

## Items NOT in G1 (remaining for G2/G3)

| ID | Finding | Status |
|---|---|---|
| G-C4 | No Trivy / container scanning | G2 |
| G-C5 | No Dependabot | G2 |
| G-C6 | No SBOM/provenance | G2 |
| G-C7 | No CodeQL/SAST | G2 |
| G-C3 | Node 20 EOL plan | G2 |
| G-H6 | No K8s migration Job | G3 |
| G-H4 | WebSocket real-cluster validation | G3 |
| G-H5 | Ingress WSS staging validation | G3 |
| G-R5/R6 | Integration/smoke tests | G3 |
| G-T1 | Duplicate Group B tag | Defer |

## Next Phase

**G2 -- Supply-Chain Security Scan**: Trivy container scanning, Dependabot, SBOM/provenance, CodeQL. CI-only, no K8s/secrets required.
