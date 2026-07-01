# Infrastructure Config Regression Tests

Regression tests for the batch-1 P0 infrastructure fixes (see
`docs/review-findings-batch1.md`). These tests validate the *configuration files*
themselves and deliberately do NOT require Docker, helm, psql, or a running
cluster -- they parse the YAML / template text directly so they run in any CI
environment, including the restricted local host.

## What is covered

| Test file | P0 | Assertions |
|---|---|---|
| `test_compose_config.py` | P0-2 | `migrate` service uses a `postgres:` image (has psql), mounts `./database/migrations`, applies the full 01-11 SQL sequence with `ON_ERROR_STOP=1`, and depends on `postgres` `service_healthy`. |
| `test_compose_config.py` | P0-5 | `agents` command is `["python","-m","orchestrator.main"]` (matches `agents/Dockerfile` with `PYTHONPATH=/app/src`). |
| `test_compose_config.py` | P0-6 | `replay-service` (and any service using `httpx`) healthcheck checks `r.is_success` and `sys.exit(1)`, not a bare `httpx.get()`. |
| `test_compose_config.py` | JWT | No `JWT_SECRET=supersecret` hardcode and no non-`${}` literal secret value. |
| `test_helm_config.py` | P0-3 | Every `secretKeyRef` `key:` in `gateway.yaml` / `agents.yaml` resolves to a key declared in `values.yaml` (`secrets.<group>.connectionStringKey`); values declares `connectionStringKey` (not the old misnamed `passwordKey`). |

## Running

From the repo root:

```bash
python -m pytest tests/infra/ -v
```

Only dependency: `PyYAML` (already installed in this repo's environment).

## Known limitations

- `docker-compose.yml` uses `${VAR:-default}` interpolation. PyYAML does not
  resolve these, so assertions are on the *literal* strings as written (e.g.
  `JWT_SECRET=${JWT_SECRET:?...}`). This is sufficient to catch the regressions
  these tests guard against. A full validation still requires `docker compose
  config` against a real `.env`, which cannot run on a host without Docker.
- The Helm tests parse template *text* (regex over `secretKeyRef` blocks)
  rather than rendering with `helm template`, because the helm binary is not
  assumed to be present. `helm dependency build` + `helm template` in CI
  (`.github/workflows/ci-cd.yml`) is the authoritative render-time check.
