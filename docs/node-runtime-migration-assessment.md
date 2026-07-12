# Node.js Runtime Migration Assessment

**Date:** 2026-07-12
**Author:** Group G G2 PR2
**Scope:** Assessment only.  No runtime upgrade is performed in this PR.

## 1. Current State

### CI (actions/setup-node@v4 with node-version: "20")

| File | Count |
|---|---|
| .github/workflows/ci-cd.yml | 5 occurrences (lint, test-gateway, test-agents, migration-runner, integration-tests) |
| .github/workflows/tenant-isolation.yml | 1 occurrence |
| **Total** | **6 occurrences** |

### Dockerfiles

| Dockerfile | Base Image |
|---|---|
| gateway/Dockerfile | node:20-bullseye (both stages) |
| frontend/Dockerfile | node:20-alpine (all 3 stages) |
| database/Dockerfile.migrate | node:20-bullseye-slim |
| agents/Dockerfile | python:3.11-slim (not affected) |

### Runtime Dependencies

| Component | Node 20 Status |
|---|---|
| Next.js 14 | Supports Node 18.17+ |
| Express 4.x | Supports Node 18+ |
| Prisma 5.x | Supports Node 18+ |
| Jest / ts-jest | Supports Node 20+; Node 24 needs verification |

## 2. Node 20 EOL Timeline

- **End-of-Life:** 2026-04-30 (already past)
- **GitHub Actions warning:** actions/setup-node@v4 emits deprecation warnings
- **Security patches:** No longer guaranteed
- **Docker base images:** node:20-* may stop receiving updates

### Risk Level

| Window | Risk |
|---|---|
| 2026-07 (now) | LOW -- community backports still available |
| 2026-10 | MEDIUM -- EOL+6 months; CVE fixes not guaranteed |
| 2027-01 | HIGH -- EOL+9 months; running unpatched Node 20 in production |

## 3. Migration Target: Node 24 LTS

### Why Node 24 (not Node 22)

- Node 24 will be the active LTS when the migration happens (planned Q4 2026)
- Node 22 enters maintenance LTS in 2026-10, making it a short-lived target
- Skipping Node 22 avoids doing the migration twice within a year

### Migration Checklist

#### Phase 1 -- CI Only (lowest risk)

- [ ] Update actions/setup-node@v4 node-version: "20" -> "24" in all workflows
- [ ] Verify lint, type-check, unit tests, migration-runner all pass
- [ ] Verify OPA policy tests pass (setup-opa, not Node-dependent)

#### Phase 2 -- Docker Base Images

- [ ] gateway/Dockerfile: node:20-bullseye -> node:24-bullseye
- [ ] frontend/Dockerfile: node:20-alpine -> node:24-alpine
- [ ] database/Dockerfile.migrate: node:20-bullseye-slim -> node:24-bullseye-slim
- [ ] Verify docker compose build for all images
- [ ] Verify smoke-test and ws-proxy-test in CI pass

#### Phase 3 -- package.json Engines

- [ ] gateway/package.json: engines.node >= 20 -> >= 24
- [ ] frontend/package.json: engines.node >= 20 -> >= 24 (if present)

### Compatibility Risk Assessment

| Component | Risk | Notes |
|---|---|---|
| Next.js 14 | LOW | Node 24 likely supported; Next.js does not pin Node versions tightly |
| Express 4.x | LOW | Stable API; no Node-version-specific features used |
| Prisma 5.x | LOW | Prisma client generation is Node-independent |
| Jest / ts-jest | LOW-MEDIUM | ts-jest may need a version bump; Jest config files may need module resolution updates |
| libssl / OpenSSL | MEDIUM | node:24-* will ship with libssl3 or newer; gateway Dockerfile currently installs libssl1.1 (F-D1) which will be incompatible with Node 24.  libssl1.1 removal is a prerequisite tracked as G-D2 |
| Docker build cache | LOW | BuildKit cache mounts are Docker-native, no Node dependency |
| frontend standalone output | LOW | Next.js standalone output is framework-dependent, not Node-version-dependent |
| Keycloak client auth | LOW | No Node runtime dependency on Keycloak protocol |
| Kafka client | LOW | KafkaJS supports all Node LTS releases |
| ioredis | LOW | Redis client supports Node 18+ |

## 4. GitHub Actions Node 24 Deprecation Warning

### What Will Happen

When GitHub Actions enforces Node 24:
- Jobs using actions/setup-node@v4 with node-version: "20" will emit warnings
- Eventually: jobs may fail if Node 20 is removed from the runner image
- Timeline: GitHub typically gives 12+ months notice for runner OS/tool removals

### Mitigation Before the Warning Becomes an Error

1. Complete the migration checklist phases 1-3 above
2. Use node-version: "lts/*" to track the latest LTS (placeholder)
3. Pin to a specific Node 24.x version after validation

## 5. Decision

**This PR does NOT upgrade Node.js runtime.** The migration checklist above serves as the plan for a separate, focused PR that will:
- Touch all CI workflows and Dockerfiles
- Be tested end-to-end in CI (compose, smoke, ws-proxy)
- Be a single-purpose change, not mixed with security scanning or other hardening

The migration should be completed before 2026-10 to avoid the MEDIUM risk window.

## 6. Relationship to Other Hardening Items

| ID | Item | Depends on Node Mig? |
|---|---|---|
| G-D2 | libssl1.1 removal from gateway | PREREQUISITE -- libssl1.1 incompatible with Node 24 |
| F-D1 | gateway libssl1.1 assessment | PREREQUISITE |
| G3 | K8s migration Job | No |
| G4 | Reliability backlog | No |
| G5 | Frontend test framework | No -- Vitest/Jest run on CI Node, independent of Docker Node |
