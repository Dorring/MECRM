# Group E Self-Review — Helm Governance (E1 + E2)

**Date:** 2026-07-11  
**Branch:** `codex/group-e-e1e2-helm-governance`  
**Baseline:** `main@2b85ea5` (Group D fully closed)  
**Status:** Implemented, awaiting review

## Scope

This change closes the Group E E1/E2 findings:

- E1: remove mutable `latest` defaults from Helm application images.
- E2: make Ollama opt-in and remove Helm values that are not consumed by templates.

It intentionally does not implement digest pinning. Digest support requires coordinated changes to Helm image references and CI deploy jobs, so it remains deferred to E3 / Phase 4.

## E1 — Image Tag Governance

### Changes

The following values now default to an empty string instead of `latest`:

- `images.frontend.tag`
- `images.gateway.tag`
- `images.agents.tag`

This was applied consistently in:

- `deploy/helm/enterprise-crm/values.yaml`
- `deploy/helm/enterprise-crm/values-staging.yaml`
- `deploy/helm/enterprise-crm/values-production.yaml`

Each workload template now guards image tags with Helm `required()`:

- `templates/frontend.yaml`: `images.frontend.tag is required`
- `templates/gateway.yaml`: `images.gateway.tag is required`
- `templates/agents.yaml`: `images.agents.tag is required`

This means a plain `helm install` / `helm template` without explicit tags fails before rendering an invalid or stale image reference.

### CI impact

The Helm lint/template job now passes explicit sentinel tags:

```bash
--set images.frontend.tag=ci-test
--set images.gateway.tag=ci-test
--set images.agents.tag=ci-test
```

Staging and production deploy jobs still pass `${{ github.sha }}` for all three image tags.

## E2 — Ollama Optional + Dead Values Cleanup

### Ollama default

`ollama.enabled` now defaults to `false`.

`templates/agents.yaml` only renders these variables when `.Values.ollama.enabled=true`:

- `OLLAMA_URL`
- `OLLAMA_MODEL`

Ollama remains an out-of-band optional GPU capability. The chart does not deploy an Ollama Deployment, Service, or subchart.

### Removed dead values

Removed values that were not consumed by any Helm template:

- `opa.*`
- `weaviate.*`
- `monitoring.*`
- `frontend.keycloakUrl`
- `frontend.keycloakRealm`
- `frontend.keycloakClientId`
- `secrets.keycloak.*`
- `ollama.resources.*`

Rationale: keeping non-consumed values in `values.yaml` makes operators believe the chart controls components that are actually provisioned out of band.

## Regression Tests

Added `tests/infra/test_group_e_helm_governance.py`.

Coverage:

- values files contain no `tag: latest`
- image tags default to `""`
- workload templates use Helm `required()` for image tags
- CI Helm lint/template commands pass explicit `ci-test` tags
- deploy jobs still pass `${{ github.sha }}`
- `ollama.enabled` defaults to `false`
- `OLLAMA_URL` / `OLLAMA_MODEL` are guarded by `.Values.ollama.enabled`
- dead values are removed from base and production values files
- values files parse as valid YAML
- optional real-Helm tests validate fail-fast rendering when `helm` is installed

## Verification

Local verification:

```text
python -m pytest tests/infra/test_group_e_helm_governance.py -v
python -m pytest tests/infra/test_group_d_image_pinning.py -v
python -m pytest tests/infra/test_group_d_health_dependencies.py tests/infra/test_group_d_chaos_cleanup.py tests/infra/test_group_d_image_pinning.py tests/infra/test_group_e_helm_governance.py -q
git diff --check
```

Observed locally:

- Group E tests: passed
- D2 image pinning tests: passed
- D1/D2/D3/E static infra tests: passed
- `helm` binary was not available locally, so real Helm rendering is deferred to the CI Helm job

## Deferred

Digest pinning remains deferred to E3 / Phase 4 because it requires:

- optional digest fields in values
- image reference helper logic in three workload templates
- CI deploy jobs passing digests instead of only `${{ github.sha }}` tags

No Compose files, Dockerfiles, Bitnami chart versions, or OPA/Weaviate/Ollama subcharts were changed in this step.
