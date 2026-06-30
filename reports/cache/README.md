# Secure Caching Proof (Phase 7)

This folder contains machine-verifiable artifacts proving tenant-safe, permission-aware caching behavior.

## Artifacts

- `perf_report.json`
- `cache_security_report.json`
  - Proof of tenant cache isolation, permission invalidation, and fail-closed Redis behavior.

## How to regenerate

Tests:

````bash
pytest tests/test_cache_isolation.py -v
pytest tests/test_cache_permission.py -v
pytest tests/test_cache_policy_invalidation.py -v
pytest tests/test_cache_security_report.py -v
pytest tests/test_cache_fail_closed.py -v

Benchmark:

```bash
export REDIS_URL=redis://localhost:6379
export OPA_URL=http://localhost:8181
python scripts/cache_benchmark.py
````
