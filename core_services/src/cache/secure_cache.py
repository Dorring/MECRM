from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from .metrics import cache_hit_total, cache_invalidation_total, cache_miss_total


@dataclass(frozen=True)
class UserContext:
    user_id: str
    roles_hash: str


class SecureCache:
    def __init__(self, redis_url: str, *, namespace: str = "sc", key_version: str = "v1"):
        self._redis_url = redis_url
        self._namespace = namespace
        self._key_version = key_version
        self._client: redis.Redis | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._client:
                return
            self._client = redis.from_url(self._redis_url, decode_responses=True)

    async def close(self) -> None:
        async with self._lock:
            if not self._client:
                return
            aclose = getattr(self._client, "aclose", None)
            if callable(aclose):
                await aclose()
            else:
                await self._client.close()
            self._client = None

    async def get(self, *, tenant_id: str, user_ctx: UserContext, resource: str, policy_id: str, policy_hash: str) -> Any | None:
        key = await self._build_key(tenant_id=tenant_id, resource=resource, user_ctx=user_ctx, policy_id=policy_id, policy_hash=policy_hash)
        client = await self._get_client()
        raw = await client.get(key)
        if raw is None:
            cache_miss_total.labels(tenant=tenant_id, cache="secure").inc()
            return None
        try:
            cache_hit_total.labels(tenant=tenant_id, cache="secure").inc()
            return json.loads(raw)
        except Exception:
            await client.delete(key)
            return None

    async def set(
        self,
        *,
        tenant_id: str,
        user_ctx: UserContext,
        resource: str,
        policy_id: str,
        policy_hash: str,
        value: Any,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        key = await self._build_key(tenant_id=tenant_id, resource=resource, user_ctx=user_ctx, policy_id=policy_id, policy_hash=policy_hash)
        client = await self._get_client()
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        await client.setex(key, ttl_seconds, payload)

    async def invalidate_tenant(self, tenant_id: str) -> int:
        client = await self._get_client()
        cache_invalidation_total.labels(tenant=tenant_id, reason="tenant_epoch").inc()
        return int(await client.incr(self._tenant_epoch_key(tenant_id)))

    async def invalidate_policy(self, policy_id: str) -> int:
        client = await self._get_client()
        cache_invalidation_total.labels(tenant="*", reason=f"policy_epoch:{policy_id}").inc()
        return int(await client.incr(self._policy_epoch_key(policy_id)))

    async def invalidate_user(self, tenant_id: str, user_id: str) -> int:
        client = await self._get_client()
        cache_invalidation_total.labels(tenant=tenant_id, reason=f"user_epoch:{user_id}").inc()
        return int(await client.incr(self._user_epoch_key(tenant_id, user_id)))

    async def _build_key(self, *, tenant_id: str, resource: str, user_ctx: UserContext, policy_id: str, policy_hash: str) -> str:
        client = await self._get_client()

        tenant_epoch_key = self._tenant_epoch_key(tenant_id)
        policy_epoch_key = self._policy_epoch_key(policy_id)
        user_epoch_key = self._user_epoch_key(tenant_id, user_ctx.user_id)

        tv, pv, uv = await client.mget(tenant_epoch_key, policy_epoch_key, user_epoch_key)
        tenant_epoch = tv or "0"
        policy_epoch = pv or "0"
        user_epoch = uv or "0"

        return (
            f"{self._namespace}:{self._key_version}:t:{tenant_id}:tv:{tenant_epoch}:p:{policy_id}:pv:{policy_epoch}"
            f":u:{user_ctx.user_id}:uv:{user_epoch}:rh:{user_ctx.roles_hash}:ph:{policy_hash}:r:{resource}"
        )

    async def _get_client(self) -> redis.Redis:
        if not self._client:
            await self.start()
        assert self._client
        return self._client

    def _tenant_epoch_key(self, tenant_id: str) -> str:
        return f"{self._namespace}:{self._key_version}:tenant_epoch:{tenant_id}"

    def _policy_epoch_key(self, policy_id: str) -> str:
        return f"{self._namespace}:{self._key_version}:policy_epoch:{policy_id}"

    def _user_epoch_key(self, tenant_id: str, user_id: str) -> str:
        return f"{self._namespace}:{self._key_version}:user_epoch:{tenant_id}:{user_id}"
