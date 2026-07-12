# Group G G2 Preflight -- Supply-Chain Security Scan

**Date:** 2026-07-12
**Baseline:** main@8f6e10c (G1 closeout complete)
**Status:** PREFLIGHT -- PENDING REVIEW

## Executive Summary

G2 adds supply-chain security scanning to the CI pipeline. This is a CI-only phase: no K8s
secrets, no application logic changes, no deploy changes. The recommended approach is two
PRs: PR1 (Trivy + SBOM/provenance + SARIF) and PR2 (CodeQL + Dependabot), with Node 20
migration assessed separately.

---

## 1. Current CI Security Posture

### 1.1 Inventory

| Capability | Status | Evidence |
|---|---|---|
| Trivy container scan | NOT PRESENT | No trivy-action, no trivy CLI in any workflow |
| CodeQL / SAST | NOT PRESENT | No github/codeql-action, no .github/workflows/codeql*.yml |
| Dependabot | NOT PRESENT | No .github/dependabot.yml |
| SBOM generation | NOT PRESENT | docker/build-push-action@v5 has sbom: true support -- not used |
| Provenance attestation | NOT PRESENT | docker/build-push-action@v5 has provenance: true support -- not used |
| npm-audit / pip-audit CI gate | NOT PRESENT | No audit steps in lint or test jobs |
| Secret scanning (GitHub native) | UNKNOWN | Depends on repo settings, not workflow code |

### 1.2 Build & Push Current Configuration

File: .github/workflows/ci-cd.yml, build job (lines 765-776):

```yaml
- name: Build and push
  id: build-push
  uses: docker/build-push-action@v5
  with:
    builder: ${{ steps.buildx.outputs.name }}
    context: ${{ matrix.context }}
    file: ${{ matrix.dockerfile }}
    push: true
    tags: ${{ steps.meta.outputs.tags }}
    labels: ${{ steps.meta.outputs.labels }}
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

Missing: provenance, sbom, attestation parameters.

### 1.3 Permissions

```yaml
permissions:
  contents: read
  packages: write
