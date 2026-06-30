import asyncio
import json
import logging
import os
import pathlib
import random
import sys
from typing import Any
from uuid import UUID

import asyncpg
from aiokafka import AIOKafkaProducer

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / "core_services" / "src"))

from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError
from resilience.retry_policy import RetryPolicy, RetryExhaustedError, retry_async


log = logging.getLogger("outbox_publisher")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v is not None else default


def _env_str(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _backoff_ms(retry_count: int, base_ms: int, cap_ms: int) -> int:
    exp = min(cap_ms, base_ms * (2**max(0, retry_count)))
    jitter = random.randint(0, max(1, exp // 10))
    return min(cap_ms, exp + jitter)


def _extract_key(payload: dict[str, Any]) -> bytes | None:
    tenant = payload.get("tenantid")
    data = payload.get("data") or {}
    agg = data.get("aggregate_id") or data.get("aggregateId")
    if tenant and agg:
        return f"{tenant}:{agg}".encode("utf-8")
    return None


async def _fetch_active_tenants(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch("SELECT id FROM tenants WHERE status = 'active'")
    return [r["id"] for r in rows]


async def _process_tenant(
    *,
    pool: asyncpg.Pool,
    producer: AIOKafkaProducer,
    tenant_id: UUID,
    batch_size: int,
    max_retries: int,
    backoff_base_ms: int,
    backoff_cap_ms: int,
) -> tuple[int, int, int]:
    published = 0
    retried = 0
    dead_lettered = 0
    send_timeout_s = float(os.environ.get("KAFKA_SEND_TIMEOUT_SECONDS", "2.0"))
    kafka_breaker = CircuitBreaker(
        name="outbox-publisher",
        dependency="kafka",
        tenant_id=str(tenant_id),
        config=CircuitBreakerConfig(failure_threshold=3, recovery_timeout_seconds=5.0, half_open_max_calls=1, success_threshold=1),
    )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            rows = await conn.fetch(
                """
                SELECT id, event_id, topic, payload, retry_count
                FROM outbox_events
                WHERE published_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND next_attempt_at <= now()
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT $1
                """,
                batch_size,
            )

            for r in rows:
                row_id = r["id"]
                retry_count = int(r["retry_count"])
                topic = r["topic"]
                payload_val = r["payload"]
                if isinstance(payload_val, str):
                    payload = json.loads(payload_val)
                elif isinstance(payload_val, dict):
                    payload = payload_val
                else:
                    payload = dict(payload_val)

                value = json.dumps(payload).encode("utf-8")
                key = _extract_key(payload)

                try:
                    await kafka_breaker.call(lambda: asyncio.wait_for(producer.send_and_wait(topic, value=value, key=key), timeout=send_timeout_s))
                    await conn.execute(
                        """
                        UPDATE outbox_events
                        SET published_at = now(), last_error = NULL
                        WHERE id = $1
                        """,
                        row_id,
                    )
                    published += 1
                except (CircuitOpenError, Exception) as e:
                    next_retry = retry_count + 1
                    if next_retry >= max_retries:
                        await conn.execute(
                            """
                            UPDATE outbox_events
                            SET retry_count = $2,
                                last_error = $3,
                                dead_lettered_at = now()
                            WHERE id = $1
                            """,
                            row_id,
                            next_retry,
                            str(e),
                        )
                        dead_lettered += 1
                    else:
                        delay_ms = _backoff_ms(retry_count, backoff_base_ms, backoff_cap_ms)
                        await conn.execute(
                            """
                            UPDATE outbox_events
                            SET retry_count = $2,
                                last_error = $3,
                                next_attempt_at = now() + ($4::text)::interval
                            WHERE id = $1
                            """,
                            row_id,
                            next_retry,
                            str(e),
                            f"{delay_ms} milliseconds",
                        )
                        retried += 1

    return published, retried, dead_lettered


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    database_url = _env_str("DATABASE_URL")
    kafka_brokers = _env_str("KAFKA_BROKERS", "localhost:9094")

    poll_ms = _env_int("OUTBOX_POLL_INTERVAL_MS", 250)
    batch_size = _env_int("OUTBOX_BATCH_SIZE", 200)
    max_retries = _env_int("OUTBOX_MAX_RETRIES", 10)
    backoff_base_ms = _env_int("OUTBOX_BACKOFF_BASE_MS", 100)
    backoff_cap_ms = _env_int("OUTBOX_BACKOFF_CAP_MS", 10_000)

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=10)
    producer = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","))

    await retry_async(
        producer.start,
        policy=RetryPolicy(max_retries=5, base_delay_seconds=0.2, max_delay_seconds=2.0, max_elapsed_seconds=10.0, jitter_ratio=0.2),
        operation="kafka_start",
        dependency="kafka",
    )
    try:
        tenant_override = os.environ.get("OUTBOX_TENANT_IDS")
        if tenant_override:
            tenants = [UUID(t.strip()) for t in tenant_override.split(",") if t.strip()]
        else:
            async with pool.acquire() as conn:
                tenants = await _fetch_active_tenants(conn)

        while True:
            total_published = 0
            total_retried = 0
            total_dlq = 0

            for tenant_id in tenants:
                p, r, d = await _process_tenant(
                    pool=pool,
                    producer=producer,
                    tenant_id=tenant_id,
                    batch_size=batch_size,
                    max_retries=max_retries,
                    backoff_base_ms=backoff_base_ms,
                    backoff_cap_ms=backoff_cap_ms,
                )
                total_published += p
                total_retried += r
                total_dlq += d

            if total_published or total_retried or total_dlq:
                log.info(
                    "outbox_cycle",
                    extra={"published": total_published, "retried": total_retried, "dead_lettered": total_dlq},
                )

            await asyncio.sleep(poll_ms / 1000)
    finally:
        await producer.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

