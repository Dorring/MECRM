import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import asyncpg


REPO_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _apply_event(state: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
    if event_type == "lead.created":
        state.update(payload)
    elif event_type == "lead.updated":
        state.update(payload.get("changes") or {})


async def main() -> None:
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    tenant_id = os.environ.get("TENANT_ID")

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
    try:
        async with pool.acquire() as conn:
            if tenant_id:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)

            streams = await conn.fetch(
                """
                SELECT tenant_id, stream_id
                FROM event_streams
                WHERE stream_id LIKE 'lead:%'
                ORDER BY updated_at DESC
                """
            )

        t0 = time.perf_counter()
        processed_events = 0
        processed_streams = 0

        async with pool.acquire() as conn:
            if tenant_id:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)

            for s in streams:
                tid = s["tenant_id"]
                stream_id = s["stream_id"]
                lead_id = stream_id.split(":", 1)[1]

                rows = await conn.fetch(
                    """
                    SELECT version, event_type, payload
                    FROM events
                    WHERE tenant_id = $1 AND stream_id = $2
                    ORDER BY version ASC
                    """,
                    tid,
                    stream_id,
                )

                state: dict[str, Any] = {}
                last_version = 0
                for r in rows:
                    payload_val = r["payload"]
                    if isinstance(payload_val, str):
                        payload = json.loads(payload_val)
                    elif isinstance(payload_val, dict):
                        payload = payload_val
                    else:
                        payload = dict(payload_val)
                    _apply_event(state, r["event_type"], payload)
                    last_version = int(r["version"])
                    processed_events += 1

                if last_version == 0:
                    continue

                await conn.execute(
                    """
                    INSERT INTO lead_read_model (tenant_id, lead_id, name, email, phone, company, status, score, assigned_to, metadata, version, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,now())
                    ON CONFLICT (tenant_id, lead_id)
                    DO UPDATE SET
                      name = EXCLUDED.name,
                      email = EXCLUDED.email,
                      phone = EXCLUDED.phone,
                      company = EXCLUDED.company,
                      status = EXCLUDED.status,
                      score = EXCLUDED.score,
                      assigned_to = EXCLUDED.assigned_to,
                      metadata = EXCLUDED.metadata,
                      version = EXCLUDED.version,
                      updated_at = now()
                    """,
                    tid,
                    lead_id,
                    state.get("name") or "",
                    state.get("email"),
                    state.get("phone"),
                    state.get("company"),
                    state.get("status") or "new",
                    state.get("score"),
                    state.get("assignedTo") or state.get("assigned_to"),
                    json.dumps(state.get("metadata") or {}),
                    last_version,
                )
                processed_streams += 1

        total_ms = int((time.perf_counter() - t0) * 1000)
        report = {
            "streams_rebuilt": processed_streams,
            "events_processed": processed_events,
            "rebuild_time_ms": total_ms,
        }

        os.makedirs("reports/cqrs", exist_ok=True)
        with open("reports/cqrs/rebuild_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(json.dumps(report, indent=2))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

