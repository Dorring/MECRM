import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = REPO_ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from write.db import create_db_pool, tenant_transaction
from write.event_store import EventStore, NewEvent
from write.outbox import OutboxEvent, TransactionalOutbox


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


@dataclass
class Stats:
    ok: int = 0
    err: int = 0
    lat_ms: list[int] = field(default_factory=list)


async def _write_via_http(client: httpx.AsyncClient, *, tenant_id: str, base_url: str) -> int:
    t0 = time.perf_counter()
    resp = await client.post(
        f"{base_url.rstrip('/')}/commands/leads",
        headers={"X-Tenant-Id": tenant_id},
        json={"name": f"LoadTest-{uuid.uuid4()}", "idempotency_key": str(uuid.uuid4())},
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(resp.text)
    _ = resp.json()
    return int((time.perf_counter() - t0) * 1000)


async def _write_direct(pool: asyncpg.Pool, *, tenant_id: uuid.UUID, store: EventStore, outbox: TransactionalOutbox) -> int:
    t0 = time.perf_counter()
    lead_id = uuid.uuid4()
    stream_id = f"lead:{lead_id}"
    idem = str(uuid.uuid4())
    ev = NewEvent(
        event_type="lead.created",
        payload={"leadId": str(lead_id), "name": f"LoadTest-{lead_id}", "status": "new"},
        schema_version=1,
    )

    async with tenant_transaction(pool, tenant_id) as conn:
        version = await store.append_in_transaction(conn, tenant_id=tenant_id, stream_id=stream_id, events=[ev], expected_version=0, idempotency_key=idem)
        await outbox.enqueue_in_transaction(
            conn,
            items=[
                OutboxEvent(
                    tenant_id=tenant_id,
                    event_id=ev.event_id,
                    event_type="lead.created",
                    topic="crm.leads.events",
                    payload={
                        "specversion": "1.0",
                        "type": "crm.leads.created",
                        "source": "/scripts/load_test_writes",
                        "id": str(ev.event_id),
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "datacontenttype": "application/json",
                        "tenantid": str(tenant_id),
                        "data": {
                            "aggregate_type": "lead",
                            "aggregate_id": str(lead_id),
                            "event_type": "lead.created",
                            "version": version,
                            "schema_version": 1,
                            "payload": ev.payload,
                        },
                    },
                    schema_version=1,
                    idempotency_key=idem,
                )
            ],
        )
    return int((time.perf_counter() - t0) * 1000)


async def main() -> None:
    duration_s = int(os.environ.get("LOAD_DURATION_S", "10"))
    target_rps = int(os.environ.get("TARGET_RPS", "1000"))
    mode = os.environ.get("LOAD_MODE", "http")

    tenant_id = os.environ.get("TENANT_ID", "11111111-1111-4111-8111-111111111111")
    command_api = os.environ.get("COMMAND_API_URL", "http://localhost:5020")
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")

    stats = Stats()
    start = time.perf_counter()
    end = start + duration_s

    if mode == "http":
        async with httpx.AsyncClient() as client:
            while time.perf_counter() < end:
                tick = time.perf_counter()
                batch = target_rps // 10
                results = await asyncio.gather(
                    *[_write_via_http(client, tenant_id=tenant_id, base_url=command_api) for _ in range(batch)],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        stats.err += 1
                    else:
                        stats.ok += 1
                        stats.lat_ms.append(int(r))
                elapsed = time.perf_counter() - tick
                await asyncio.sleep(max(0, 0.1 - elapsed))
    else:
        pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=int(os.environ.get("LOAD_POOL_MAX", "10")))
        store = EventStore(pool)
        outbox = TransactionalOutbox()
        try:
            while time.perf_counter() < end:
                tick = time.perf_counter()
                batch = target_rps // 10
                results = await asyncio.gather(
                    *[_write_direct(pool, tenant_id=uuid.UUID(tenant_id), store=store, outbox=outbox) for _ in range(batch)],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        stats.err += 1
                    else:
                        stats.ok += 1
                        stats.lat_ms.append(int(r))
                elapsed = time.perf_counter() - tick
                await asyncio.sleep(max(0, 0.1 - elapsed))
        finally:
            await pool.close()

    total_s = time.perf_counter() - start
    achieved_rps = stats.ok / total_s if total_s > 0 else 0
    p50 = sorted(stats.lat_ms)[int(len(stats.lat_ms) * 0.5)] if stats.lat_ms else None
    p95 = sorted(stats.lat_ms)[int(len(stats.lat_ms) * 0.95)] if stats.lat_ms else None

    os.makedirs("reports/cqrs", exist_ok=True)
    report = {
        "mode": mode,
        "duration_s": duration_s,
        "target_rps": target_rps,
        "achieved_rps": achieved_rps,
        "ok": stats.ok,
        "errors": stats.err,
        "latency_ms_p50": p50,
        "latency_ms_p95": p95,
    }
    with open("reports/cqrs/load_test_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

