from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

import asyncpg


Stage = Literal["awareness", "engaged", "negotiating", "hesitation", "converted", "churn_risk"]


@dataclass(frozen=True)
class StageResult:
    stage: Stage
    confidence: float
    features: dict[str, Any]


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


async def classify_stage(*, tenant_id: str, customer_id: str, conn: asyncpg.Connection) -> StageResult:
    now = _utc_now_dt()

    deals_row = await conn.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE stage = 'closed_won')::int as won_count,
          COUNT(*) FILTER (WHERE stage = 'closed_lost')::int as lost_count,
          MAX(updated_at) FILTER (WHERE stage = 'closed_lost') as last_lost_at,
          (SELECT stage FROM deals WHERE tenant_id = $1::uuid AND customer_id = $2::uuid ORDER BY updated_at DESC LIMIT 1) as latest_stage
        FROM deals
        WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
        """,
        tenant_id,
        customer_id,
    )

    tickets_row = await conn.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE status != 'resolved')::int as open_count,
          COUNT(*) FILTER (WHERE status != 'resolved' AND priority IN ('high','urgent'))::int as open_high_count,
          COUNT(*) FILTER (WHERE status != 'resolved' AND sla_due_at IS NOT NULL AND sla_due_at < now())::int as overdue_count,
          MAX(updated_at) FILTER (WHERE status != 'resolved') as last_open_update_at
        FROM tickets
        WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
        """,
        tenant_id,
        customer_id,
    )

    won_count = int(deals_row["won_count"] or 0) if deals_row else 0
    lost_count = int(deals_row["lost_count"] or 0) if deals_row else 0
    latest_stage = str(deals_row["latest_stage"] or "") if deals_row else ""
    last_lost_at = deals_row["last_lost_at"] if deals_row else None

    open_count = int(tickets_row["open_count"] or 0) if tickets_row else 0
    open_high_count = int(tickets_row["open_high_count"] or 0) if tickets_row else 0
    overdue_count = int(tickets_row["overdue_count"] or 0) if tickets_row else 0
    last_open_update_at = tickets_row["last_open_update_at"] if tickets_row else None

    features: dict[str, Any] = {
        "won_count": won_count,
        "lost_count": lost_count,
        "latest_deal_stage": latest_stage or None,
        "open_ticket_count": open_count,
        "open_high_ticket_count": open_high_count,
        "overdue_ticket_count": overdue_count,
        "last_open_ticket_update_at": (last_open_update_at.isoformat().replace("+00:00", "Z") if last_open_update_at else None),
    }

    if won_count > 0:
        return StageResult(stage="converted", confidence=0.9, features=features)

    if overdue_count > 0 or open_high_count >= 2:
        confidence = 0.85 if overdue_count > 0 else 0.75
        return StageResult(stage="churn_risk", confidence=confidence, features=features)

    if latest_stage in {"negotiation"}:
        return StageResult(stage="negotiating", confidence=0.82, features=features)

    if latest_stage in {"proposal", "qualification"}:
        return StageResult(stage="engaged", confidence=0.75, features=features)

    if latest_stage == "prospecting":
        return StageResult(stage="engaged", confidence=0.68, features=features)

    if lost_count > 0 and last_lost_at and last_lost_at.replace(tzinfo=timezone.utc) > now - timedelta(days=30):
        return StageResult(stage="hesitation", confidence=0.7, features=features)

    if open_count > 0:
        return StageResult(stage="engaged", confidence=0.62, features=features)

    return StageResult(stage="awareness", confidence=0.6, features=features)

