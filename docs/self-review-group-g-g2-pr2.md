# Group G G2 PR2 Self-Review

**Date:** 2026-07-13
**Branch:** main (direct-to-main)
**Status:** STABILIZED
**Tag:** `hardening-group-g-g2-pr2-stabilized` -> `101e784`
**CI:** CI/CD Pipeline + CodeQL Analysis all green; Dependabot actively generating PRs

## G2 PR2 Scope Closed

| ID | Finding | Status |
|---|---|---|
| G-C5 | No Dependabot config | CLOSED |
| G-C7 | No CodeQL/SAST on source code | CLOSED |
| G-C3 | Node 20 EOL -- migration plan needed | CLOSED (assessment doc written) |

## Commit Chain

| Commit | Description |
|---|---|
| `323ad11` | feat(security): add CodeQL and Dependabot governance |
| `101e784` | test(security): add G2 PR2 governance regressions |
| `35acd13` | fix(g2-pr2): remove core_services pip entry (no manifest) |

## Self-Review Items

### 1. CodeQL

| Check | Result |
|---|---|
| Separate workflow (not mixed into ci-cd.yml) | PASS -- `.github/workflows/codeql.yml` |
| Triggers: pull_request, push main, weekly schedule | PASS |
| Languages: javascript-typescript + python | PASS |
| Queries: security-extended | PASS |
| Permissions minimal: security-events:write, packages/actions/contents:read | PASS |
| No deploy, image build, K8s, or business logic | PASS |

### 2. Dependabot

| Check | Result |
|---|---|
| github-actions / | PASS |
| npm /gateway | PASS -- package.json exists |
| npm /frontend | PASS -- package.json exists |
| pip /agents | PASS -- requirements.txt exists |
| pip /core_services | FIXED -- commented out because no requirements.txt exists; uncomment when manifest added |
| docker /gateway | PASS -- Dockerfile exists |
| docker /frontend | PASS -- Dockerfile exists |
| docker /agents | PASS -- Dockerfile exists |
| docker /database | PASS -- Dockerfile.migrate exists |
| Weekly schedule | PASS |
| open-pull-requests-limit: 5 per ecosystem | PASS |
| minor/patch grouped | PASS |
| major NOT grouped | PASS |
| labels include dependencies + security | PASS |
| Dependabot actively generating PRs (observed 20+ branches) | CONFIRMED |

### 3. Node 20 Assessment

| Check | Result |
|---|---|
| Assessment doc exists | PASS -- `docs/node-runtime-migration-assessment.md` |
| No runtime upgrade in this PR | PASS |
| Lists setup-node@v4 + node:20-* locations | PASS |
| Covers Node 24 migration risks (Next.js, Jest, Prisma, OpenSSL, Docker) | PASS |
| States migration is a separate future PR | PASS |

### 4. Tests

| Check | Result |
|---|---|
| `test_group_g_g2_codeql_dependabot.py` | 22 passed |
| `pytest tests/infra -q` | 333 passed, 10 skipped |
| `git diff --check` | Clean |

## Explicitly NOT in G2 PR2

| Item | Phase |
|---|---|
| Node 20/24 runtime upgrade | Separate PR after assessment |
| K8s migration Job | G3 |
| Staging validation | G3 |
| Trivy gate changes | G2 PR1 (closed) |
| SBOM/provenance changes | G2 PR1 (closed) |
| Business logic changes | None |
| Deploy changes | None |

## Verification

- `pytest tests/infra -q`: 333 passed, 10 skipped
- `git diff --check`: Clean
- CI: CI/CD Pipeline + CodeQL Analysis all green
- Dependabot: 20+ automatic PRs generated across all ecosystems

## G2 Fully Closed

G2 PR1 (Trivy + SBOM + provenance + SARIF) and G2 PR2 (CodeQL + Dependabot + Node assessment) together close all supply-chain security items. Remaining production blocker: G-H6 (K8s migration Job + staging validation) -> G3.
