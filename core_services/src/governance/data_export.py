from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .data_erasure import GovernanceActor, SYSTEM_ACTOR_ID


@dataclass(frozen=True)
class ExportResult:
    tenant_id: UUID
    subject_type: str
    subject_id: UUID
    exported_at: str
    data: dict[str, Any]


class DataExportService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def export_customer_data(self, tenant_id: UUID, customer_id: UUID, *, actor: GovernanceActor | None = None) -> dict[str, Any]:
        actor = actor or GovernanceActor(actor_type="system", actor_id=SYSTEM_ACTOR_ID)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))

                cust = await conn.fetchrow(
                    """
                    SELECT id, tenant_id, name, email, phone, company, segment, lifetime_value, status,
                           created_at, updated_at, deleted_at, deletion_type
                    FROM customers
                    WHERE tenant_id=$1 AND id=$2
                    """,
                    tenant_id,
                    customer_id,
                )
                if not cust:
                    raise ValueError("customer_not_found")

                deals = await conn.fetch(
                    """
                    SELECT id, name, stage, amount, currency, probability, expected_close_date, actual_close_date, won,
                           created_at, updated_at
                    FROM deals
                    WHERE tenant_id=$1 AND customer_id=$2
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    customer_id,
                )

                tickets = await conn.fetch(
                    """
                    SELECT id, subject, description, priority, status, category, sla_due_at, resolved_at, resolution,
                           created_at, updated_at
                    FROM tickets
                    WHERE tenant_id=$1 AND customer_id=$2
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    customer_id,
                )

                audit = await conn.fetch(
                    """
                    SELECT id, actor_type, actor_id, action, resource_type, resource_id, ip_address, user_agent, correlation_id, created_at
                    FROM audit_logs
                    WHERE tenant_id=$1
                      AND resource_id=$2
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    customer_id,
                )

                decisions = await _find_agent_decisions_referencing(conn, tenant_id=tenant_id, needle=str(customer_id))

                out = {
                    "customer": _as_dict(cust),
                    "deals": [_as_dict(r) for r in deals],
                    "tickets": [_as_dict(r) for r in tickets],
                    "audit_logs": [_as_dict(r) for r in audit],
                    "agent_decisions": [_as_dict(r) for r in decisions],
                }

                await _insert_audit_log(
                    conn,
                    tenant_id=tenant_id,
                    actor=actor,
                    action="gdpr.export_customer",
                    resource_type="customer",
                    resource_id=customer_id,
                    new_value={"subject_type": "customer", "subject_id": str(customer_id)},
                )

                return {
                    "tenant_id": str(tenant_id),
                    "subject_type": "customer",
                    "subject_id": str(customer_id),
                    "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "data": out,
                }

    async def export_user_data(self, tenant_id: UUID, user_id: UUID, *, actor: GovernanceActor | None = None) -> dict[str, Any]:
        actor = actor or GovernanceActor(actor_type="system", actor_id=SYSTEM_ACTOR_ID)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))

                user = await conn.fetchrow(
                    """
                    SELECT id, tenant_id, email, name, status, last_login_at, created_at, updated_at, deleted_at, deletion_type
                    FROM users
                    WHERE tenant_id=$1 AND id=$2
                    """,
                    tenant_id,
                    user_id,
                )
                if not user:
                    raise ValueError("user_not_found")

                leads = await conn.fetch(
                    """
                    SELECT id, name, email, phone, company, source, status, score, created_at, updated_at
                    FROM leads
                    WHERE tenant_id=$1 AND (created_by=$2 OR assigned_to=$2)
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    user_id,
                )

                deals = await conn.fetch(
                    """
                    SELECT id, name, stage, amount, currency, probability, created_at, updated_at
                    FROM deals
                    WHERE tenant_id=$1 AND (created_by=$2 OR assigned_to=$2)
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    user_id,
                )

                tickets = await conn.fetch(
                    """
                    SELECT id, subject, description, priority, status, created_at, updated_at
                    FROM tickets
                    WHERE tenant_id=$1 AND (created_by=$2 OR assigned_to=$2)
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    user_id,
                )

                audits = await conn.fetch(
                    """
                    SELECT id, actor_type, actor_id, action, resource_type, resource_id, ip_address, user_agent, correlation_id, created_at
                    FROM audit_logs
                    WHERE tenant_id=$1 AND actor_id=$2
                    ORDER BY created_at ASC
                    """,
                    tenant_id,
                    user_id,
                )

                decisions = await _find_agent_decisions_referencing(conn, tenant_id=tenant_id, needle=str(user_id))

                out = {
                    "user": _as_dict(user),
                    "leads": [_as_dict(r) for r in leads],
                    "deals": [_as_dict(r) for r in deals],
                    "tickets": [_as_dict(r) for r in tickets],
                    "audit_logs": [_as_dict(r) for r in audits],
                    "agent_decisions": [_as_dict(r) for r in decisions],
                }

                await _insert_audit_log(
                    conn,
                    tenant_id=tenant_id,
                    actor=actor,
                    action="gdpr.export_user",
                    resource_type="user",
                    resource_id=user_id,
                    new_value={"subject_type": "user", "subject_id": str(user_id)},
                )

                return {
                    "tenant_id": str(tenant_id),
                    "subject_type": "user",
                    "subject_id": str(user_id),
                    "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "data": out,
                }


async def _find_agent_decisions_referencing(conn: asyncpg.Connection, *, tenant_id: UUID, needle: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, tenant_id, agent_id, action_type, risk_level, status, confidence, approval_id, correlation_id, created_at
        FROM agent_decisions
        WHERE tenant_id=$1 AND (
          input_context::text ILIKE '%' || $2 || '%'
          OR reasoning::text ILIKE '%' || $2 || '%'
          OR evidence::text ILIKE '%' || $2 || '%'
          OR tool_calls::text ILIKE '%' || $2 || '%'
        )
        ORDER BY created_at ASC
        """,
        tenant_id,
        needle,
    )


async def _insert_audit_log(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    actor: GovernanceActor,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    new_value: dict[str, Any] | None,
    correlation_id: UUID | None = None,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await conn.execute(
        """
        INSERT INTO audit_logs (
          id, tenant_id, actor_type, actor_id, action, resource_type, resource_id, old_value, new_value, correlation_id, created_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11)
        """,
        uuid4(),
        tenant_id,
        actor.actor_type,
        actor.actor_id,
        action,
        resource_type,
        resource_id,
        None,
        json.dumps(new_value, separators=(",", ":")) if new_value is not None else None,
        correlation_id,
        now,
    )


def _as_dict(r: asyncpg.Record) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in r.keys():
        v = r[k]
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if v.tzinfo is None else v.isoformat()
        else:
            out[k] = v
    return out
