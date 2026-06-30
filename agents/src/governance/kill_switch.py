import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import redis.asyncio as redis


class KillSwitchState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"


@dataclass(frozen=True)
class KillSwitchStatus:
    state: KillSwitchState
    updated_at_ms: int
    reason: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "state": self.state.value,
                "updated_at_ms": self.updated_at_ms,
                "reason": self.reason,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(raw: str | bytes | None) -> Optional["KillSwitchStatus"]:
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return KillSwitchStatus(
            state=KillSwitchState(data["state"]),
            updated_at_ms=int(data["updated_at_ms"]),
            reason=data.get("reason"),
        )


@dataclass(frozen=True)
class KillSwitchDecision:
    blocked: bool
    status: Optional[KillSwitchStatus]
    scope_key: Optional[str]


class AgentKillSwitch:
    def __init__(self, redis_url: str, *, channel: str = "governance:killswitch:events", redis_client: Optional[redis.Redis] = None):
        self._redis_url = redis_url
        self._channel = channel
        self._redis: Optional[redis.Redis] = redis_client
        self._pubsub: Optional[redis.client.PubSub] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._cache: dict[str, KillSwitchStatus] = {}
        self._cache_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._redis and self._pubsub:
            return
        if not self._redis:
            self._redis = redis.from_url(self._redis_url, decode_responses=False)
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._stop_event.clear()
        self._listener_task = asyncio.create_task(self._listen())

    async def close(self) -> None:
        listener_task = self._listener_task
        pubsub = self._pubsub
        redis_client = self._redis
        self._stop_event.set()

        if listener_task:
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener_task

        if pubsub:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(self._channel)
            aclose = getattr(pubsub, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
            else:
                with contextlib.suppress(Exception):
                    await pubsub.close()

        if redis_client:
            aclose = getattr(redis_client, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
            else:
                with contextlib.suppress(Exception):
                    await redis_client.close()
        self._redis = None
        self._pubsub = None
        self._listener_task = None

    async def emergency_stop(self, agent_id: str | None, *, reason: str | None = None) -> None:
        if agent_id is None:
            await self._set_state(self._global_key(), KillSwitchState.KILLED, reason=reason)
            return
        await self._set_state(self._agent_key(agent_id), KillSwitchState.KILLED, reason=reason)

    async def pause_all_agents(self, tenant_id: str, *, reason: str | None = None) -> None:
        await self._set_state(self._tenant_key(tenant_id), KillSwitchState.PAUSED, reason=reason)

    async def resume_agents(self, tenant_id: str, *, reason: str | None = None) -> None:
        await self._set_state(self._tenant_key(tenant_id), KillSwitchState.RUNNING, reason=reason)

    async def get_status(self) -> dict[str, Any]:
        if not self._redis:
            await self.start()
        assert self._redis

        global_raw = await self._redis.get(self._global_key())
        global_status = KillSwitchStatus.from_json(global_raw)

        tenant_states: dict[str, Any] = {}
        agent_states: dict[str, Any] = {}
        tenant_agent_states: dict[str, Any] = {}

        async for key_raw in self._redis.scan_iter(match="governance:killswitch:*"):
            if isinstance(key_raw, bytes):
                key = key_raw.decode("utf-8")
            else:
                key = str(key_raw)
            if key == self._global_key():
                continue

            raw = await self._redis.get(key_raw)
            status = KillSwitchStatus.from_json(raw)
            if not status:
                continue

            parts = key.split(":")
            if parts[:3] == ["governance", "killswitch", "tenant"] and len(parts) == 4:
                tenant_states[parts[3]] = status.__dict__
            elif parts[:3] == ["governance", "killswitch", "agent"] and len(parts) == 4:
                agent_states[parts[3]] = status.__dict__
            elif parts[:3] == ["governance", "killswitch", "tenant"] and len(parts) == 6 and parts[4] == "agent":
                tenant_agent_states[f"{parts[3]}:{parts[5]}"] = status.__dict__

        return {
            "global": global_status.__dict__ if global_status else None,
            "tenants": tenant_states,
            "agents": agent_states,
            "tenant_agents": tenant_agent_states,
        }

    async def decision(self, *, tenant_id: str, agent_id: str) -> KillSwitchDecision:
        try:
            if not self._redis:
                await self.start()
        except Exception:
            status = KillSwitchStatus(state=KillSwitchState.KILLED, updated_at_ms=_now_ms(), reason="redis_unavailable")
            return KillSwitchDecision(blocked=True, status=status, scope_key="redis_unavailable")

        keys = [
            self._global_key(),
            self._tenant_key(tenant_id),
            self._agent_key(agent_id),
            self._tenant_agent_key(tenant_id, agent_id),
        ]

        cached = await self._get_from_cache(keys)
        for key, cached_status in cached:
            if cached_status is not None and cached_status.state in (KillSwitchState.PAUSED, KillSwitchState.KILLED):
                return KillSwitchDecision(blocked=True, status=cached_status, scope_key=key)

        try:
            assert self._redis
            raw_values = await self._redis.mget(keys)
            statuses = [KillSwitchStatus.from_json(v) for v in raw_values]
            await self._put_in_cache(dict(zip(keys, statuses)))
        except Exception:
            status = KillSwitchStatus(state=KillSwitchState.KILLED, updated_at_ms=_now_ms(), reason="redis_unavailable")
            return KillSwitchDecision(blocked=True, status=status, scope_key="redis_unavailable")

        for key, fetched_status in zip(keys, statuses):
            if fetched_status is not None and fetched_status.state in (KillSwitchState.PAUSED, KillSwitchState.KILLED):
                return KillSwitchDecision(blocked=True, status=fetched_status, scope_key=key)

        return KillSwitchDecision(blocked=False, status=None, scope_key=None)

    async def _listen(self) -> None:
        pubsub = self._pubsub
        assert pubsub
        try:
            while not self._stop_event.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not msg:
                    continue
                data = msg.get("data")
                try:
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    payload = json.loads(data)
                    key = payload["key"]
                    status = KillSwitchStatus(
                        state=KillSwitchState(payload["state"]),
                        updated_at_ms=int(payload["updated_at_ms"]),
                        reason=payload.get("reason"),
                    )
                    await self._put_in_cache({key: status})
                except Exception:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _set_state(self, key: str, state: KillSwitchState, *, reason: str | None) -> None:
        if not self._redis:
            await self.start()
        assert self._redis

        status = KillSwitchStatus(state=state, updated_at_ms=_now_ms(), reason=reason)
        await self._redis.set(key, status.to_json().encode("utf-8"))
        await self._redis.publish(
            self._channel,
            json.dumps(
                {"key": key, "state": state.value, "updated_at_ms": status.updated_at_ms, "reason": reason},
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        await self._put_in_cache({key: status})

    async def _get_from_cache(self, keys: list[str]) -> list[tuple[str, Optional[KillSwitchStatus]]]:
        async with self._cache_lock:
            return [(k, self._cache.get(k)) for k in keys]

    async def _put_in_cache(self, updates: dict[str, Optional[KillSwitchStatus]]) -> None:
        async with self._cache_lock:
            for k, v in updates.items():
                if v is None:
                    self._cache.pop(k, None)
                else:
                    self._cache[k] = v

    @staticmethod
    def _global_key() -> str:
        return "governance:killswitch:global"

    @staticmethod
    def _tenant_key(tenant_id: str) -> str:
        return f"governance:killswitch:tenant:{tenant_id}"

    @staticmethod
    def _tenant_agent_key(tenant_id: str, agent_id: str) -> str:
        return f"governance:killswitch:tenant:{tenant_id}:agent:{agent_id}"

    @staticmethod
    def _agent_key(agent_id: str) -> str:
        return f"governance:killswitch:agent:{agent_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)
