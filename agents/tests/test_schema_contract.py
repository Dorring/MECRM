"""Schema contract tests for the agents test database.

These tests verify that the database initialization path used in CI
(Prisma migrate deploy + raw SQL migrations 01-11) produces the schema
that production code expects. They guard against the agents CI path
accidentally running only a subset of migrations.
"""
from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio


@pytest_asyncio.fixture()
async def conn(database_url: str):
    """Yield a raw asyncpg connection to the test database."""
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_core_entity_tables_exist(conn: asyncpg.Connection):
    """All entity tables referenced by structured search must exist."""
    rows = await conn.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY($1)
        """,
        ["leads", "deals", "tickets", "customers"],
    )
    found = {r["table_name"] for r in rows}
    missing = {"leads", "deals", "tickets", "customers"} - found
    assert not missing, f"Missing entity tables: {missing}"


@pytest.mark.asyncio
async def test_core_entity_tables_have_rls_enabled_and_forced(conn: asyncpg.Connection):
    """All core entity tables must have RLS enabled and forced."""
    rows = await conn.fetch(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE relkind = 'r'
          AND relname = ANY($1)
        """,
        ["leads", "deals", "tickets", "customers"],
    )
    found = {r["relname"]: r for r in rows}
    missing = {"leads", "deals", "tickets", "customers"} - set(found)
    assert not missing, f"Missing tables for RLS check: {missing}"

    for name, row in found.items():
        assert row["relrowsecurity"], f"{name} does not have RLS enabled"
        assert row["relforcerowsecurity"], f"{name} does not have RLS forced"


@pytest.mark.asyncio
async def test_required_entity_indexes_exist(conn: asyncpg.Connection):
    """Structured search relies on tenant-scoped indexes for performance."""
    rows = await conn.fetch(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = ANY($1)
        """,
        ["deals_tenant_id_stage_idx", "tickets_tenant_id_status_idx"],
    )
    found = {r["indexname"] for r in rows}
    missing = {"deals_tenant_id_stage_idx", "tickets_tenant_id_status_idx"} - found
    assert not missing, f"Missing required indexes: {missing}"
