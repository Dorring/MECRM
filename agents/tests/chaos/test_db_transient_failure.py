import json
import os
import time
import uuid

import asyncpg
import pytest

from .utils import compose, endpoints, report_dir


def _skip_if_disabled():
    if os.getenv("CHAOS_TESTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        pytest.skip("CHAOS_TESTS_ENABLED is not true")
    if os.getenv("CHAOS_ENVIRONMENT", "").lower() not in ("local", "ci", "staging"):
        pytest.skip("CHAOS_ENVIRONMENT not in {local,ci,staging}")


@pytest.mark.asyncio
async def test_db_transient_failure_breaker_opens_and_recovers():
    _skip_if_disabled()

    compose_file = "docker-compose.chaos.yml"
    compose(compose_file, ["up", "-d", "--build"], timeout=900)

    ep = endpoints()

    from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError
    from resilience.retry_policy import RetryPolicy, RetryExhaustedError, retry_async

    breaker = CircuitBreaker(
        name="chaos-db",
        dependency="postgres",
        tenant_id=str(uuid.uuid4()),
        config=CircuitBreakerConfig(failure_threshold=2, recovery_timeout_seconds=3.0, half_open_max_calls=1, success_threshold=1),
    )
    retry = RetryPolicy(max_retries=3, base_delay_seconds=0.1, max_delay_seconds=1.0, max_elapsed_seconds=3.0, jitter_ratio=0.2)

    async def probe():
        conn = await asyncpg.connect(dsn=ep.postgres_dsn)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

    compose(compose_file, ["stop", "postgres"], timeout=60)
    try:
        opened_at = None
        start = time.monotonic()
        failures = 0
        while time.monotonic() - start < 10:
            try:
                await breaker.call(lambda: retry_async(probe, policy=retry, operation="db_probe", dependency="postgres"))
            except CircuitOpenError:
                if opened_at is None:
                    opened_at = time.monotonic()
                break
            except RetryExhaustedError:
                failures += 1
            except Exception:
                failures += 1
            await asyncio_sleep(0.1)

        assert opened_at is not None
    finally:
        compose(compose_file, ["start", "postgres"], timeout=180)

    recovered_at = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            await breaker.call(lambda: retry_async(probe, policy=retry, operation="db_probe", dependency="postgres"))
            recovered_at = time.monotonic()
            break
        except Exception:
            await asyncio_sleep(0.5)

    assert recovered_at is not None
    recovery_seconds = recovered_at - opened_at
    assert recovery_seconds <= 60

    out = {"breaker_opened": True, "failures_observed": failures, "recovery_time_seconds": recovery_seconds}
    (report_dir() / "db_recovery.json").write_text(json.dumps(out, indent=2))


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)

