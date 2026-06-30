import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
from aiokafka import AIOKafkaProducer

ROOT = Path(__file__).resolve().parents[1]
AGENTS_SRC = ROOT / "agents" / "src"
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from replay.replay_service import EventReplayService


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ce(tenant_id: str, event_type: str, data: dict) -> dict:
    return {
        "specversion": "1.0",
        "type": event_type,
        "source": "/scripts/replay_determinism_report",
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
                    "name": "Determinism Lead",
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


async def main() -> int:
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    kafka_brokers = _env("KAFKA_BROKERS", "localhost:9094")
    topic = os.environ.get("REPLAY_DETERMINISM_TOPIC", "crm.leads.events")
    events_count = int(os.environ.get("REPLAY_DETERMINISM_EVENTS", "200"))

    tenant_id = os.environ.get("REPLAY_DETERMINISM_TENANT_ID", "11111111-1111-4111-8111-111111111111")
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
        run1 = await service.replay_from_offset(topic, first_offset, uuid.UUID(tenant_id), "lead", uuid.UUID(aggregate_id), partition=partition)
        run1_ms = int((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        run2 = await service.replay_from_offset(topic, first_offset, uuid.UUID(tenant_id), "lead", uuid.UUID(aggregate_id), partition=partition)
        run2_ms = int((time.perf_counter() - t1) * 1000)

        h1 = _stable_hash(run1.state)
        h2 = _stable_hash(run2.state)

        report = {
            "phase": "event_replay_determinism",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tenant_id": tenant_id,
            "aggregate_id": aggregate_id,
            "topic": topic,
            "partition": partition,
            "start_offset": first_offset,
            "events": events_count,
            "run1_ms": run1_ms,
            "run2_ms": run2_ms,
            "state_hash_1": h1,
            "state_hash_2": h2,
            "deterministic": h1 == h2,
        }

        os.makedirs("reports/replay", exist_ok=True)
        with open("reports/replay/replay-determinism-report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")

        return 0 if report["deterministic"] else 2
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

