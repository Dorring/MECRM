import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from governance.data_export import DataExportService
from governance.data_erasure import GovernanceActor, SYSTEM_ACTOR_ID


@pytest.mark.asyncio
async def test_data_export_is_tenant_scoped_and_audited(database_url: str):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tenant_a = uuid4()
    tenant_b = uuid4()
    customer_a = uuid4()
    customer_b = uuid4()
    actor = GovernanceActor(actor_type="user", actor_id=SYSTEM_ACTOR_ID)

    conn = await asyncpg.connect(dsn=database_url)
    try:
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, settings, status, created_at, updated_at) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)",
            tenant_a,
            f"tenant-{tenant_a}",
            f"tenant-{tenant_a}",
            "{}",
            "active",
            now,
            now,
        )
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, settings, status, created_at, updated_at) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)",
            tenant_b,
            f"tenant-{tenant_b}",
            f"tenant-{tenant_b}",
            "{}",
            "active",
            now,
            now,
        )

        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_a))
        await conn.execute(
            """
            INSERT INTO customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, metadata, created_by, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)
            """,
            customer_a,
            tenant_a,
            "Alice",
            "alice@a.example",
            None,
            "A Co",
            None,
            0,
            "active",
            "{}",
            None,
            now,
            now,
        )

        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_b))
        await conn.execute(
            """
            INSERT INTO customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, metadata, created_by, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)
            """,
            customer_b,
            tenant_b,
            "Bob",
            "bob@b.example",
            None,
            "B Co",
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
        svc = DataExportService(pool)
        exported = await svc.export_customer_data(tenant_a, customer_a, actor=actor)
        assert exported["tenant_id"] == str(tenant_a)
        assert exported["subject_id"] == str(customer_a)

        cust = exported["data"]["customer"]
        assert cust["id"] == str(customer_a)
        assert cust["tenant_id"] == str(tenant_a)
        assert cust["email"] == "alice@a.example"

        serialized = json.dumps(exported)
        assert str(customer_b) not in serialized
        assert "bob@b.example" not in serialized

        async with pool.acquire() as c:
            await c.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_a))
            audit = await c.fetchrow(
                "SELECT action FROM audit_logs WHERE tenant_id=$1 AND resource_id=$2 ORDER BY created_at DESC LIMIT 1",
                tenant_a,
                customer_a,
            )
            assert audit is not None
            assert audit["action"] == "gdpr.export_customer"

        out_dir = ROOT / "reports" / "compliance"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "data_export.json").write_text(
            json.dumps(
                {
                    "phase": "data_export",
                    "tenant_id": str(tenant_a),
                    "customer_id": str(customer_a),
                    "no_cross_tenant_leakage": True,
                    "audit_logged": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        await pool.close()
