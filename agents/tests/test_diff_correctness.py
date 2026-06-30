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
async def test_diff_between_versions_contains_expected_changed_keys(pool: asyncpg.Pool):
    tenant_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    aggregate_id = uuid.uuid4()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_id}'")
            await conn.execute(
                """
                INSERT INTO event_log (id, event_id, tenant_id, aggregate_type, aggregate_id, event_type, version, ts, payload)
                VALUES
                  ($1,$2,$3,'lead',$4,'lead.created',1,$5,$6),
                  ($7,$8,$3,'lead',$4,'lead.updated',2,$9,$10),
                  ($11,$12,$3,'lead',$4,'lead.updated',3,$13,$14)
                ON CONFLICT (event_id) DO NOTHING
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                tenant_id,
                aggregate_id,
                datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                json.dumps({"leadId": str(aggregate_id), "name": "Alice", "status": "new"}),
                uuid.uuid4(),
                uuid.uuid4(),
                datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
                json.dumps({"leadId": str(aggregate_id), "changes": {"status": "contacted"}}),
                uuid.uuid4(),
                uuid.uuid4(),
                datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc),
                json.dumps({"leadId": str(aggregate_id), "changes": {"status": "qualified", "score": 88}}),
            )

    service = EventReplayService(pool=pool, kafka_brokers=os.environ.get("KAFKA_BROKERS", "localhost:9094"))
    diff = await service.diff(
        tenant_id=tenant_id,
        aggregate_type="lead",
        aggregate_id=aggregate_id,
        from_version=1,
        to_version=3,
    )

    assert "changed_keys" in diff
    assert "status" in diff["changed_keys"]
    assert "score" in diff["changed_keys"]

