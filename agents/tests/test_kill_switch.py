import asyncio
import fnmatch

import pytest



class FakePubSub:
    def __init__(self, redis):
        self._redis = redis
        self._channels = set()
        self._queue: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, channel: str):
        self._channels.add(channel)
        self._redis._pubsubs.add(self)

    async def listen(self):
        while True:
            data = await self._queue.get()
            yield {"type": "message", "data": data}

    async def close(self):
        self._redis._pubsubs.discard(self)


class FakeRedis:
    def __init__(self):
        self._kv: dict[bytes, bytes] = {}
        self._pubsubs: set[FakePubSub] = set()

    def pubsub(self):
        return FakePubSub(self)

    async def get(self, key):
        k = key.encode("utf-8") if isinstance(key, str) else key
        return self._kv.get(k)

    async def set(self, key, value, ex=None):
        k = key.encode("utf-8") if isinstance(key, str) else key
        v = value.encode("utf-8") if isinstance(value, str) else value
        self._kv[k] = v

    async def delete(self, key):
        k = key.encode("utf-8") if isinstance(key, str) else key
        self._kv.pop(k, None)

    async def mget(self, keys):
        out = []
        for key in keys:
            k = key.encode("utf-8") if isinstance(key, str) else key
            out.append(self._kv.get(k))
        return out

    async def publish(self, channel, payload):
        data = payload
        for ps in list(self._pubsubs):
            if channel in ps._channels:
                ps._queue.put_nowait(data)

    async def scan_iter(self, match: str):
        for k in list(self._kv.keys()):
            key = k.decode("utf-8")
            if fnmatch.fnmatch(key, match):
                yield k

    async def close(self):
        return


@pytest.mark.asyncio
async def test_kill_switch_pause_and_resume():
    from governance.kill_switch import AgentKillSwitch, KillSwitchState

    fake = FakeRedis()
    ks = AgentKillSwitch("redis://fake", redis_client=fake)
    await ks.start()

    await ks.pause_all_agents("tenant-1")
    decision = await ks.decision(tenant_id="tenant-1", agent_id="sales-agent")
    assert decision.blocked is True
    assert decision.status is not None
    assert decision.status.state == KillSwitchState.PAUSED

    await ks.resume_agents("tenant-1")
    decision2 = await ks.decision(tenant_id="tenant-1", agent_id="sales-agent")
    assert decision2.blocked is False
    await ks.close()


@pytest.mark.asyncio
async def test_kill_switch_agent_stop_blocks_any_tenant():
    from governance.kill_switch import AgentKillSwitch, KillSwitchState

    fake = FakeRedis()
    ks = AgentKillSwitch("redis://fake", redis_client=fake)
    await ks.start()

    await ks.emergency_stop("sales-agent")
    decision = await ks.decision(tenant_id="tenant-1", agent_id="sales-agent")
    assert decision.blocked is True
    assert decision.status is not None
    assert decision.status.state == KillSwitchState.KILLED
    await ks.close()
