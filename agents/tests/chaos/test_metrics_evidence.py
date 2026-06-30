import json
import os
import time
import urllib.parse
import urllib.request

import pytest

from .utils import compose, report_dir


def _skip_if_disabled():
    if os.getenv("CHAOS_TESTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        pytest.skip("CHAOS_TESTS_ENABLED is not true")
    if os.getenv("CHAOS_ENVIRONMENT", "").lower() not in ("local", "ci", "staging"):
        pytest.skip("CHAOS_ENVIRONMENT not in {local,ci,staging}")


def _prom_query(expr: str) -> dict:
    base = os.getenv("CHAOS_PROMETHEUS_URL", "http://localhost:9090")
    url = f"{base}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


@pytest.mark.asyncio
async def test_chaos_metrics_visible_and_queryable():
    _skip_if_disabled()

    compose_file = "docker-compose.chaos.yml"
    compose(compose_file, ["up", "-d", "--build"], timeout=900)

    time.sleep(3)

    queries = {
        "circuit_breaker_state": "max(circuit_breaker_state) by (breaker, dependency)",
        "retry_attempts_total": "sum(increase(retry_attempts_total[10m])) by (operation, dependency)",
        "retry_failures_total": "sum(increase(retry_failures_total[10m])) by (operation, dependency)",
        "consumer_lag": "max(consumer_lag) by (group_id, topic)",
        "replay_failures": "sum(increase(replay_failures[10m])) by (component, error_type)",
        "recovery_time_p95": "histogram_quantile(0.95, sum(rate(recovery_time_seconds_bucket[10m])) by (le, breaker, dependency))",
    }

    results: dict[str, dict] = {}
    missing: list[str] = []
    for name, expr in queries.items():
        data = _prom_query(expr)
        results[name] = {"query": expr, "response": data}
        if data.get("status") != "success":
            missing.append(name)

    out = {
        "phase": "chaos_metrics_evidence",
        "timestamp": time.time(),
        "prometheus_url": os.getenv("CHAOS_PROMETHEUS_URL", "http://localhost:9090"),
        "queries": results,
        "query_success": missing == [],
    }
    (report_dir() / "chaos-recovery-report.json").write_text(json.dumps(out, indent=2) + "\n")

    assert missing == []

