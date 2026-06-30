import asyncio
import json
import os
from pathlib import Path
from statistics import median

import asyncpg


REPO_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


async def main() -> None:
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    tenant_id = os.environ.get("TENANT_ID", "11111111-1111-4111-8111-111111111111")
    limit = int(os.environ.get("LAG_SAMPLE_LIMIT", "200"))

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET app.tenant_id = '{tenant_id}'")

            total_outbox_row = await conn.fetchrow(
                "SELECT count(*) AS c FROM outbox_events WHERE topic = 'crm.leads.events'"
            )
            total_outbox = int(total_outbox_row["c"]) if total_outbox_row else 0

            processed_row = await conn.fetchrow(
                """
                SELECT count(*) AS c
                FROM outbox_events o
                JOIN processed_events pe
                  ON pe.tenant_id = o.tenant_id
                 AND pe.event_id = o.event_id
                WHERE o.topic = 'crm.leads.events'
                """,
            )
            processed_outbox = int(processed_row["c"]) if processed_row else 0
            unprocessed = max(0, total_outbox - processed_outbox)

            sample_rows = await conn.fetch(
                """
                SELECT
                  o.created_at AS outbox_created_at,
                  o.published_at AS published_at,
                  pe.processed_at AS processed_at
                FROM outbox_events o
                LEFT JOIN processed_events pe
                  ON pe.tenant_id = o.tenant_id
                 AND pe.event_id = o.event_id
                WHERE o.topic = 'crm.leads.events'
                ORDER BY o.created_at DESC
                LIMIT $1
                """,
                limit,
            )

            lags = []
            for r in sample_rows:
                if r["processed_at"] is None:
                    continue
                lags.append((r["processed_at"] - r["outbox_created_at"]).total_seconds())

            lags_sorted = sorted(lags)
            p50 = lags_sorted[int(len(lags_sorted) * 0.5)] if lags_sorted else None
            p95 = lags_sorted[int(len(lags_sorted) * 0.95)] if lags_sorted else None

            report = {
                "sample_size": len(sample_rows),
                "lag_samples": len(lags_sorted),
                "unprocessed_events": unprocessed,
                "lag_seconds_median": median(lags_sorted) if lags_sorted else None,
                "lag_seconds_p50": p50,
                "lag_seconds_p95": p95,
            }

            os.makedirs("reports/cqrs", exist_ok=True)
            with open("reports/cqrs/lag_report.json", "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            print(json.dumps(report, indent=2))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

