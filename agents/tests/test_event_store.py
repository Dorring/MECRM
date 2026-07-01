import os
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

# P1-11: this is an integration test for the core_services event store. The
# `write` package lives in core_services/src, which is deliberately NOT on the
# global sys.path (it ships a conflicting `governance` package). Append it
# here (not insert) so `governance` still resolves to the agents package first,
# while `write` resolves to core_services. Scoped to this test module only.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_SRC = _REPO_ROOT / "core_services" / "src"
if _CORE_SRC.is_dir() and str(_CORE_SRC) not in sys.path:
    sys.path.append(str(_CORE_SRC))

from write.db import create_db_pool, tenant_transaction  # noqa: E402
from write.event_store import ConcurrencyError, EventStore, NewEvent  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]


async def _apply_sql(conn: asyncpg.Connection, rel_path: str) -> None:
    sql = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    await conn.execute(sql)


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")

@pytest.fixture(scope="session")
def admin_database_url() -> str:
    return os.environ.get("ADMIN_DATABASE_URL") or os.environ.get(
        "DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"
    )


@pytest_asyncio.fixture()
async def pool(database_url: str, admin_database_url: str):
    admin_pool = await create_db_pool(admin_database_url)
    try:
        async with admin_pool.acquire() as conn:
            await _apply_sql(conn, "database/migrations/06-event-store.sql")
    finally:
        await admin_pool.close()

    pool = await create_db_pool(database_url)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_append_and_read_stream(pool: asyncpg.Pool):
    store = EventStore(pool)
    tenant_id = uuid4()
    stream_id = f"lead:{uuid4()}"

    v1 = await store.append(
        tenant_id=tenant_id,
        stream_id=stream_id,
        expected_version=0,
        events=[NewEvent(event_type="lead.created", payload={"leadId": stream_id.split(":", 1)[1], "name": "A", "status": "new"})],
    )
    assert v1 == 1

    events = await store.read_stream(tenant_id=tenant_id, stream_id=stream_id, from_version=0)
    assert len(events) == 1
    assert events[0].version == 1
    assert events[0].event_type == "lead.created"


@pytest.mark.asyncio
async def test_optimistic_concurrency_rejects_wrong_expected_version(pool: asyncpg.Pool):
    store = EventStore(pool)
    tenant_id = uuid4()
    stream_id = f"lead:{uuid4()}"

    await store.append(
        tenant_id=tenant_id,
        stream_id=stream_id,
        expected_version=0,
        events=[NewEvent(event_type="lead.created", payload={"leadId": stream_id.split(":", 1)[1], "name": "A", "status": "new"})],
    )

    with pytest.raises(ConcurrencyError):
        await store.append(
            tenant_id=tenant_id,
            stream_id=stream_id,
            expected_version=0,
            events=[NewEvent(event_type="lead.updated", payload={"leadId": stream_id.split(":", 1)[1], "changes": {"status": "qualified"}})],
        )


@pytest.mark.asyncio
async def test_idempotency_key_prevents_duplicates(pool: asyncpg.Pool):
    store = EventStore(pool)
    tenant_id = uuid4()
    stream_id = f"lead:{uuid4()}"
    idem = "idem-1"

    v1 = await store.append(
        tenant_id=tenant_id,
        stream_id=stream_id,
        expected_version=0,
        idempotency_key=idem,
        events=[NewEvent(event_type="lead.created", payload={"leadId": stream_id.split(":", 1)[1], "name": "A", "status": "new"})],
    )
    v2 = await store.append(
        tenant_id=tenant_id,
        stream_id=stream_id,
        expected_version=0,
        idempotency_key=idem,
        events=[NewEvent(event_type="lead.created", payload={"leadId": stream_id.split(":", 1)[1], "name": "A", "status": "new"})],
    )

    assert v1 == v2 == 1

    async with tenant_transaction(pool, tenant_id) as conn:
        row = await conn.fetchrow("SELECT count(*) AS c FROM events WHERE tenant_id = $1 AND stream_id = $2", tenant_id, stream_id)
        assert int(row["c"]) == 1

