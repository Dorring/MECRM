import os
import uuid

import asyncpg
import pytest
import pytest_asyncio


def _uuid() -> str:
    return str(uuid.uuid4())


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


@pytest_asyncio.fixture()
async def seeded(pool: asyncpg.Pool):
    tenant_a = _uuid()
    tenant_b = _uuid()
    lead_a = _uuid()
    lead_b = _uuid()
    customer_a = _uuid()
    customer_b = _uuid()
    slug_a = f"tenant-a-{tenant_a[:8]}"
    slug_b = f"tenant-b-{tenant_b[:8]}"

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (id, name, slug, status, created_at, updated_at)
            VALUES ($1, 'Tenant A', $3, 'active', NOW(), NOW()),
                   ($2, 'Tenant B', $4, 'active', NOW(), NOW())
            """,
            tenant_a,
            tenant_b,
            slug_a,
            slug_b,
        )

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a}'")
            await conn.execute(
                """
                INSERT INTO leads (id, tenant_id, name, status, created_at, updated_at)
                VALUES ($1, $2, 'Lead A', 'new', NOW(), NOW())
                """,
                lead_a,
                tenant_a,
            )
            await conn.execute(
                """
                INSERT INTO customers (id, tenant_id, name, status, lifetime_value, created_at, updated_at)
                VALUES ($1, $2, 'Customer A', 'active', 0, NOW(), NOW())
                """,
                customer_a,
                tenant_a,
            )

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_b}'")
            await conn.execute(
                """
                INSERT INTO leads (id, tenant_id, name, status, created_at, updated_at)
                VALUES ($1, $2, 'Lead B', 'new', NOW(), NOW())
                """,
                lead_b,
                tenant_b,
            )
            await conn.execute(
                """
                INSERT INTO customers (id, tenant_id, name, status, lifetime_value, created_at, updated_at)
                VALUES ($1, $2, 'Customer B', 'active', 0, NOW(), NOW())
                """,
                customer_b,
                tenant_b,
            )

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "lead_a": lead_a,
        "lead_b": lead_b,
        "customer_a": customer_a,
        "customer_b": customer_b,
    }


@pytest.mark.asyncio
async def test_rls_select_enforcement(pool: asyncpg.Pool, seeded: dict[str, str]):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{seeded['tenant_a']}'")
            rows = await conn.fetch("SELECT id, tenant_id FROM leads ORDER BY id")
            assert len(rows) == 1
            assert str(rows[0]["tenant_id"]) == seeded["tenant_a"]

            cross = await conn.fetch("SELECT id FROM leads WHERE id = $1", seeded["lead_b"])
            assert cross == []


@pytest.mark.asyncio
async def test_rls_update_blocked_cross_tenant(pool: asyncpg.Pool, seeded: dict[str, str]):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{seeded['tenant_a']}'")
            result = await conn.execute(
                "UPDATE leads SET name = 'PWNED' WHERE id = $1",
                seeded["lead_b"],
            )
            assert result.endswith(" 0")

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{seeded['tenant_b']}'")
            name = await conn.fetchval("SELECT name FROM leads WHERE id = $1", seeded["lead_b"])
            assert name == "Lead B"


@pytest.mark.asyncio
async def test_rls_delete_blocked_cross_tenant(pool: asyncpg.Pool, seeded: dict[str, str]):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{seeded['tenant_a']}'")
            result = await conn.execute("DELETE FROM customers WHERE id = $1", seeded["customer_b"])
            assert result.endswith(" 0")

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{seeded['tenant_b']}'")
            exists = await conn.fetchval(
                "SELECT COUNT(1) FROM customers WHERE id = $1",
                seeded["customer_b"],
            )
            assert exists == 1


@pytest.mark.asyncio
async def test_escape_attempt_sql_injection_like_query_does_not_leak(pool: asyncpg.Pool, seeded: dict[str, str]):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{seeded['tenant_a']}'")
            rows = await conn.fetch(
                f"SELECT id, tenant_id FROM customers WHERE id = '{seeded['customer_b']}' OR '1'='1'"
            )
            assert all(str(r["tenant_id"]) == seeded["tenant_a"] for r in rows)


@pytest.mark.asyncio
async def test_tenant_escape_kill_test_missing_context_fails_closed(pool: asyncpg.Pool, seeded: dict[str, str]):
    async with pool.acquire() as conn:
        with pytest.raises(Exception):
            await conn.fetch("SELECT id FROM leads")

        with pytest.raises(Exception):
            await conn.execute("UPDATE leads SET name = 'X'")

        with pytest.raises(Exception):
            await conn.execute("DELETE FROM customers")

