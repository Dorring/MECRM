import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import asyncpg
from aiokafka import AIOKafkaProducer


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
AGENTS_SRC = ROOT / "agents" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from write.db import tenant_transaction
from write.event_store import EventStore
from write.outbox import TransactionalOutbox
from write.commands.lead_commands import CreateLeadCommand, create_lead
from projections.projector import run_projector
from services import outbox_publisher


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


async def _wait_for_kafka(kafka_brokers: str, *, timeout_seconds: int = 90) -> None:
    deadline = time.time() + timeout_seconds
    last_err: Exception | None = None
    while time.time() < deadline:
        producer = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","))
        try:
            await producer.start()
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(2)
        finally:
            try:
                await producer.stop()
            except Exception:
                pass
    raise RuntimeError("kafka did not become ready") from last_err


async def _apply_sql(admin_conn: asyncpg.Connection, rel_path: str) -> None:
    sql = (ROOT / rel_path).read_text(encoding="utf-8")
    await admin_conn.execute(sql)


async def _ensure_schema(*, admin_dsn: str) -> None:
    conn = await asyncpg.connect(dsn=admin_dsn)
    try:
        await _apply_sql(conn, "database/migrations/06-event-store.sql")
        await _apply_sql(conn, "database/migrations/07-outbox.sql")
        await _apply_sql(conn, "database/migrations/08-read-models.sql")
    finally:
        await conn.close()


async def _count_read_model(pool: asyncpg.Pool, tenant_id: UUID, lead_id: UUID) -> int:
    async with tenant_transaction(pool, tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT count(*) AS c FROM lead_read_model WHERE tenant_id=$1 AND lead_id=$2",
            tenant_id,
            lead_id,
        )
        return int(row["c"]) if row else 0


async def _outbox_row(pool: asyncpg.Pool, tenant_id: UUID, event_id: UUID) -> dict:
    async with tenant_transaction(pool, tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, retry_count, published_at, created_at, last_error, dead_lettered_at
            FROM outbox_events
            WHERE tenant_id=$1 AND event_id=$2
            """,
            tenant_id,
            event_id,
        )
        if not row:
            return {}
        out = dict(row)
        for k, v in list(out.items()):
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
        return out


async def main() -> int:
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    admin_database_url = _env("ADMIN_DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm")
    kafka_brokers = _env("KAFKA_BROKERS", "localhost:9094")

    await _ensure_schema(admin_dsn=admin_database_url)

    tenant_id = UUID(os.environ.get("OUTBOX_TEST_TENANT_ID", str(uuid.uuid4())))

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
    producer = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","))
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                store = EventStore(pool)
                outbox = TransactionalOutbox()
                cmd = CreateLeadCommand(tenant_id=tenant_id, name="Outbox Proof Lead", idempotency_key=f"outbox-proof-{uuid.uuid4()}")
                result = await create_lead(conn, store=store, outbox=outbox, cmd=cmd)
                lead_id = result.aggregate_id

        async with tenant_transaction(pool, tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT event_id FROM events WHERE tenant_id=$1 AND stream_id=$2 AND version=1",
                tenant_id,
                f"lead:{lead_id}",
            )
            if not row or not row.get("event_id"):
                raise RuntimeError("missing event_id in events table")
            event_id = UUID(str(row["event_id"]))

        assert await _count_read_model(pool, tenant_id, lead_id) == 0

        docker_down = int(os.environ.get("OUTBOX_TEST_STOP_KAFKA", "1")) == 1
        kafka_stopped = False
        await producer.start()
        try:
            if docker_down:
                os.system("docker compose stop kafka > NUL 2>&1")
                kafka_stopped = True

            p1, r1, d1 = await outbox_publisher._process_tenant(
                pool=pool,
                producer=producer,
                tenant_id=tenant_id,
                batch_size=10,
                max_retries=3,
                backoff_base_ms=50,
                backoff_cap_ms=250,
            )
        finally:
            await producer.stop()

        row_after_fail = await _outbox_row(pool, tenant_id, event_id)

        if kafka_stopped:
            os.system("docker compose start kafka > NUL 2>&1")
            await _wait_for_kafka(kafka_brokers, timeout_seconds=90)

        producer2 = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","))
        try:
            await producer2.start()
            p2, r2, d2 = await outbox_publisher._process_tenant(
                pool=pool,
                producer=producer2,
                tenant_id=tenant_id,
                batch_size=10,
                max_retries=3,
                backoff_base_ms=50,
                backoff_cap_ms=250,
            )
        finally:
            await producer2.stop()

        row_after_success = await _outbox_row(pool, tenant_id, event_id)

        os.environ["DATABASE_URL"] = database_url
        os.environ["KAFKA_BROKERS"] = kafka_brokers
        os.environ["PROJECTION_TOPICS"] = "crm.leads.events"
        os.environ["PROJECTION_GROUP_ID"] = f"cqrs-projector-proof-{uuid.uuid4()}"
        os.environ["PROJECTOR_MAX_MESSAGES"] = "1"
        os.environ["PROJECTOR_TENANT_ID"] = str(tenant_id)
        os.environ["REPO_ROOT"] = str(ROOT)

        await run_projector()

        read_model_count = await _count_read_model(pool, tenant_id, lead_id)

        async with tenant_transaction(pool, tenant_id) as conn:
            lag_row = await conn.fetchrow(
                """
                SELECT pe.processed_at - o.created_at AS lag
                FROM outbox_events o
                JOIN processed_events pe
                  ON pe.tenant_id = o.tenant_id
                 AND pe.event_id = o.event_id
                WHERE o.tenant_id=$1 AND o.event_id=$2
                """,
                tenant_id,
                event_id,
            )
            lag_seconds = lag_row["lag"].total_seconds() if lag_row and lag_row["lag"] else None

        report = {
            "phase": "cqrs_outbox_reliability",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tenant_id": str(tenant_id),
            "lead_id": str(lead_id),
            "event_id": str(event_id),
            "kafka_down_injected": kafka_stopped,
            "publish_attempt_while_down": {"published": p1, "retried": r1, "dead_lettered": d1},
            "publish_attempt_after_recovery": {"published": p2, "retried": r2, "dead_lettered": d2},
            "outbox_row_after_down": row_after_fail,
            "outbox_row_after_recovery": row_after_success,
            "read_model_created_after_projection": read_model_count == 1,
            "projection_lag_seconds": lag_seconds,
        }

        os.makedirs("reports/cqrs", exist_ok=True)
        with open("reports/cqrs/outbox-reliability-report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        print(json.dumps(report, indent=2))

        if kafka_stopped:
            assert p1 == 0 and d1 == 0
        assert p2 >= 1 and d2 == 0
        assert report["read_model_created_after_projection"] is True

        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

