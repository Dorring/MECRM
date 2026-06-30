import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
from aiokafka import AIOKafkaProducer

ROOT = Path(__file__).resolve().parents[1]
AGENTS_SRC = ROOT / "agents" / "src"
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from replay.replay_service import EventReplayService
from replay.snapshot_store import save_snapshot


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _ce(tenant_id: str, event_type: str, data: dict) -> dict:
    return {
        "specversion": "1.0",
        "type": event_type,
        "source": "/scripts/benchmark_replay",
        "id": str(uuid.uuid4()),
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json",
        "tenantid": tenant_id,
        "data": data,
    }


async def _produce_events(*, kafka_brokers: str, topic: str, tenant_id: str, aggregate_id: str, count: int) -> tuple[int, int]:
    producer = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","))
    await producer.start()
    try:
        base = datetime.now(timezone.utc)
        first_offset = None
        partition = 0
        for i in range(count):
            if i == 0:
                payload = {
                    "aggregate_type": "lead",
                    "aggregate_id": aggregate_id,
                    "event_type": "lead.created",
                    "leadId": aggregate_id,
                    "name": "Bench Lead",
                    "status": "new",
                }
                msg = _ce(tenant_id, "crm.leads.created", payload)
            else:
                payload = {
                    "aggregate_type": "lead",
                    "aggregate_id": aggregate_id,
                    "event_type": "lead.updated",
                    "leadId": aggregate_id,
                    "changes": {"score": i, "status": "qualified" if i % 2 == 0 else "contacted"},
                }
                msg = _ce(tenant_id, "crm.leads.updated", payload)
                msg["time"] = (base + timedelta(seconds=i)).isoformat()
            meta = await producer.send_and_wait(topic, json.dumps(msg).encode("utf-8"), key=aggregate_id.encode("utf-8"))
            if first_offset is None:
                first_offset = int(meta.offset)
                partition = int(meta.partition)
        return int(first_offset or 0), partition
    finally:
        await producer.stop()


async def main() -> None:
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    kafka_brokers = _env("KAFKA_BROKERS", "localhost:9094")
    topic = os.environ.get("REPLAY_BENCH_TOPIC", "crm.leads.events")
    events_count = int(os.environ.get("REPLAY_BENCH_EVENTS", "500"))
    snapshot_at = int(os.environ.get("REPLAY_BENCH_SNAPSHOT_AT", "400"))

    tenant_id = os.environ.get("REPLAY_BENCH_TENANT_ID", "11111111-1111-4111-8111-111111111111")
    aggregate_id = str(uuid.uuid4())

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        first_offset, partition = await _produce_events(
            kafka_brokers=kafka_brokers,
            topic=topic,
            tenant_id=tenant_id,
            aggregate_id=aggregate_id,
            count=events_count,
        )

        service = EventReplayService(pool=pool, kafka_brokers=kafka_brokers)

        t0 = time.perf_counter()
        full = await service.replay_from_offset(topic, first_offset, uuid.UUID(tenant_id), "lead", uuid.UUID(aggregate_id), partition=partition)
        full_ms = int((time.perf_counter() - t0) * 1000)

        snap_target_time = datetime.now(timezone.utc) + timedelta(seconds=snapshot_at)
        snap_result = await service.replay_to_time(topic, first_offset, snap_target_time, uuid.UUID(tenant_id), "lead", uuid.UUID(aggregate_id), partition=partition)

        await save_snapshot(
            pool,
            tenant_id=uuid.UUID(tenant_id),
            aggregate_type="lead",
            aggregate_id=uuid.UUID(aggregate_id),
            version=snapshot_at,
            ts=snap_target_time,
            state=snap_result.state,
            kafka_topic=topic,
            kafka_partition=partition,
            kafka_offset=(snap_result.job.end_offset or first_offset) + 1,
        )

        t1 = time.perf_counter()
        snap = await service.replay_from_offset(topic, int((snap_result.job.end_offset or first_offset) + 1), uuid.UUID(tenant_id), "lead", uuid.UUID(aggregate_id), partition=partition)
        snap_ms = int((time.perf_counter() - t1) * 1000)

        diff = await service.diff(
            tenant_id=uuid.UUID(tenant_id),
            aggregate_type="lead",
            aggregate_id=uuid.UUID(aggregate_id),
            from_version=1,
            to_version=min(events_count, snapshot_at),
        )

        os.makedirs("reports/replay", exist_ok=True)
        with open("reports/replay/replay_benchmark.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "aggregate_id": aggregate_id,
                    "tenant_id": tenant_id,
                    "topic": topic,
                    "partition": partition,
                    "events": events_count,
                    "full_rebuild_time_ms": full_ms,
                    "snapshot_used": True,
                    "snapshot_at_version": snapshot_at,
                    "snapshot_rebuild_time_ms": snap_ms,
                },
                f,
                indent=2,
            )

        with open(f"reports/replay/aggregate_diff_{aggregate_id}.json", "w", encoding="utf-8") as f:
            json.dump(diff, f, indent=2)

        print(json.dumps({"full_ms": full_ms, "snapshot_ms": snap_ms, "aggregate_id": aggregate_id}, indent=2))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

