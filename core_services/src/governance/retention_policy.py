from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .data_erasure import DataErasureService, GovernanceActor, SYSTEM_ACTOR_ID


@dataclass(frozen=True)
class DataRetentionPolicy:
    id: UUID
    tenant_id: UUID
    entity_type: str
    retention_days: int
    hard_delete: bool


class RetentionPolicyEngine:
    def __init__(self, pool: asyncpg.Pool, *, erasure: DataErasureService | None = None):
        self._pool = pool
        self._erasure = erasure

    async def set_policy(self, tenant_id: UUID, entity_type: str, days: int, *, hard_delete: bool) -> None:
        if days <= 0:
            raise ValueError("retention_days_must_be_positive")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                existing = await conn.fetchrow(
                    "SELECT id FROM data_retention_policies WHERE tenant_id=$1 AND entity_type=$2",
                    tenant_id,
                    entity_type,
                )
                if existing:
                    await conn.execute(
                        """
                        UPDATE data_retention_policies
                        SET retention_days=$3, hard_delete=$4, updated_at=now()
                        WHERE tenant_id=$1 AND entity_type=$2
                        """,
                        tenant_id,
                        entity_type,
                        int(days),
                        bool(hard_delete),
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO data_retention_policies (id, tenant_id, entity_type, retention_days, hard_delete, updated_at)
                        VALUES ($1,$2,$3,$4,$5,now())
                        """,
                        uuid4(),
                        tenant_id,
                        entity_type,
                        int(days),
                        bool(hard_delete),
                    )

    async def apply_policies(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"tenants": {}}
        async with self._pool.acquire() as conn:
            tenants = await conn.fetch("SELECT id FROM tenants ORDER BY id ASC")

        for t in tenants:
            tenant_id = UUID(str(t["id"]))
            out = await self._apply_for_tenant(tenant_id)
            summary["tenants"][str(tenant_id)] = out
        return summary

    async def _apply_for_tenant(self, tenant_id: UUID) -> dict[str, Any]:
        actor = GovernanceActor(actor_type="system", actor_id=SYSTEM_ACTOR_ID)
        now = datetime.now(timezone.utc)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                policies = await conn.fetch(
                    """
                    SELECT id, tenant_id, entity_type, retention_days, hard_delete
                    FROM data_retention_policies
                    WHERE tenant_id=$1
                    """,
                    tenant_id,
                )

                per_policy: dict[str, Any] = {}
                for p in policies:
                    entity_type = str(p["entity_type"])
                    retention_days = int(p["retention_days"])
                    hard_delete = bool(p["hard_delete"])

                    cutoff = now - timedelta(days=retention_days)
                    if entity_type == "customers":
                        per_policy[entity_type] = await self._apply_customers(conn, tenant_id, cutoff, hard_delete, actor)
                    elif entity_type == "users":
                        per_policy[entity_type] = await self._apply_users(conn, tenant_id, cutoff, hard_delete, actor)
                    else:
                        per_policy[entity_type] = {"error": "unsupported_entity_type"}
                return per_policy

    async def _apply_customers(
        self,
        conn: asyncpg.Connection,
        tenant_id: UUID,
        cutoff: datetime,
        hard_delete: bool,
        actor: GovernanceActor,
    ) -> dict[str, Any]:
        ids = await conn.fetch(
            """
            SELECT id
            FROM customers
            WHERE tenant_id=$1
              AND created_at < $2
              AND (deletion_type IS NULL OR deletion_type != 'gdpr_forget')
            ORDER BY created_at ASC
            """,
            tenant_id,
            cutoff.replace(tzinfo=None),
        )
        affected = 0
        if hard_delete:
            if not self._erasure:
                raise RuntimeError("erasure_service_required_for_hard_delete")
            for r in ids:
                await self._erasure.forget_customer(tenant_id, UUID(str(r["id"])), reason="retention_policy", actor=actor)
                affected += 1
            return {"expired": len(ids), "forgotten": affected}

        if ids:
            await conn.execute(
                """
                UPDATE customers
                SET deleted_at = now(),
                    deletion_type = 'soft',
                    status = 'deleted',
                    updated_at = now()
                WHERE tenant_id=$1
                  AND created_at < $2
                  AND (deletion_type IS NULL OR deletion_type != 'gdpr_forget')
                """,
                tenant_id,
                cutoff.replace(tzinfo=None),
            )
            affected = len(ids)

        await _insert_audit_log(
            conn,
            tenant_id=tenant_id,
            actor=actor,
            action="retention.customers",
            resource_type="customers",
            resource_id=None,
            new_value={"cutoff": cutoff.isoformat(), "hard_delete": hard_delete, "affected": affected},
        )
        return {"expired": len(ids), "soft_deleted": affected}

    async def _apply_users(
        self,
        conn: asyncpg.Connection,
        tenant_id: UUID,
        cutoff: datetime,
        hard_delete: bool,
        actor: GovernanceActor,
    ) -> dict[str, Any]:
        ids = await conn.fetch(
            """
            SELECT id
            FROM users
            WHERE tenant_id=$1
              AND created_at < $2
              AND (deletion_type IS NULL OR deletion_type != 'gdpr_forget')
            ORDER BY created_at ASC
            """,
            tenant_id,
            cutoff.replace(tzinfo=None),
        )
        affected = 0
        if hard_delete:
            if not self._erasure:
                raise RuntimeError("erasure_service_required_for_hard_delete")
            for r in ids:
                await self._erasure.forget_user(tenant_id, UUID(str(r["id"])), reason="retention_policy", actor=actor)
                affected += 1
            return {"expired": len(ids), "forgotten": affected}

        if ids:
            await conn.execute(
                """
                UPDATE users
                SET deleted_at = now(),
                    deletion_type = 'soft',
                    status = 'deleted',
                    updated_at = now()
                WHERE tenant_id=$1
                  AND created_at < $2
                  AND (deletion_type IS NULL OR deletion_type != 'gdpr_forget')
                """,
                tenant_id,
                cutoff.replace(tzinfo=None),
            )
            affected = len(ids)

        await _insert_audit_log(
            conn,
            tenant_id=tenant_id,
            actor=actor,
            action="retention.users",
            resource_type="users",
            resource_id=None,
            new_value={"cutoff": cutoff.isoformat(), "hard_delete": hard_delete, "affected": affected},
        )
        return {"expired": len(ids), "soft_deleted": affected}


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
