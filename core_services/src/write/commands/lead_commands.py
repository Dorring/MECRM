from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from ..event_store import EventStore, NewEvent
from ..outbox import OutboxEvent, TransactionalOutbox


@dataclass(frozen=True)
class CreateLeadCommand:
    tenant_id: UUID
    name: str
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class CommandResult:
    aggregate_id: UUID
    version: int


def _cloud_event(*, tenant_id: UUID, event_type: str, event_id: UUID, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "specversion": "1.0",
        "type": f"crm.{event_type}",
        "source": "/core_services/commands",
        "id": str(event_id),
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json",
        "tenantid": str(tenant_id),
        "data": data,
    }


async def create_lead(
    conn: asyncpg.Connection,
    *,
    store: EventStore,
    outbox: TransactionalOutbox,
    cmd: CreateLeadCommand,
) -> CommandResult:
    name = (cmd.name or "").strip()
    if not name:
        raise ValueError("name is required")

    if cmd.idempotency_key:
        existing = await conn.fetchrow(
            """
            SELECT stream_id, max(version) AS max_version
            FROM events
            WHERE tenant_id = $1 AND idempotency_key = $2
            GROUP BY stream_id
            """,
            cmd.tenant_id,
            cmd.idempotency_key,
        )
        if existing and existing["stream_id"] is not None and existing["max_version"] is not None:
            stream_id = str(existing["stream_id"])
            if stream_id.startswith("lead:"):
                return CommandResult(aggregate_id=UUID(stream_id.split(":", 1)[1]), version=int(existing["max_version"]))

    lead_id = uuid4()
    stream_id = f"lead:{lead_id}"

    domain_event = NewEvent(
        event_type="lead.created",
        payload={
            "leadId": str(lead_id),
            "name": name,
            "email": cmd.email,
            "phone": cmd.phone,
            "company": cmd.company,
            "status": "new",
        },
        schema_version=1,
    )

    version = await store.append_in_transaction(
        conn,
        tenant_id=cmd.tenant_id,
        stream_id=stream_id,
        events=[domain_event],
        expected_version=0,
        idempotency_key=cmd.idempotency_key,
    )

    outbox_payload = _cloud_event(
        tenant_id=cmd.tenant_id,
        event_type="leads.created",
        event_id=domain_event.event_id,
        data={
            "aggregate_type": "lead",
            "aggregate_id": str(lead_id),
            "event_type": "lead.created",
            "version": version,
            "schema_version": domain_event.schema_version,
            "payload": domain_event.payload,
        },
    )

    await outbox.enqueue_in_transaction(
        conn,
        items=[
            OutboxEvent(
                tenant_id=cmd.tenant_id,
                event_id=domain_event.event_id,
                event_type="lead.created",
                topic="crm.leads.events",
                payload=outbox_payload,
                schema_version=1,
                idempotency_key=cmd.idempotency_key,
            )
        ],
    )

    return CommandResult(aggregate_id=lead_id, version=version)

