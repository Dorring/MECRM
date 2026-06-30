import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from replay.db import create_db_pool, tenant_transaction


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
            await _apply_sql(conn, "database/migrations/08-read-models.sql")
    finally:
        await admin_pool.close()

    pool = await create_db_pool(database_url)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_processed_events_dedupes_by_event_id(pool: asyncpg.Pool):
    tenant_id = uuid4()
    event_id = uuid4()

    async with tenant_transaction(pool, tenant_id) as conn:
        a = await conn.execute(
            """
            INSERT INTO processed_events (tenant_id, event_id)
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            event_id,
        )
        b = await conn.execute(
            """
            INSERT INTO processed_events (tenant_id, event_id)
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            event_id,
        )

        assert a.endswith("1")
        assert b.endswith("0")

