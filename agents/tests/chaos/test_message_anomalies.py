import json
import os
import time
import uuid

import asyncpg
import pytest
from aiokafka import AIOKafkaProducer

from .utils import compose, endpoints


def _skip_if_disabled():
    if os.getenv("CHAOS_TESTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        pytest.skip("CHAOS_TESTS_ENABLED is not true")
    if os.getenv("CHAOS_ENVIRONMENT", "").lower() not in ("local", "ci", "staging"):
        pytest.skip("CHAOS_ENVIRONMENT not in {local,ci,staging}")


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_events_converge():
    _skip_if_disabled()

    compose_file = "docker-compose.chaos.yml"
    compose(compose_file, ["up", "-d", "--build"], timeout=900)

    ep = endpoints()
    tenant_id = str(uuid.uuid4())
    aggregate_id = str(uuid.uuid4())
    topic = f"chaos.projection.anomalies.{uuid.uuid4()}"
    group_id = f"chaos-anomalies-{uuid.uuid4()}"

    pool = await asyncpg.create_pool(dsn=ep.postgres_dsn, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chaos_processed_events (
                  tenant_id uuid NOT NULL,
                  event_id uuid NOT NULL,
                  processed_at timestamptz NOT NULL DEFAULT now(),
                  PRIMARY KEY (tenant_id, event_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chaos_projection (
                  tenant_id uuid NOT NULL,
                  aggregate_id uuid NOT NULL,
                  current_version integer NOT NULL DEFAULT 0,
                  current_value text NULL,
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  PRIMARY KEY (tenant_id, aggregate_id)
                )
                """
            )

        producer = AIOKafkaProducer(bootstrap_servers=ep.kafka_brokers.split(","))
        await producer.start()
        try:
            e2 = str(uuid.uuid4())
            events = [
                {"event_id": str(uuid.uuid4()), "version": 1, "value": "v1"},
                {"event_id": e2, "version": 2, "value": "v2"},
                {"event_id": str(uuid.uuid4()), "version": 3, "value": "v3"},
                {"event_id": e2, "version": 2, "value": "v2-duplicate"},
                {"event_id": str(uuid.uuid4()), "version": 5, "value": "v5"},
                {"event_id": str(uuid.uuid4()), "version": 4, "value": "v4-out-of-order"},
            ]
            for ev in events:
                payload = {"tenant_id": tenant_id, "aggregate_id": aggregate_id, **ev}
                await producer.send_and_wait(topic, json.dumps(payload).encode("utf-8"))
        finally:
            await producer.stop()

        worker_path = os.path.join("agents", "tests", "chaos", "consumer_worker.py")
        env = os.environ.copy()
        env.update(
            {
                "CHAOS_DATABASE_URL": ep.postgres_dsn,
                "CHAOS_KAFKA_BROKERS": ep.kafka_brokers,
                "CHAOS_TOPIC": topic,
                "CHAOS_GROUP_ID": group_id,
                "CHAOS_TENANT_ID": tenant_id,
            }
        )

        import subprocess

        proc = subprocess.Popen([os.environ.get("PYTHON", "python"), worker_path], cwd=os.getcwd(), env=env)

        deadline = time.time() + 45
        while time.time() < deadline:
            async with pool.acquire() as conn:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    "SELECT current_version, current_value FROM chaos_projection WHERE tenant_id=$1::uuid AND aggregate_id=$2::uuid",
                    tenant_id,
                    aggregate_id,
                )
                if row and int(row["current_version"]) >= 5:
                    break
            time.sleep(1)
        proc.terminate()
        proc.wait(timeout=30)

        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            row = await conn.fetchrow(
                "SELECT current_version, current_value FROM chaos_projection WHERE tenant_id=$1::uuid AND aggregate_id=$2::uuid",
                tenant_id,
                aggregate_id,
            )
            assert row is not None
            assert int(row["current_version"]) == 5
            assert row["current_value"] == "v5"

            processed = await conn.fetchval(
                "SELECT COUNT(*) FROM chaos_processed_events WHERE tenant_id=$1::uuid",
                tenant_id,
            )
            assert int(processed) == 5
    finally:
        await pool.close()
