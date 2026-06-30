import json
import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio
from aiokafka import AIOKafkaProducer

from replay.replay_service import EventReplayService


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"
    return url


@pytest.fixture(scope="session")
def kafka_brokers() -> str:
    return os.environ.get("KAFKA_BROKERS", "localhost:9094")


@pytest_asyncio.fixture()
async def pool(database_url: str):
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_replay_from_kafka_offset_reconstructs_state(pool: asyncpg.Pool, kafka_brokers: str):
    producer = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","))
    try:
        await producer.start()
    except Exception:
        pytest.skip("Kafka not available")

    tenant_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    lead_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    topic = "crm.leads.events"

    def ce(event_type: str, data: dict) -> dict:
        return {
            "specversion": "1.0",
            "type": event_type,
            "source": "/tests",
            "id": str(uuid.uuid4()),
            "time": datetime.now(timezone.utc).isoformat(),
            "datacontenttype": "application/json",
            "tenantid": str(tenant_id),
            "data": data,
        }

    created = ce(
        "crm.leads.created",
        {
            "aggregate_type": "lead",
            "aggregate_id": str(lead_id),
            "event_type": "lead.created",
            "leadId": str(lead_id),
            "name": "Replay Lead",
            "status": "new",
        },
    )
    updated = ce(
        "crm.leads.updated",
        {
            "aggregate_type": "lead",
            "aggregate_id": str(lead_id),
            "event_type": "lead.updated",
            "leadId": str(lead_id),
            "changes": {"status": "qualified", "score": 77},
        },
    )

    first = await producer.send_and_wait(topic, json.dumps(created).encode("utf-8"), key=str(lead_id).encode("utf-8"))
    await producer.send_and_wait(topic, json.dumps(updated).encode("utf-8"), key=str(lead_id).encode("utf-8"))
    await producer.stop()

    service = EventReplayService(pool=pool, kafka_brokers=kafka_brokers)
    result = await service.replay_from_offset(topic, int(first.offset), tenant_id, "lead", lead_id, partition=int(first.partition))

    assert result.state["status"] == "qualified"
    assert result.state["score"] == 77