```

Missing: actions: read (for artifact download in Trivy/SARIF steps), security-events: write
(for SARIF upload to GitHub Security tab).

### 1.4 Node 20 Deprecation

| File | Occurrences |
|---|---|
| .github/workflows/ci-cd.yml | 5x actions/setup-node@v4 with node-version: "20" |
| .github/workflows/tenant-isolation.yml | 1x actions/setup-node@v4 with node-version: "20" |
| Dockerfiles | gateway/Dockerfile, frontend/Dockerfile, database/Dockerfile.migrate all use node:20-* |

Node 20 EOL: 2026-04-30. Currently July 2026 -- Node 20 is already past EOL.

The deprecation manifests as:
- GitHub Actions runner warnings on actions/setup-node@v4
- No security patches for Node 20 runtime
- Docker base images node:20-* may stop receiving updates

Immediate risk: LOW (Node 20 still receives community backports). Becomes MEDIUM by
2026-10 and HIGH by 2027-01.

---

## 2. G2 Scope Breakdown

### G2a -- Trivy Container Scan

**What:** Scan pushed container images (gateway, frontend, agents) for known CVEs using
aquasecurity/trivy-action or trivy CLI.

**Where:** In the build job, after "Build and push", on the just-pushed digest.

**Gating:**
- CRITICAL: fail the build (exit code 1)
- HIGH: upload SARIF report, do NOT fail (report-only initially; re-evaluate after 2 weeks of data)
- MEDIUM/LOW: SARIF only

**SARIF upload:** actions/upload-artifact (trivy-results-gateway.sarif) AND
github/codeql-action/upload-sarif for GitHub Security tab integration.

**Three images scanned:** gateway, frontend, agents. Same matrix build, same Trivy step.

### G2b -- SBOM / Provenance Artifact

**What:** Enable native SBOM and provenance attestation on docker/build-push-action@v5.

**Parameters to add:**
```yaml
provenance: true
sbom: true
```

**Outputs:**
- SBOM in SPDX format attached to the image manifest
- Provenance attestation signed with GitHub Actions OIDC token

**Gating:** NOT a fail condition. These are attestation-only artifacts. If docker/buildx
fails to generate them, the build should still pass (non-blocking).

### G2c -- Dependabot Config

**What:** .github/dependabot.yml for npm (gateway, frontend) and pip (agents).

**Config:**
```yaml
version: 2
updates:
  - package-ecosystem: "npm"
    directory: "/gateway"
    schedule: { interval: "weekly" }
    open-pull-requests-limit: 5
  - package-ecosystem: "npm"
    directory: "/frontend"
    schedule: { interval: "weekly" }
    open-pull-requests-limit: 5
  - package-ecosystem: "pip"
    directory: "/agents"
    schedule: { interval: "weekly" }
    open-pull-requests-limit: 5
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule: { interval: "monthly" }
```

**Gating:** Dependabot operates on its own schedule. It creates PRs, not CI failures. This
is purely additive and does not block any CI job.

**Risk:** open-pull-requests-limit: 5 per ecosystem = max 20 concurrent Dependabot PRs.
This is manageable for a repo this size.

### G2d -- CodeQL Workflow

**What:** GitHub's native code scanning via github/codeql-action.

**Where:** New workflow file .github/workflows/codeql.yml, triggered on push to main and
PR to main.

**Languages:** javascript-typescript (gateway + frontend), python (agents).

**Gating:** CodeQL uploads SARIF to GitHub Security tab. Most findings are informational.
Configure as:
- error severity: fail PR check
- warning/note: SARIF only

**Monorepo handling:** CodeQL auto-detects languages. For a monorepo with separate
gateway/frontend/agents directories, set paths or use matrix builds to avoid scanning
test files and node_modules.

### G2e -- Node 20 Migration Assessment

**What:** Document the Node 22 migration path. Do NOT upgrade as part of G2.

**Assessment scope:**
1. Check Next.js 14 compatibility with Node 22 (Next.js 14 supports Node 18.17+,
   including Node 22)
2. Check gateway Express/Prisma compatibility with Node 22 (Express 4.x and Prisma 5.x
   both support Node 22)
3. Check docker-compose services for node:22-* base image availability
4. Check CI actions/setup-node@v4 node-version: "22" support

**Expected outcome:** Both Next.js 14 and Express/Prisma should be compatible with Node 22
without code changes. The migration is primarily:
- Dockerfiles: node:20-* -> node:22-*
- CI: node-version: "20" -> node-version: "22"
- package.json engines field update (optional)

**Gating:** This is a documentation/output item in G2. The actual upgrade should be a
separate PR (not part of G2 implementation) to avoid mixing runtime changes with security
scanning.

---

## 3. Gating Strategy

### 3.1 Severity Matrix

| Scanner | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| Trivy (container) | FAIL build | SARIF report-only | SARIF only | SARIF only |
| CodeQL (source) | FAIL PR | SARIF report-only | SARIF only | Ignore |
| Dependabot | N/A (creates PRs) | N/A | N/A | N/A |
| SBOM/provenance | N/A (attestation only) | N/A | N/A | N/A |

### 3.2 PR vs Main Push Behavior

| Event | Trivy | CodeQL | SBOM/provenance | SARIF upload |
|---|---|---|---|---|
| PR to main | Run scan, fail on CRITICAL | Run analysis, fail on error | Generate (non-blocking) | Upload to artifacts (not Security tab on PR) |
| Push to main | Run scan, fail on CRITICAL | Run analysis | Generate + attach to image | Upload to GitHub Security tab |

### 3.3 SARIF Upload Target

- Trivy SARIF: upload-sarif to GitHub Security tab (requires security-events: write
  permission)
- CodeQL SARIF: native integration, auto-uploaded
- SARIF naming convention: trivy-results-${{ matrix.project }}.sarif

### 3.4 What NOT to Scan

- Migrate image: not built in CI
- Compose-only images (kafka-init, frontend-proxy): not pushed to registry
- Test dependencies inside built images: Trivy scans the full image, including test deps

---

## 4. Risk Assessment

### 4.1 Trivy DB Rate Limiting

**Risk:** aquasecurity/trivy-action pulls the Trivy vulnerability DB from ghcr.io on
every run. GitHub Actions IPs may hit Docker Hub / GHCR rate limits.

**Mitigation:**
- Use trivy-action's built-in cache (it caches the DB in the GitHub Actions cache)
- OR use the trivy CLI directly with --cache-dir pointing to a GHA-cached directory
- DB refresh interval: once per workflow run is acceptable (typically 3-5s download)

### 4.2 GHCR Private Image Pull for Trivy

**Risk:** Trivy needs to pull the just-pushed image from GHCR to scan it. Since the build
job already logged in via docker/login-action, the same credentials apply.

**Mitigation:** Trivy step runs after Build and push, same job, same docker login session.
No additional auth needed.

### 4.3 SARIF Upload Permissions

**Risk:** github/codeql-action/upload-sarif requires security-events: write permission.
This is not currently in the workflow permissions block.

**Mitigation:** Add security-events: write to the build job permissions. This is a
standard GitHub permission; no secret or admin approval needed.

### 4.4 CodeQL Monorepo Scope

**Risk:** CodeQL auto-build may scan test files, node_modules, or other noise in a
monorepo with TypeScript + Python.

**Mitigation:**
- For TypeScript: set paths to gateway/src and frontend/src
- For Python: set paths to agents/src
- Use query-suite: security-extended (not security-and-quality which produces more noise)
- Exclude tests/ directories from CodeQL analysis paths

### 4.5 Dependabot PR Volume

**Risk:** Enabling Dependabot on 4 ecosystems (gateway npm, frontend npm, agents pip,
github-actions) could create 10-20 PRs/week initially.

**Mitigation:**
- open-pull-requests-limit: 5 per ecosystem
- schedule: weekly (not daily)
- First run will produce the most PRs; subsequent runs are incremental
- Can be tuned: reduce limits or add ignore-dependencies for known-slow-moving packages

### 4.6 Trivy False Positives

**Risk:** Trivy may flag CVEs that are not exploitable in the CRM context (e.g., a CVE in
a Python package used only for CLI tooling, not at runtime).

**Mitigation:**
- CRITICAL-only fail gives a high bar before blocking
- HIGH is report-only -- gives visibility without blocking
- Trivy supports .trivyignore file for suppressions with documented rationale

---

## 5. Recommended Implementation Order

### PR1: Trivy + SBOM/Provenance + SARIF (items G2a, G2b)

**Scope:**
1. Add provenance: true and sbom: true to docker/build-push-action@v5 in build job
2. Add Trivy scan step after Build and push in build job:
   - Scan the just-pushed image by digest
   - severity CRITICAL,HIGH --exit-code 1 on CRITICAL
   - Output SARIF: trivy-results-${{ matrix.project }}.sarif
3. Upload SARIF to artifact (retention: 30 days)
4. On push to main: also upload SARIF to GitHub Security tab
5. Update build job permissions: add security-events: write, actions: read
6. Add tests: tests/infra/test_group_g_g2_supply_chain.py
7. Add .trivyignore file (empty initially, with comment explaining suppression format)

**Verification:**
- CI build job passes (no CRITICAL CVEs in current images, or fail and assess)
- SBOM visible in ghcr.io image metadata
- Provenance attestation visible in ghcr.io image metadata
- SARIF artifact uploaded
- On main push: SARIF visible in GitHub Security tab -> Code scanning alerts

**Tests:**
- build-push-action has provenance: true and sbom: true
- Trivy scan step exists after build-push
- Trivy uses --exit-code with CRITICAL severity
- SARIF upload step exists
- build job has security-events: write permission
- .trivyignore file exists

### PR2: CodeQL + Dependabot (items G2c, G2d)

**Scope:**
1. Create .github/dependabot.yml with 4 ecosystem entries
2. Create .github/workflows/codeql.yml:
   - On: push to main, PR to main
   - Matrix: javascript-typescript, python
   - Upload SARIF to GitHub Security tab
   - Exclude tests/ and node_modules from analysis paths

**Verification:**
- Dependabot appears in repo Insights -> Dependency graph -> Dependabot
- CodeQL workflow runs on PR and push to main
- CodeQL SARIF visible in GitHub Security tab
- No false-positive noise from test files

**Tests:**
- .github/dependabot.yml exists and is valid YAML
- .github/workflows/codeql.yml exists
- CodeQL workflow has javascript-typescript and python languages
- CodeQL excludes tests/ directories

### Separate Assessment: Node 20 Migration Plan (item G2e)

**Not a PR.** Output: docs/node20-migration-assessment.md (or section in G2 closeout).

Content:
- Next.js 14 Node 22 compatibility confirmation
- Gateway Express/Prisma Node 22 compatibility confirmation
- Docker base image availability
- Recommended migration timeline
- Risk: no code changes, documentation only

---

## 6. Findings Summary

### Blocker (Production Release Gate)

| ID | Finding | Phase |
|---|---|---|
| G-C4 | No Trivy container vulnerability scanning | G2 PR1 |

### Should Fix

| ID | Finding | Phase |
|---|---|---|
| G-C5 | No Dependabot config | G2 PR2 |
| G-C6 | No SBOM/provenance for container images | G2 PR1 |
| G-C7 | No CodeQL/SAST on source code | G2 PR2 |
| G-C3 | Node 20 EOL -- migration plan needed | G2 assessment |

### Defer

| ID | Finding | Reason |
|---|---|---|
| Node 20 -> 22 actual upgrade | Runtime migration | Separate PR after compatibility assessment |
| npm-audit/pip-audit CI gate | Audit step in lint job | Add later; low-noise first, then gate |

---

## 7. Self-Review

### Scope Boundaries Verified

| Check | Status |
|---|---|
| No application logic changes | PASS |
| No deploy changes | PASS |
| No K8s/staging secrets required | PASS |
| No scan warning becomes deploy blocker unless gating strategy says so | PASS |
| Does not change image content | PASS |
| Does not introduce external dependencies that need organization approval | PASS |

### Risk of Premature Hard-Fail

The CRITICAL-only gate for Trivy is intentionally conservative. If the current images
contain CRITICAL CVEs but fixing them requires a multi-day effort, G2 PR1 would block
main pushes. Mitigation: .trivyignore file can suppress known CVEs with documented
rationale. The first Trivy run should be on a PR branch to assess the CVE landscape before
merging.

---

## 8. Decision Points Requiring Confirmation

1. **Trivy CRITICAL fail now, HIGH report-only:** Agree with this threshold?

2. **SBOM format:** SPDX (default for build-push-action) or CycloneDX?

3. **CodeQL query suite:** security-extended (fewer false positives) or
   security-and-quality (more findings)?

4. **Dependabot PR limit:** 5 per ecosystem per week. Acceptable?

5. **Node 20 migration:** Confirm that the actual upgrade stays out of G2 scope.
   The assessment document is the only G2 deliverable for G2e.

6. **PR splitting:** PR1 (Trivy + SBOM/provenance + SARIF) first, PR2 (CodeQL +
   Dependabot) second. Agree?
