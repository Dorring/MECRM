# Group G G2 PR1 Self-Review

**Date:** 2026-07-12
**Branch:** main (direct-to-main)
**Status:** STABILIZED
**Tag:** `hardening-group-g-g2-pr1-stabilized` -> `99c957e`
**CI:** All workflows green (CI/CD Pipeline, Security Scan (PR), Tenant Isolation Proof)

## G2 PR1 Scope Closed

| ID | Finding | Status |
|---|---|---|
| **G-C4** | No Trivy container vulnerability scanning | CLOSED |
| **G-C6** | No SBOM/provenance for container images | CLOSED |

## Commit Chain

| Commit | Description |
|---|---|
| `b71e992` | feat: add Trivy scan, CycloneDX SBOM, provenance, SARIF |
| `ce66062` | fix: add PR security-scan job, JSON report, pin Trivy version |
| `3a25e87` | fix: ignore unfixed CRITICAL CVEs in Trivy gate |
| `b1dbdc1` | fix: upgrade gateway protobufjs for fixed CRITICAL CVE |
| `99c957e` | fix: apply gateway Debian security upgrades |

## Architecture

### Main Push Path (build job)

1. Build and push image to GHCR with `sbom: true`, `provenance: true`
2. Pull just-pushed image by immutable digest
3. Trivy scan (3 passes on GHCR digest image):
   - Pass 1: SARIF (all severities, `--exit-code 0`)
   - Pass 2: JSON (all severities, `--exit-code 0`)
   - Pass 3: CRITICAL gate (`--ignore-unfixed --exit-code 1`)
4. Upload SARIF artifact (30-day retention)
5. Upload JSON artifact (30-day retention)
6. Extract CycloneDX SBOM from digest image, upload artifact (30-day retention)
7. Upload SARIF to GitHub Security tab (main push only)

### PR Path (security-scan job)

1. Build image locally with `push: false, load: true`
2. Trivy scan (3 passes on local image):
   - Same 3-pass pattern as main push
3. Upload SARIF artifact (14-day retention)
4. Upload JSON artifact (14-day retention)
5. Extract CycloneDX SBOM, upload artifact (14-day retention)
6. **No GitHub Security SARIF upload on PR**

### PR vs Main Push Comparison

| Aspect | Main Push | PR |
|---|---|---|
| Image source | GHCR digest (pushed) | Local (load: true, push: false) |
| CRITICAL gate | Yes (`--ignore-unfixed --exit-code 1`) | Yes (same) |
| SARIF artifact | Yes (30 days) | Yes (14 days) |
| JSON artifact | Yes (30 days) | Yes (14 days) |
| SBOM artifact | Yes (30 days) | Yes (14 days) |
| GitHub Security upload | Yes | No |

## CVE Resolution Strategy

Two CRITICAL CVEs were fixed rather than suppressed:
- **protobufjs:** Upgraded to >= 7.5.5 in gateway/package-lock.json (CVE-2025-39925)
- **Debian OS packages:** Added `apt-get upgrade -y` in gateway Dockerfile builder and runner stages to apply security patches for `libgnutls30`, `libtasn1-6`, `openssl`, and `perl` CVEs

Unfixed/fix_deferred CVEs in upstream base images are excluded from the build gate via `--ignore-unfixed` on the CRITICAL pass only. They remain visible in the SARIF/JSON report artifacts for tracking.

## .trivyignore

Empty policy file exists at repo root. Header comment documents the suppression format. No suppressions were added -- CVEs were fixed directly.

## Tests

| Suite | Result |
|---|---|
| `test_group_g_g2_supply_chain.py` | **30 passed** (10 test classes, G2-01 through G2-20) |
| Full infra suite | **311 passed, 10 skipped** |
| `git diff --check` | Clean |

New tests since initial PR1 commit:
- G2-18: `--ignore-unfixed` only on CRITICAL gate pass
- G2-19: gateway protobufjs lockfile version fixed
- G2-20: gateway Dockerfile applies Debian security upgrades

## Explicitly NOT in G2 PR1

| Item | Phase |
|---|---|
| CodeQL | G2 PR2 |
| Dependabot | G2 PR2 |
| Node 20 migration plan | G2 assessment / separate |
| K8s migration Job | G3 |
| Staging validation | G3 |
| Business logic changes | None |
| Deploy changes | None |

## Verification

- `pytest tests/infra -q`: 311 passed, 10 skipped
- `git diff --check`: Clean
- CI: All workflows green
