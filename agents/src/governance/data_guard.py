from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4
from uuid import uuid5

import asyncpg

from .agent_telemetry import inc_data_governance_violation


@dataclass(frozen=True)
class DataGovernanceBlock:
    reason: str
    subject_type: str
    subject_id: str
    deletion_type: str | None = None


class DataGovernanceBlocked(Exception):
    def __init__(self, block: DataGovernanceBlock):
        super().__init__(block.reason)
        self.block = block


class DataGuard:
    def __init__(self, database_url: str):
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._pool:
                return
            self._pool = await asyncpg.create_pool(dsn=self._database_url, min_size=1, max_size=3)

    async def close(self) -> None:
        async with self._lock:
            if not self._pool:
                return
            await self._pool.close()
            self._pool = None

    async def ensure_allowed(self, *, tenant_id: str, agent_id: str, customer_id: str | None = None, user_id: str | None = None) -> None:
        if not customer_id and not user_id:
            return
        try:
            if not self._pool:
                await self.start()
            assert self._pool
        except Exception:
            await self._audit_violation(
                tenant_id=tenant_id,
                actor_type="agent",
                actor_id=_agent_actor_id(agent_id),
                action="ai.data_access_violation",
                resource_type="unknown",
                resource_id=None,
                details={"reason": "db_unavailable"},
            )
            inc_data_governance_violation(agent_id=agent_id, violation="db_unavailable", subject_type="unknown")
            raise DataGovernanceBlocked(DataGovernanceBlock(reason="db_unavailable", subject_type="unknown", subject_id="unknown"))

        if customer_id:
            await self._ensure_customer_allowed(tenant_id=tenant_id, agent_id=agent_id, customer_id=customer_id)
        if user_id:
            await self._ensure_user_allowed(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)

    async def _ensure_customer_allowed(self, *, tenant_id: str, agent_id: str, customer_id: str) -> None:
        assert self._pool
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            row = await conn.fetchrow(
                "SELECT deletion_type, deleted_at FROM customers WHERE tenant_id=$1::uuid AND id=$2::uuid",
                tenant_id,
                customer_id,
            )
            if not row:
                return
            deletion_type = row["deletion_type"]
            deleted_at = row["deleted_at"]
            if deletion_type == "gdpr_forget":
                await self._audit_violation(
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=_agent_actor_id(agent_id),
                    action="ai.data_access_violation",
                    resource_type="customer",
                    resource_id=UUID(customer_id),
                    details={"reason": "gdpr_forgotten", "subject_type": "customer", "subject_id": customer_id},
                )
                inc_data_governance_violation(agent_id=agent_id, violation="gdpr_forgotten", subject_type="customer")
                raise DataGovernanceBlocked(DataGovernanceBlock(reason="gdpr_forgotten", subject_type="customer", subject_id=customer_id, deletion_type="gdpr_forget"))
            if deleted_at is not None and deletion_type == "soft":
                await self._audit_violation(
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=_agent_actor_id(agent_id),
                    action="ai.data_access_violation",
                    resource_type="customer",
                    resource_id=UUID(customer_id),
                    details={"reason": "soft_deleted", "subject_type": "customer", "subject_id": customer_id},
                )
                inc_data_governance_violation(agent_id=agent_id, violation="soft_deleted", subject_type="customer")
                raise DataGovernanceBlocked(DataGovernanceBlock(reason="soft_deleted", subject_type="customer", subject_id=customer_id, deletion_type="soft"))

    async def _ensure_user_allowed(self, *, tenant_id: str, agent_id: str, user_id: str) -> None:
        assert self._pool
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            row = await conn.fetchrow(
                "SELECT deletion_type, deleted_at FROM users WHERE tenant_id=$1::uuid AND id=$2::uuid",
                tenant_id,
                user_id,
            )
            if not row:
                return
            deletion_type = row["deletion_type"]
            deleted_at = row["deleted_at"]
            if deletion_type == "gdpr_forget":
                await self._audit_violation(
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=_agent_actor_id(agent_id),
                    action="ai.data_access_violation",
                    resource_type="user",
                    resource_id=UUID(user_id),
                    details={"reason": "gdpr_forgotten", "subject_type": "user", "subject_id": user_id},
                )
                inc_data_governance_violation(agent_id=agent_id, violation="gdpr_forgotten", subject_type="user")
                raise DataGovernanceBlocked(DataGovernanceBlock(reason="gdpr_forgotten", subject_type="user", subject_id=user_id, deletion_type="gdpr_forget"))
            if deleted_at is not None and deletion_type == "soft":
                await self._audit_violation(
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=_agent_actor_id(agent_id),
                    action="ai.data_access_violation",
                    resource_type="user",
                    resource_id=UUID(user_id),
                    details={"reason": "soft_deleted", "subject_type": "user", "subject_id": user_id},
                )
                inc_data_governance_violation(agent_id=agent_id, violation="soft_deleted", subject_type="user")
                raise DataGovernanceBlocked(DataGovernanceBlock(reason="soft_deleted", subject_type="user", subject_id=user_id, deletion_type="soft"))

    async def _audit_violation(
        self,
        *,
        tenant_id: str,
        actor_type: str,
        actor_id: UUID,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        details: dict[str, Any],
    ) -> None:
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                await conn.execute(
                    """
                    INSERT INTO audit_logs (
                      id, tenant_id, actor_type, actor_id, action, resource_type, resource_id, old_value, new_value, created_at
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10)
                    """,
                    uuid4(),
                    UUID(tenant_id),
                    actor_type,
                    actor_id,
                    action,
                    resource_type,
                    resource_id,
                    None,
                    json.dumps(details, separators=(",", ":")),
                    now,
                )


def _agent_actor_id(agent_id: str) -> UUID:
    return uuid5(UUID("00000000-0000-0000-0000-000000000001"), agent_id)
