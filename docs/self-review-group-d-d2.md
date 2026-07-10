# D2 Self-Review — Image Pinning & OPA Version Convergence

**Date:** 2026-07-10
**Branch:** `codex/group-d-d2-image-pinning` (deleted — squash-merged as #12)
**Baseline:** `main@1b4a689` (D1 stabilized)
**Status:** ✅ **MERGED / STABILIZED**
**Merge commit:** `main@4422593` (squash-merge PR #12)
**Tag:** `hardening-group-d-d2-stabilized`
**CI:** [All checks passed](https://github.com/Dorring/MECRM/actions/runs/PR-12)

---

## 1. OPA Version Convergence

| Source | D1 state | D2 target | Status |
|--------|----------|-----------|--------|
| `docker-compose.yml` | `openpolicyagent/opa:latest` | `openpolicyagent/opa:0.70.0` | Fixed |
| `docker-compose.chaos.yml` | `openpolicyagent/opa:0.55.0` | `openpolicyagent/opa:0.70.0` | Fixed |
| `.github/workflows/ci-cd.yml` | OPA `0.70.0` | unchanged | Aligned |
| `.github/workflows/tenant-isolation.yml` | OPA `0.70.0` | unchanged | Aligned |

Rationale:

- `0.70.0` is already CI-validated by `test-policies` and tenant-isolation workflows.
- The change removes critical-path drift between Compose, chaos Compose, and CI.
- OPA 1.x/Rego v1 migration is out of D2 scope.

---

## 2. Compose Image Pins

### `docker-compose.yml`

| Service | Old | New | Tag existence |
|---------|-----|-----|---------------|
| `postgres-exporter` | `prometheuscommunity/postgres-exporter:latest` | `prometheuscommunity/postgres-exporter:v0.19.1` | Docker Hub API 200 |
| `redis-exporter` | `oliver006/redis_exporter:latest` | `oliver006/redis_exporter:v1.82.0` | Docker Hub API 200 |
| `kafka-exporter` | `danielqsj/kafka-exporter:latest` | `danielqsj/kafka-exporter:v1.9.0` | Docker Hub API 200 |
| `kafka-ui` | `provectuslabs/kafka-ui:latest` | `provectuslabs/kafka-ui:v0.7.2` | Docker Hub tag list; `v1.0.0` returned 404 |
| `opa` | `openpolicyagent/opa:latest` | `openpolicyagent/opa:0.70.0` | Docker Hub API 200 |
| `ollama` | `ollama/ollama:latest` | `ollama/ollama:0.22.1` | Docker Hub API 200; profile-gated |
| `prometheus` | `prom/prometheus:latest` | `prom/prometheus:v3.5.2` | Docker Hub API 200 |
| `grafana` | `grafana/grafana:latest` | `grafana/grafana:13.0.2` | Docker Hub API 200 |
| `loki` | `grafana/loki:latest` | `grafana/loki:3.7.3` | Docker Hub API 200 |

### `docker-compose.chaos.yml`

| Service | Old | New | Tag existence |
|---------|-----|-----|---------------|
| `opa` | `openpolicyagent/opa:0.55.0` | `openpolicyagent/opa:0.70.0` | Docker Hub API 200 |
| `prometheus` | `prom/prometheus:latest` | `prom/prometheus:v3.5.2` | Docker Hub API 200 |
| `grafana` | `grafana/grafana:latest` | `grafana/grafana:13.0.2` | Docker Hub API 200 |

Tag validation note:

- Docker Hub tag existence was checked on 2026-07-10.
- `provectuslabs/kafka-ui:v1.0.0` was rejected because Docker Hub returned 404; `v0.7.2` was selected from the Docker Hub tag list.
- `docker compose config` validates Compose structure, not image platform compatibility or pull success. Full `docker compose pull` remains useful on a Docker-enabled machine, but tag existence is no longer deferred.

---

## 3. Ollama Boundary

Ollama remains optional.

- It is behind the `local-llm` profile.
- The default Compose stack does not pull, install, or start the Ollama image.
- It should only be enabled when a user explicitly requests local Ollama inference.
- This D2 change is only a reproducibility pin; it is not an AI provider migration and does not validate model capability.

---

## 4. Out of Scope

| Item | Reason |
|------|--------|
| Helm `values*.yaml` `tag: latest` | CI deploy overrides app image tags with `${{ github.sha }}`. Manual Helm fallback remains deferred. |
| CI digest-based deploy | Requires digest aggregation across matrix builds and deploy jobs; belongs to later CI/CD hardening. |
| Dockerfiles / image size optimization | Group F scope. D2 only pins external Compose images. |
| Ollama provider/API migration | Separate AI provider adapter work; not part of image pinning. |

---

## 5. Verification Results

| Check | Result |
|-------|--------|
| `python -m pytest tests/infra/test_group_d_image_pinning.py -v` | 10 passed |
| `docker-compose config --quiet` | Passed |
| `docker-compose -f docker-compose.chaos.yml config --quiet` | Passed; obsolete `version` warning only |
| `rg -n "image:\s+\S+:latest\s*$" docker-compose.yml docker-compose.chaos.yml` | No matches |
| OPA version in main compose, chaos compose, CI/CD, tenant-isolation | All `0.70.0` |
| Docker Hub tag existence | Verified via API/tag list as described above |

---

## 6. Exit Gate Assessment

| # | Criterion | Status |
|---|-----------|--------|
| 1 | No `image: ...:latest` in `docker-compose.yml` or `docker-compose.chaos.yml` | Passed |
| 2 | OPA converged to `0.70.0` across Compose and CI | Passed |
| 3 | Static regression tests added and passing | Passed |
| 4 | Compose configs valid | Passed |
| 5 | Helm latest fallback explicitly deferred | Passed |
| 6 | CI digest deploy unchanged and deferred | Passed |
| 7 | Ollama remains optional/profile-gated | Passed |
