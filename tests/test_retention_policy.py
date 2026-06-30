import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from governance.data_erasure import DataErasureService
from governance.retention_policy import RetentionPolicyEngine


@pytest.mark.asyncio
async def test_retention_policy_hard_delete_forgets_expired_customers(database_url: str):
    tenant_id = uuid4()
    old_customer = uuid4()
    new_customer = uuid4()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    old_created = (datetime.now(timezone.utc) - timedelta(days=45)).replace(tzinfo=None)

    conn = await asyncpg.connect(dsn=database_url)
    try:
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, settings, status, created_at, updated_at) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)",
            tenant_id,
            f"tenant-{tenant_id}",
            f"tenant-{tenant_id}",
            "{}",
            "active",
            now,
            now,
        )

        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute(
            """
            INSERT INTO customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, metadata, created_by, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)
            """,
            old_customer,
            tenant_id,
            "Old Person",
            "old@example.com",
            None,
            None,
            None,
            0,
            "active",
            "{}",
            None,
            old_created,
            old_created,
        )
        await conn.execute(
            """
            INSERT INTO customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, metadata, created_by, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)
            """,
            new_customer,
            tenant_id,
            "New Person",
            "new@example.com",
            None,
            None,
            None,
            0,
            "active",
            "{}",
            None,
            now,
            now,
        )
    finally:
        await conn.close()

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=3)
    try:
        erasure = DataErasureService(pool)
        engine = RetentionPolicyEngine(pool, erasure=erasure)
        await engine.set_policy(tenant_id, "customers", 30, hard_delete=True)
        result = await engine.apply_policies()
        assert str(tenant_id) in result["tenants"]

        async with pool.acquire() as c:
            await c.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            old_row = await c.fetchrow("SELECT deletion_type, email FROM customers WHERE tenant_id=$1 AND id=$2", tenant_id, old_customer)
            new_row = await c.fetchrow("SELECT deletion_type, email FROM customers WHERE tenant_id=$1 AND id=$2", tenant_id, new_customer)
            assert old_row["deletion_type"] == "gdpr_forget"
            assert old_row["email"] is None
            assert new_row["deletion_type"] is None
            assert new_row["email"] == "new@example.com"
    finally:
        await pool.close()
