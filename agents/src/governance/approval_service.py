import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import redis.asyncio as redis


@dataclass(frozen=True)
class PendingAction:
    tenant_id: str
    agent_id: str
    approval_id: str
    action_type: str
    topic: str
    event_type: str
    data: dict[str, Any]
    correlation_id: Optional[str] = None
    expires_at_ms: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant_id": self.tenant_id,
                "agent_id": self.agent_id,
                "approval_id": self.approval_id,
                "action_type": self.action_type,
                "topic": self.topic,
                "event_type": self.event_type,
                "data": self.data,
                "correlation_id": self.correlation_id,
                "expires_at_ms": self.expires_at_ms,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(raw: str | bytes | None) -> Optional["PendingAction"]:
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        d = json.loads(raw)
        return PendingAction(
            tenant_id=d["tenant_id"],
            agent_id=d["agent_id"],
            approval_id=d["approval_id"],
            action_type=d["action_type"],
            topic=d["topic"],
            event_type=d["event_type"],
            data=d["data"],
            correlation_id=d.get("correlation_id"),
            expires_at_ms=d.get("expires_at_ms"),
        )


class ApprovalService:
    def __init__(self, redis_url: str, redis_client: Optional[redis.Redis] = None):
        self._redis_url = redis_url
        self._redis: Optional[redis.Redis] = redis_client

    async def start(self) -> None:
        if self._redis:
            return
        self._redis = redis.from_url(self._redis_url, decode_responses=False)

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
        self._redis = None

    async def request_approval(self, action: PendingAction, *, ttl_seconds: int = 86400) -> str:
        if not self._redis:
            await self.start()
        assert self._redis

        key = _pending_key(action.approval_id)
        expires_at_ms = _now_ms() + ttl_seconds * 1000
        payload = PendingAction(
            **{**action.__dict__, "expires_at_ms": expires_at_ms},
        ).to_json()
        await self._redis.set(key, payload.encode("utf-8"), ex=ttl_seconds)
        return action.approval_id

    async def approve(self, approval_id: str, user: str) -> None:
        if not self._redis:
            await self.start()
        assert self._redis
        await self._redis.set(_decision_key(approval_id), json.dumps({"decision": "approved", "by": user, "at_ms": _now_ms()}).encode("utf-8"), ex=86400)

    async def reject(self, approval_id: str, user: str) -> None:
        if not self._redis:
            await self.start()
        assert self._redis
        await self._redis.set(_decision_key(approval_id), json.dumps({"decision": "rejected", "by": user, "at_ms": _now_ms()}).encode("utf-8"), ex=86400)

    async def pop_pending(self, approval_id: str) -> Optional[PendingAction]:
        if not self._redis:
            await self.start()
        assert self._redis

        key = _pending_key(approval_id)
        raw = await self._redis.get(key)
        if not raw:
            return None
        await self._redis.delete(key)
        return PendingAction.from_json(raw)

    async def expire_pending(self) -> int:
        if not self._redis:
            await self.start()
        assert self._redis

        expired = 0
        async for key in self._redis.scan_iter(match="governance:approvals:pending:*"):
            raw = await self._redis.get(key)
            action = PendingAction.from_json(raw)
            if not action or not action.expires_at_ms:
                continue
            if action.expires_at_ms <= _now_ms():
                await self._redis.delete(key)
                expired += 1
        return expired


def approval_requestor_uuid(agent_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"enterprise-crm:agent:{agent_id}"))


def _pending_key(approval_id: str) -> str:
    return f"governance:approvals:pending:{approval_id}"


def _decision_key(approval_id: str) -> str:
    return f"governance:approvals:decision:{approval_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)
