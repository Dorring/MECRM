import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_SRC = ROOT / "agents" / "src"
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from governance.data_guard import DataGuard, DataGovernanceBlocked


@pytest.mark.asyncio
async def test_ai_data_guard_blocks_soft_deleted_subjects_and_audits(database_url: str):
    tenant_id = uuid4()
    customer_id = uuid4()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

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
            INSERT INTO customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, deleted_at, deletion_type, metadata, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14)
            """,
            customer_id,
            tenant_id,
            "Soft Deleted",
            "soft@example.com",
            None,
            None,
            None,
            0,
            "deleted",
            now,
            "soft",
            "{}",
            now,
            now,
        )
    finally:
        await conn.close()

    guard = DataGuard(database_url)
    await guard.start()
    try:
        with pytest.raises(DataGovernanceBlocked) as e:
            await guard.ensure_allowed(tenant_id=str(tenant_id), agent_id="sales-agent", customer_id=str(customer_id))
        assert e.value.block.reason == "soft_deleted"
    finally:
        await guard.close()

    conn2 = await asyncpg.connect(dsn=database_url)
    try:
        await conn2.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        row = await conn2.fetchrow(
            "SELECT action FROM audit_logs WHERE tenant_id=$1 AND resource_id=$2 ORDER BY created_at DESC LIMIT 1",
            tenant_id,
            customer_id,
        )
        assert row is not None
        assert row["action"] == "ai.data_access_violation"
    finally:
        await conn2.close()
