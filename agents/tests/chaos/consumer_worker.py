from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from aiokafka import AIOKafkaConsumer
from aiokafka.structs import OffsetAndMetadata, TopicPartition


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    kafka_brokers: str
    topic: str
    group_id: str
    tenant_id: str


def _env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"{name} required")
    return v


def config_from_env() -> WorkerConfig:
    return WorkerConfig(
        database_url=_env("CHAOS_DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"),
        kafka_brokers=_env("CHAOS_KAFKA_BROKERS", "localhost:9094"),
        topic=_env("CHAOS_TOPIC"),
        group_id=_env("CHAOS_GROUP_ID", "chaos-consumer"),
        tenant_id=_env("CHAOS_TENANT_ID"),
    )


async def _ensure_tables(conn: asyncpg.Connection) -> None:
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


async def _process(conn: asyncpg.Connection, tenant_id: UUID, raw: dict) -> bool:
    event_id = UUID(raw["event_id"])
    aggregate_id = UUID(raw["aggregate_id"])
    version = int(raw["version"])
    value = str(raw.get("value") or f"v{version}")

    inserted = await conn.execute(
        """
        INSERT INTO chaos_processed_events (tenant_id, event_id)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        tenant_id,
        event_id,
    )
    if not inserted.endswith("1"):
        return True

    row = await conn.fetchrow(
        """
        SELECT current_version
        FROM chaos_projection
        WHERE tenant_id = $1 AND aggregate_id = $2
        """,
        tenant_id,
        aggregate_id,
    )
    current_version = int(row["current_version"]) if row else 0

    if version <= current_version:
        return True

    await conn.execute(
        """
        INSERT INTO chaos_projection (tenant_id, aggregate_id, current_version, current_value)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id, aggregate_id)
        DO UPDATE SET current_version = EXCLUDED.current_version, current_value = EXCLUDED.current_value, updated_at = now()
        """,
        tenant_id,
        aggregate_id,
        version,
        value,
    )
    return True


async def run_worker(cfg: WorkerConfig) -> None:
    running = True

    def stop(*_args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    tenant_id = UUID(cfg.tenant_id)
    pool = await asyncpg.create_pool(dsn=cfg.database_url, min_size=1, max_size=3)
    consumer = AIOKafkaConsumer(
        cfg.topic,
        bootstrap_servers=cfg.kafka_brokers.split(","),
        group_id=cfg.group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda m: m.decode("utf-8"),
    )
    await consumer.start()
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await _ensure_tables(conn)

        async for msg in consumer:
            if not running:
                break
            raw = json.loads(msg.value)
            tp = TopicPartition(msg.topic, msg.partition)

            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                    ok = await _process(conn, tenant_id, raw)
                    if ok:
                        await consumer.commit({tp: OffsetAndMetadata(msg.offset + 1, "")})
    finally:
        await consumer.stop()
        await pool.close()


def main() -> None:
    cfg = config_from_env()
    asyncio.run(run_worker(cfg))


if __name__ == "__main__":
    main()

