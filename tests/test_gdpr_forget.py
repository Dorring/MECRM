import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from governance.data_erasure import DataErasureService, GovernanceActor, SYSTEM_ACTOR_ID


@pytest.mark.asyncio
async def test_gdpr_forget_customer_erases_pii_and_is_audited(database_url: str):
    tenant_id = uuid4()
    customer_id = uuid4()
    deal_id = uuid4()
    ticket_id = uuid4()
    actor = GovernanceActor(actor_type="user", actor_id=SYSTEM_ACTOR_ID)

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
            INSERT INTO customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, metadata, created_by, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)
            """,
            customer_id,
            tenant_id,
            "Alice Example",
            "alice@example.com",
            "+1-555-0100",
            "ExampleCo",
            "enterprise",
            0,
            "active",
            "{}",
            None,
            now,
            now,
        )

        await conn.execute(
            """
            INSERT INTO deals (id, tenant_id, name, customer_id, stage, currency, probability, metadata, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)
            """,
            deal_id,
            tenant_id,
            "Deal One",
            customer_id,
            "prospecting",
            "USD",
            0,
            "{}",
            now,
            now,
        )

        await conn.execute(
            """
            INSERT INTO tickets (id, tenant_id, subject, description, customer_id, priority, status, metadata, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)
            """,
            ticket_id,
            tenant_id,
            "Customer reported issue",
            "Alice Example called support. Phone +1-555-0100",
            customer_id,
            "medium",
            "open",
            "{}",
            now,
            now,
        )
    finally:
        await conn.close()

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=3)
    try:
        svc = DataErasureService(pool)
        await svc.forget_customer(tenant_id, customer_id, reason="dsar_test", actor=actor)

        async with pool.acquire() as c:
            await c.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            cust = await c.fetchrow(
                "SELECT name, email, phone, deletion_type, deleted_at FROM customers WHERE tenant_id=$1 AND id=$2",
                tenant_id,
                customer_id,
            )
            assert cust is not None
            assert cust["deletion_type"] == "gdpr_forget"
            assert cust["deleted_at"] is not None
            assert cust["name"] == "Deleted Customer"
            assert cust["email"] is None
            assert cust["phone"] is None

            d = await c.fetchrow("SELECT customer_id FROM deals WHERE tenant_id=$1 AND id=$2", tenant_id, deal_id)
            assert d["customer_id"] is None

            t = await c.fetchrow("SELECT customer_id, subject, description FROM tickets WHERE tenant_id=$1 AND id=$2", tenant_id, ticket_id)
            assert t["customer_id"] is None
            assert t["subject"] == "[ERASED]"
            assert t["description"] is None

            audit = await c.fetchrow(
                "SELECT action FROM audit_logs WHERE tenant_id=$1 AND resource_id=$2 ORDER BY created_at DESC LIMIT 1",
                tenant_id,
                customer_id,
            )
            assert audit is not None
            assert audit["action"] == "gdpr.forget_customer"

        out_dir = ROOT / "reports" / "compliance"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "gdpr_forget.json").write_text(
            json.dumps(
                {
                    "phase": "gdpr_forget",
                    "tenant_id": str(tenant_id),
                    "customer_id": str(customer_id),
                    "pii_erased": True,
                    "ai_blocked": None,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        await pool.close()
