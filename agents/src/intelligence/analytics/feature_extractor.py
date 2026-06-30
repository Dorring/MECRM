from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

import asyncpg


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CustomerFeatures:
    values: dict[str, Any]


@dataclass(frozen=True)
class TicketFeatures:
    values: dict[str, Any]


@dataclass(frozen=True)
class LeadFeatures:
    values: dict[str, Any]


async def extract_customer_features(*, tenant_id: str, customer_id: str, conn: asyncpg.Connection) -> CustomerFeatures:
    now = _utc_now_dt()
    since = now - timedelta(days=30)

    deal_row = await conn.fetchrow(
        """
        SELECT
          COUNT(*)::int as deal_count,
          (SELECT stage FROM deals WHERE tenant_id = $1::uuid AND customer_id = $2::uuid ORDER BY updated_at DESC LIMIT 1) as latest_stage,
          (SELECT created_at FROM deals WHERE tenant_id = $1::uuid AND customer_id = $2::uuid ORDER BY created_at DESC LIMIT 1) as latest_deal_created_at
        FROM deals
        WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
        """,
        tenant_id,
        customer_id,
    )

    ticket_row = await conn.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE created_at >= $3)::int as tickets_30d,
          COUNT(*) FILTER (WHERE status != 'resolved')::int as open_tickets,
          COUNT(*) FILTER (WHERE status != 'resolved' AND priority IN ('high','urgent'))::int as open_high_tickets,
          COUNT(*) FILTER (WHERE status != 'resolved' AND sla_due_at IS NOT NULL AND sla_due_at < now())::int as overdue_tickets
        FROM tickets
        WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
        """,
        tenant_id,
        customer_id,
        since,
    )

    latest_stage = str(deal_row["latest_stage"] or "") if deal_row else ""
    latest_deal_created_at = deal_row["latest_deal_created_at"] if deal_row else None

    deal_age_days = None
    if latest_deal_created_at:
        latest_deal_created_at = latest_deal_created_at.replace(tzinfo=timezone.utc)
        deal_age_days = max(0.0, (now - latest_deal_created_at).total_seconds() / 86400.0)

    values = {
        "deal_count": int(deal_row["deal_count"] or 0) if deal_row else 0,
        "latest_deal_stage": latest_stage or None,
        "deal_age_days": deal_age_days,
        "tickets_30d": int(ticket_row["tickets_30d"] or 0) if ticket_row else 0,
        "open_tickets": int(ticket_row["open_tickets"] or 0) if ticket_row else 0,
        "open_high_tickets": int(ticket_row["open_high_tickets"] or 0) if ticket_row else 0,
        "overdue_tickets": int(ticket_row["overdue_tickets"] or 0) if ticket_row else 0,
    }
    return CustomerFeatures(values=values)


async def extract_ticket_features(*, tenant_id: str, ticket_id: str, conn: asyncpg.Connection) -> TicketFeatures:
    row = await conn.fetchrow(
        """
        SELECT
          id::text as ticket_id,
          priority,
          status,
          created_at,
          updated_at,
          sla_due_at
        FROM tickets
        WHERE tenant_id = $1::uuid AND id = $2::uuid
        """,
        tenant_id,
        ticket_id,
    )
    if not row:
        return TicketFeatures(values={"ticket_id": ticket_id, "missing": True})

    now = _utc_now_dt()
    created_at = row["created_at"].replace(tzinfo=timezone.utc) if row.get("created_at") else None
    sla_due_at = row["sla_due_at"].replace(tzinfo=timezone.utc) if row.get("sla_due_at") else None

    age_hours = None
    if created_at:
        age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)

    time_to_sla_hours = None
    overdue = False
    if sla_due_at:
        dt = (sla_due_at - now).total_seconds() / 3600.0
        time_to_sla_hours = dt
        overdue = dt < 0

    values = {
        "ticket_id": str(row["ticket_id"] or ticket_id),
        "priority": row.get("priority"),
        "status": row.get("status"),
        "age_hours": age_hours,
        "time_to_sla_hours": time_to_sla_hours,
        "overdue": overdue,
    }
    return TicketFeatures(values=values)


async def extract_lead_features(*, tenant_id: str, lead_id: str, conn: asyncpg.Connection) -> LeadFeatures:
    row = await conn.fetchrow(
        """
        SELECT
          id::text as lead_id,
          status,
          created_at,
          updated_at,
          assigned_to::text as assigned_to
        FROM leads
        WHERE tenant_id = $1::uuid AND id = $2::uuid
        """,
        tenant_id,
        lead_id,
    )
    if not row:
        return LeadFeatures(values={"lead_id": lead_id, "missing": True})

    now = _utc_now_dt()
    created_at = row["created_at"].replace(tzinfo=timezone.utc) if row.get("created_at") else None
    age_days = None
    if created_at:
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)

    values = {
        "lead_id": str(row["lead_id"] or lead_id),
        "status": row.get("status"),
        "age_days": age_days,
        "assigned_to": row.get("assigned_to"),
    }
    return LeadFeatures(values=values)

