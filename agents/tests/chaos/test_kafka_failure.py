import asyncio
import json
import os
import time
import uuid

import asyncpg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .utils import compose, endpoints, report_dir


def _skip_if_disabled():
    import os

    if os.getenv("CHAOS_TESTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        pytest.skip("CHAOS_TESTS_ENABLED is not true")
    if os.getenv("CHAOS_ENVIRONMENT", "").lower() not in ("local", "ci", "staging"):
        pytest.skip("CHAOS_ENVIRONMENT not in {local,ci,staging}")


async def _wait_kafka_ready(brokers: str, *, timeout_seconds: int = 90) -> None:
    deadline = time.time() + timeout_seconds
    last_err: Exception | None = None
    while time.time() < deadline:
        p = AIOKafkaProducer(bootstrap_servers=brokers.split(","))
        try:
            await p.start()
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(2)
        finally:
            try:
                await p.stop()
            except Exception:
                pass
    raise RuntimeError("kafka did not become ready") from last_err


@pytest.mark.asyncio
async def test_kafka_failure_outbox_recovers_without_loss():
    _skip_if_disabled()

    compose_file = "docker-compose.chaos.yml"
    compose(compose_file, ["up", "-d", "--build"], timeout=900)

    ep = endpoints()
    tenant_id = uuid.uuid4()
    event_id = uuid.uuid4()
    topic = f"chaos.outbox.test.{uuid.uuid4()}"

    pool = await asyncpg.create_pool(dsn=ep.postgres_dsn, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_events (
                  id uuid PRIMARY KEY,
                  tenant_id uuid NOT NULL,
                  event_id uuid NOT NULL,
                  event_type text NOT NULL,
                  topic text NOT NULL,
                  payload jsonb NOT NULL,
                  schema_version integer NOT NULL DEFAULT 1,
                  published_at timestamptz NULL,
                  retry_count integer NOT NULL DEFAULT 0,
                  last_error text NULL,
                  idempotency_key text NULL,
                  next_attempt_at timestamptz NOT NULL DEFAULT now(),
                  dead_lettered_at timestamptz NULL,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  UNIQUE (tenant_id, event_id)
                )
                """
            )

            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute("DELETE FROM outbox_events WHERE tenant_id = $1", tenant_id)
            await conn.execute(
                """
                INSERT INTO outbox_events (id, tenant_id, event_id, event_type, topic, payload, schema_version)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
                """,
                uuid.uuid4(),
                tenant_id,
                event_id,
                "chaos.event",
                topic,
                json.dumps({"tenantid": str(tenant_id), "event_id": str(event_id), "data": {"x": 1}}),
                1,
            )
            c = await conn.fetchval("SELECT count(*) FROM outbox_events WHERE tenant_id = $1", tenant_id)
            assert int(c) == 1

        from services import outbox_publisher

        producer = AIOKafkaProducer(bootstrap_servers=ep.kafka_brokers.split(","))
        await producer.start()
        try:
            os.environ["KAFKA_SEND_TIMEOUT_SECONDS"] = "1.0"
            compose(compose_file, ["stop", "kafka"], timeout=120)

            p, r, d = await outbox_publisher._process_tenant(
                pool=pool,
                producer=producer,
                tenant_id=tenant_id,
                batch_size=10,
                max_retries=3,
                backoff_base_ms=50,
                backoff_cap_ms=250,
            )
            assert p == 0
            assert r >= 1
            assert d == 0
        finally:
            await producer.stop()

        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            row = await conn.fetchrow(
                "SELECT retry_count, published_at, dead_lettered_at FROM outbox_events WHERE tenant_id=$1 AND event_id=$2",
                tenant_id,
                event_id,
            )
            assert row is not None
            assert row["published_at"] is None
            assert row["dead_lettered_at"] is None

        compose(compose_file, ["start", "kafka"], timeout=180)
        await _wait_kafka_ready(ep.kafka_brokers)

        producer2 = AIOKafkaProducer(bootstrap_servers=ep.kafka_brokers.split(","))
        try:
            await producer2.start()
            p2, r2, d2 = await outbox_publisher._process_tenant(
                pool=pool,
                producer=producer2,
                tenant_id=tenant_id,
                batch_size=10,
                max_retries=3,
                backoff_base_ms=50,
                backoff_cap_ms=250,
            )
            assert p2 == 1
            assert d2 == 0
            assert r2 == 0
        finally:
            try:
                await producer2.stop()
            except Exception:
                pass

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=ep.kafka_brokers.split(","),
            group_id=f"chaos-verify-{uuid.uuid4()}",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=lambda m: m.decode("utf-8"),
        )
        await consumer.start()
        try:
            msg = await consumer.getone()
            payload = json.loads(msg.value)
            assert payload["event_id"] == str(event_id)
        finally:
            await consumer.stop()

        out = {
            "tenant_id": str(tenant_id),
            "event_id": str(event_id),
            "topic": topic,
            "outbox_published": True,
        }
        (report_dir() / "kafka_recovery.json").write_text(json.dumps(out, indent=2))
    finally:
        await pool.close()
