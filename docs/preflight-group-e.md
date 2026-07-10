# Group E Preflight — Helm Chart Audit & Image Governance

**Date:** 2026-07-11  
**Baseline:** `main@2b85ea5` (Group D fully closed)  
**Status:** E1/E2 ✅ merged/stabilized (`main@d6d60cc`, tag `hardening-group-e-e1e2-stabilized`), PR #14 (squash-merge)  
**E3 (digest pinning):** deferred to Phase 4 CI/CD

## Executive Summary

The Helm chart had two production-facing governance gaps:

1. Application image tags defaulted to `latest` in all values files.
2. Ollama was effectively described as enabled by default even though the chart does not deploy Ollama.

The audit also found several dead values that were not consumed by templates and could mislead operators.

## Tag Consistency

Group D stabilized tags are consistent: D1, D2, and D3 all point to their respective merge commits rather than the later closeout-doc commits.

| Tag | Commit | Meaning |
| --- | --- | --- |
| `hardening-group-d-d1-stabilized` | `4336889` | D1 merge commit |
| `hardening-group-d-d2-stabilized` | `4422593` | D2 merge commit |
| `hardening-group-d-d3-stabilized` | `2014732` | D3 merge commit |
| `hardening-group-e-e1e2-stabilized` | `d6d60cc` | E1/E2 merge commit |

No tag movement is required.

## Findings

### Blockers

| ID | Finding | Impact |
| --- | --- | --- |
| E-B1 | `images.frontend.tag`, `images.gateway.tag`, and `images.agents.tag` default to `latest` in base/staging/production values | `latest` + `IfNotPresent` can reuse stale node-local images and is not a reproducible deployment input |
| E-B2 | `ollama.enabled` defaults to true while Helm does not deploy Ollama | Operators can believe Ollama is managed by the chart; agents can receive Ollama config for a service that is not present |

### Should Fix

| ID | Finding |
| --- | --- |
| E-S1 | `opa.*` values are not consumed by templates |
| E-S2 | `weaviate.*` values are not consumed by templates |
| E-S3 | `monitoring.*` values are not consumed by templates |
| E-S4 | `frontend.keycloak*` and `secrets.keycloak.*` are not consumed by templates |

### Deferred

| ID | Finding | Reason |
| --- | --- | --- |
| E-D1 | Compose and Helm topology differ for OPA/Weaviate/Ollama | This is an architecture decision, not a values cleanup |
| E-D2 | Helm templates do not support digest pinning | Requires template helper and CI deploy changes; handle in E3 / Phase 4 |

## Selected Implementation Plan

### E1 — Image Tag Governance

- Change all application image tag defaults from `latest` to `""`.
- Add Helm `required()` guards in `frontend.yaml`, `gateway.yaml`, and `agents.yaml`.
- Update CI Helm lint/template steps to pass explicit test tags.
- Keep deploy jobs passing `${{ github.sha }}`.
- Add static regression tests.

### E2 — Ollama Optional + Dead Values Cleanup

- Set `ollama.enabled: false`.
- Render `OLLAMA_URL` / `OLLAMA_MODEL` only when `.Values.ollama.enabled=true`.
- Remove dead values instead of preserving them as comments.
- Document that OPA, Weaviate, monitoring, and Ollama remain out-of-band unless a future architecture change adds chart support.

### E3 — Digest Pinning

Deferred. Digest pinning should be implemented as a separate change because CI already exports image digests, but deploy jobs still pass tags. A correct implementation should update both Helm templates and CI deploy inputs together.

## Verification Plan

Static tests:

```bash
python -m pytest tests/infra/test_group_e_helm_governance.py -v
python -m pytest tests/infra/test_group_d_image_pinning.py -v
```

Helm validation in CI:

```bash
helm lint deploy/helm/enterprise-crm \
  --set images.frontend.tag=ci-test \
  --set images.gateway.tag=ci-test \
  --set images.agents.tag=ci-test

helm template enterprise-crm deploy/helm/enterprise-crm \
  --set images.frontend.tag=ci-test \
  --set images.gateway.tag=ci-test \
  --set images.agents.tag=ci-test
```

Expected behavior:

- no rendered image uses `latest`
- default render with explicit tags does not include `OLLAMA_URL` or `OLLAMA_MODEL`
- render with `--set ollama.enabled=true` includes `OLLAMA_URL` and `OLLAMA_MODEL`
- rendering without image tags fails fast with `images.<service>.tag is required`
