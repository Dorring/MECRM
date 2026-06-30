import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from replay.snapshot_store import get_latest_snapshot, get_snapshot_at_version, save_snapshot


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
async def test_snapshot_save_and_fetch_latest(pool: asyncpg.Pool):
    tenant_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    aggregate_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    await save_snapshot(
        pool,
        tenant_id=tenant_id,
        aggregate_type="lead",
        aggregate_id=aggregate_id,
        version=3,
        ts=datetime.now(timezone.utc),
        state={"id": str(aggregate_id), "status": "qualified", "score": 95},
        kafka_topic="crm.leads.events",
        kafka_partition=0,
        kafka_offset=42,
    )

    snap = await get_latest_snapshot(pool, tenant_id=tenant_id, aggregate_type="lead", aggregate_id=aggregate_id)
    assert snap is not None
    assert snap.version == 3
    assert snap.state["status"] == "qualified"


@pytest.mark.asyncio
async def test_snapshot_fetch_at_version(pool: asyncpg.Pool):
    tenant_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    aggregate_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    await save_snapshot(
        pool,
        tenant_id=tenant_id,
        aggregate_type="ticket",
        aggregate_id=aggregate_id,
        version=5,
        ts=datetime.now(timezone.utc),
        state={"id": str(aggregate_id), "status": "open"},
        kafka_topic="crm.tickets.events",
        kafka_partition=0,
        kafka_offset=100,
    )
    await save_snapshot(
        pool,
        tenant_id=tenant_id,
        aggregate_type="ticket",
        aggregate_id=aggregate_id,
        version=10,
        ts=datetime.now(timezone.utc),
        state={"id": str(aggregate_id), "status": "resolved"},
        kafka_topic="crm.tickets.events",
        kafka_partition=0,
        kafka_offset=150,
    )

    snap = await get_snapshot_at_version(pool, tenant_id=tenant_id, aggregate_type="ticket", aggregate_id=aggregate_id, version=7)
    assert snap is not None
    assert snap.version == 5

