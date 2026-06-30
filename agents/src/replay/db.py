from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg


async def create_db_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=10)


@asynccontextmanager
async def tenant_transaction(pool: asyncpg.Pool, tenant_id: UUID):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_id}'")
            yield conn

