from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg


@dataclass(frozen=True)
class TimelineEntry:
    tenant_id: str
    customer_id: str
    event_type: str
    event_payload: dict[str, Any]
    timestamp: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def build_timeline_entries(*, tenant_id: str, event: dict[str, Any], conn: asyncpg.Connection) -> list[TimelineEntry]:
    et = str(event.get("type") or "")
    data = event.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    entries: list[TimelineEntry] = []
    ts = str(event.get("time") or "") or _utc_now()

    if et == "crm.customers.created":
        customer_id = str(data.get("customerId") or "")
        if customer_id:
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="customer.created",
                    event_payload={
                        "customer_id": customer_id,
                        "name": data.get("name"),
                        "segment": data.get("segment"),
                    },
                    timestamp=ts,
                )
            )
        return entries

    if et == "crm.customers.updated":
        customer_id = str(data.get("customerId") or "")
        if customer_id:
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="customer.updated",
                    event_payload={"customer_id": customer_id, "changes": data.get("changes") or {}},
                    timestamp=ts,
                )
            )
        return entries

    if et.startswith("crm.tickets."):
        ticket_id = str(data.get("ticketId") or "")
        if not ticket_id:
            return entries
        row = await conn.fetchrow(
            """
            SELECT
              id::text as ticket_id,
              customer_id::text as customer_id,
              subject,
              priority,
              status,
              sla_due_at
            FROM tickets
            WHERE tenant_id = $1::uuid AND id = $2::uuid
            """,
            tenant_id,
            ticket_id,
        )
        customer_id = str(row["customer_id"]) if row and row.get("customer_id") else ""
        if not customer_id:
            return entries

        if et == "crm.tickets.created":
            payload = {
                "ticket_id": ticket_id,
                "subject": row.get("subject") if row else None,
                "priority": row.get("priority") if row else None,
                "sla_due_at": (row.get("sla_due_at").isoformat() if row and row.get("sla_due_at") else None),
            }
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="ticket.created",
                    event_payload=payload,
                    timestamp=ts,
                )
            )
            return entries

        if et == "crm.tickets.updated":
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="ticket.updated",
                    event_payload={"ticket_id": ticket_id, "changes": data.get("changes") or {}},
                    timestamp=ts,
                )
            )
            return entries

        if et == "crm.tickets.resolved":
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="ticket.resolved",
                    event_payload={"ticket_id": ticket_id},
                    timestamp=ts,
                )
            )
            return entries

        return entries

    if et.startswith("crm.deals."):
        deal_id = str(data.get("dealId") or "")
        if not deal_id:
            return entries
        row = await conn.fetchrow(
            """
            SELECT
              id::text as deal_id,
              customer_id::text as customer_id,
              lead_id::text as lead_id,
              name,
              stage,
              amount,
              currency,
              probability,
              expected_close_date,
              actual_close_date,
              won
            FROM deals
            WHERE tenant_id = $1::uuid AND id = $2::uuid
            """,
            tenant_id,
            deal_id,
        )
        customer_id = str(row["customer_id"]) if row and row.get("customer_id") else ""
        if not customer_id:
            return entries

        if et == "crm.deals.created":
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="deal.created",
                    event_payload={"deal_id": deal_id, "name": row.get("name") if row else None, "amount": str(row.get("amount")) if row and row.get("amount") is not None else None},
                    timestamp=ts,
                )
            )
            return entries

        if et == "crm.deals.updated":
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="deal.updated",
                    event_payload={"deal_id": deal_id, "changes": data.get("changes") or {}},
                    timestamp=ts,
                )
            )
            return entries

        if et == "crm.deals.stage-changed":
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="deal.stage_changed",
                    event_payload={
                        "deal_id": deal_id,
                        "previous_stage": data.get("previousStage"),
                        "new_stage": data.get("newStage"),
                        "amount": str(data.get("amount")) if data.get("amount") is not None else None,
                    },
                    timestamp=ts,
                )
            )
            return entries

        if et == "crm.deals.closed":
            entries.append(
                TimelineEntry(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    event_type="deal.closed",
                    event_payload={"deal_id": deal_id, "won": data.get("won"), "amount": str(data.get("amount")) if data.get("amount") is not None else None},
                    timestamp=ts,
                )
            )
            return entries

        return entries

    if et == "crm.approvals.decision":
        decision = str(data.get("decision") or "")
        action_type = str(data.get("actionType") or "")
        target_entity = str(data.get("targetEntity") or "")
        target_id = str(data.get("targetId") or "")
        if not target_entity or not target_id:
            return entries

        customer_id = ""
        if target_entity == "customer":
            customer_id = target_id
        elif target_entity == "ticket":
            row = await conn.fetchrow(
                "SELECT customer_id::text as customer_id FROM tickets WHERE tenant_id = $1::uuid AND id = $2::uuid",
                tenant_id,
                target_id,
            )
            customer_id = str(row["customer_id"]) if row and row.get("customer_id") else ""
        elif target_entity == "deal":
            row = await conn.fetchrow(
                "SELECT customer_id::text as customer_id FROM deals WHERE tenant_id = $1::uuid AND id = $2::uuid",
                tenant_id,
                target_id,
            )
            customer_id = str(row["customer_id"]) if row and row.get("customer_id") else ""

        if not customer_id:
            return entries

        entries.append(
            TimelineEntry(
                tenant_id=tenant_id,
                customer_id=customer_id,
                event_type="approval.decision",
                event_payload={
                    "approval_id": data.get("approvalId"),
                    "decision": decision,
                    "action_type": action_type,
                    "target_entity": target_entity,
                    "target_id": target_id,
                },
                timestamp=ts,
            )
        )
        return entries

    return entries

