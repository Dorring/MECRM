from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from write.outbox import OutboxEvent, TransactionalOutbox

from cache.secure_cache import SecureCache

from .pii_registry import PIIRegistry


SYSTEM_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000000")


@dataclass(frozen=True)
class GovernanceActor:
    actor_type: str
    actor_id: UUID


class DataErasureService:
    def __init__(self, pool: asyncpg.Pool, *, outbox: TransactionalOutbox | None = None, cache: SecureCache | None = None):
        self._pool = pool
        self._outbox = outbox
        self._cache = cache

    async def forget_customer(self, tenant_id: UUID, customer_id: UUID, *, reason: str, actor: GovernanceActor | None = None) -> None:
        actor = actor or GovernanceActor(actor_type="system", actor_id=SYSTEM_ACTOR_ID)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))

                row = await conn.fetchrow(
                    "SELECT id, tenant_id, deletion_type, deleted_at FROM customers WHERE tenant_id=$1 AND id=$2",
                    tenant_id,
                    customer_id,
                )
                if not row:
                    raise ValueError("customer_not_found")
                if row["deletion_type"] == "gdpr_forget":
                    return

                await conn.execute(
                    """
                    UPDATE customers
                    SET deleted_at = now(),
                        deletion_type = 'gdpr_forget',
                        status = 'deleted',
                        name = 'Deleted Customer',
                        email = NULL,
                        phone = NULL,
                        updated_at = now()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    customer_id,
                )

                await conn.execute(
                    "UPDATE deals SET customer_id = NULL, updated_at = now() WHERE tenant_id=$1 AND customer_id=$2",
                    tenant_id,
                    customer_id,
                )
                await conn.execute(
                    "UPDATE tickets SET subject='[ERASED]', description=NULL, customer_id = NULL, updated_at = now() WHERE tenant_id=$1 AND customer_id=$2",
                    tenant_id,
                    customer_id,
                )

                erased_fields = [f.field for f in PIIRegistry.pii_fields_for_entity(entity_type="customers")]
                await _insert_audit_log(
                    conn,
                    tenant_id=tenant_id,
                    actor=actor,
                    action="gdpr.forget_customer",
                    resource_type="customer",
                    resource_id=customer_id,
                    new_value={"erased_fields": erased_fields, "deletion_type": "gdpr_forget", "reason": reason},
                )

                if self._outbox:
                    await self._outbox.enqueue_in_transaction(
                        conn,
                        items=[
                            OutboxEvent(
                                tenant_id=tenant_id,
                                event_id=uuid4(),
                                event_type="crm.gdpr.forget",
                                topic="crm.gdpr.forget",
                                payload={
                                    "tenantId": str(tenant_id),
                                    "subjectType": "customer",
                                    "subjectId": str(customer_id),
                                    "reason": reason,
                                    "actorType": actor.actor_type,
                                    "actorId": str(actor.actor_id),
                                },
                            )
                        ],
                    )

                if self._cache:
                    await self._cache.invalidate_tenant(str(tenant_id))

    async def forget_user(self, tenant_id: UUID, user_id: UUID, *, reason: str, actor: GovernanceActor | None = None) -> None:
        actor = actor or GovernanceActor(actor_type="system", actor_id=SYSTEM_ACTOR_ID)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))

                row = await conn.fetchrow(
                    "SELECT id, tenant_id, deletion_type, deleted_at FROM users WHERE tenant_id=$1 AND id=$2",
                    tenant_id,
                    user_id,
                )
                if not row:
                    raise ValueError("user_not_found")
                if row["deletion_type"] == "gdpr_forget":
                    return

                await conn.execute(
                    """
                    UPDATE users
                    SET deleted_at = now(),
                        deletion_type = 'gdpr_forget',
                        status = 'deleted',
                        email = CONCAT('deleted+', $2::text, '@example.invalid'),
                        name = 'Deleted User',
                        password_hash = NULL,
                        updated_at = now()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    user_id,
                )

                await conn.execute(
                    "UPDATE leads SET created_by = NULL, updated_at = now() WHERE tenant_id=$1 AND created_by=$2",
                    tenant_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE leads SET assigned_to = NULL, updated_at = now() WHERE tenant_id=$1 AND assigned_to=$2",
                    tenant_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE deals SET created_by = NULL, updated_at = now() WHERE tenant_id=$1 AND created_by=$2",
                    tenant_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE deals SET assigned_to = NULL, updated_at = now() WHERE tenant_id=$1 AND assigned_to=$2",
                    tenant_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE tickets SET created_by = NULL, updated_at = now() WHERE tenant_id=$1 AND created_by=$2",
                    tenant_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE tickets SET assigned_to = NULL, updated_at = now() WHERE tenant_id=$1 AND assigned_to=$2",
                    tenant_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE customers SET created_by = NULL, updated_at = now() WHERE tenant_id=$1 AND created_by=$2",
                    tenant_id,
                    user_id,
                )

                erased_fields = [f.field for f in PIIRegistry.pii_fields_for_entity(entity_type="users")]
                await _insert_audit_log(
                    conn,
                    tenant_id=tenant_id,
                    actor=actor,
                    action="gdpr.forget_user",
                    resource_type="user",
                    resource_id=user_id,
                    new_value={"erased_fields": erased_fields, "deletion_type": "gdpr_forget", "reason": reason},
                )

                if self._outbox:
                    await self._outbox.enqueue_in_transaction(
                        conn,
                        items=[
                            OutboxEvent(
                                tenant_id=tenant_id,
                                event_id=uuid4(),
                                event_type="crm.gdpr.forget",
                                topic="crm.gdpr.forget",
                                payload={
                                    "tenantId": str(tenant_id),
                                    "subjectType": "user",
                                    "subjectId": str(user_id),
                                    "reason": reason,
                                    "actorType": actor.actor_type,
                                    "actorId": str(actor.actor_id),
                                },
                            )
                        ],
                    )

                if self._cache:
                    await self._cache.invalidate_tenant(str(tenant_id))


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
