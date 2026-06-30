import os
import json
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from replay.replay_service import EventReplayService


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"
    return url


@pytest_asyncio.fixture()
async def pool(database_url: str):
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_timeline_is_tenant_scoped(pool: asyncpg.Pool):
    tenant_a = uuid.UUID("11111111-1111-4111-8111-111111111111")
    tenant_b = uuid.UUID("22222222-2222-4222-8222-222222222222")
    lead_a = uuid.uuid4()
    lead_b = uuid.uuid4()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a}'")
            await conn.execute(
                """
                INSERT INTO event_log (id, event_id, tenant_id, aggregate_type, aggregate_id, event_type, version, ts, payload)
                VALUES ($1,$2,$3,'lead',$4,'lead.created',1,$5,$6)
                ON CONFLICT (event_id) DO NOTHING
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                tenant_a,
                lead_a,
                datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                json.dumps({"leadId": str(lead_a), "name": "TenantA Lead", "status": "new"}),
            )

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_b}'")
            await conn.execute(
                """
                INSERT INTO event_log (id, event_id, tenant_id, aggregate_type, aggregate_id, event_type, version, ts, payload)
                VALUES ($1,$2,$3,'lead',$4,'lead.created',1,$5,$6)
                ON CONFLICT (event_id) DO NOTHING
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                tenant_b,
                lead_b,
                datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                json.dumps({"leadId": str(lead_b), "name": "TenantB Lead", "status": "new"}),
            )

    service = EventReplayService(pool=pool, kafka_brokers=os.environ.get("KAFKA_BROKERS", "localhost:9094"))
    a_events = await service.timeline(tenant_id=tenant_a, aggregate_type="lead", aggregate_id=lead_a)
    assert len(a_events) >= 1
    assert all(e["event_type"].startswith("lead.") for e in a_events)

    cross = await service.timeline(tenant_id=tenant_a, aggregate_type="lead", aggregate_id=lead_b)
    assert cross == []

