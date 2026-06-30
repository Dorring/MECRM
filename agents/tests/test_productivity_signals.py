import pytest

from intelligence.productivity.productivity_agent import ProductivitySignalsAgent


class FakeRedis:
    def __init__(self):
        self._sets = {}
        self._zsets = {}
        self._hashes = {}
        self._kv = {}

    async def sadd(self, key, value):
        self._sets.setdefault(key, set()).add(value)

    async def smembers(self, key):
        return self._sets.get(key, set())

    async def zadd(self, key, mapping):
        z = self._zsets.setdefault(key, {})
        for member, score in mapping.items():
            z[str(member)] = float(score)

    async def zrangebyscore(self, key, min, max, start=0, num=200):
        z = self._zsets.get(key, {})
        items = [(m, s) for m, s in z.items() if float(min) <= float(s) <= float(max)]
        items.sort(key=lambda x: x[1])
        sliced = items[start : start + num]
        return [m.encode("utf-8") for m, _ in sliced]

    async def zscore(self, key, member):
        z = self._zsets.get(key, {})
        val = z.get(str(member))
        return val

    async def zrem(self, key, member):
        z = self._zsets.get(key, {})
        z.pop(str(member), None)

    async def hset(self, key, mapping=None, **kwargs):
        h = self._hashes.setdefault(key, {})
        if mapping:
            for k, v in mapping.items():
                h[str(k)] = v
        for k, v in kwargs.items():
            h[str(k)] = v

    async def hget(self, key, field):
        h = self._hashes.get(key, {})
        v = h.get(str(field))
        if v is None:
            return None
        return v.encode("utf-8") if isinstance(v, str) else v

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._kv:
            return None
        self._kv[key] = value
        return True


@pytest.mark.asyncio
async def test_lead_idle_detector_emits_once_with_suppression():
    agent = ProductivitySignalsAgent()
    agent._redis = FakeRedis()
    emitted = []

    async def _emit_event(*, topic, event_type, tenant_id, data, correlation_id=None):
        emitted.append((topic, event_type, tenant_id, data))

    agent.emit_event = _emit_event  # type: ignore

    tenant = "11111111-1111-1111-1111-111111111111"
    lead = "22222222-2222-2222-2222-222222222222"

    await agent._redis.sadd("prod:tenants", tenant.encode("utf-8"))
    await agent._redis.zadd(f"prod:lead:last_activity:{tenant}", {lead: 0})

    await agent._scan_once()
    await agent._scan_once()

    signals = [e for e in emitted if e[0] == "crm.productivity.signal"]
    assert len(signals) == 1
    assert signals[0][3]["signal"]["type"] == "lead_idle"


@pytest.mark.asyncio
async def test_ticket_aging_detector_emits_and_stops_after_resolve():
    agent = ProductivitySignalsAgent()
    agent._redis = FakeRedis()
    emitted = []

    async def _emit_event(*, topic, event_type, tenant_id, data, correlation_id=None):
        emitted.append((topic, event_type, tenant_id, data))

    agent.emit_event = _emit_event  # type: ignore

    tenant = "11111111-1111-1111-1111-111111111111"
    ticket = "33333333-3333-3333-3333-333333333333"

    await agent._redis.sadd("prod:tenants", tenant.encode("utf-8"))
    await agent._redis.zadd(f"prod:ticket:sla_due:{tenant}", {ticket: 0})
    await agent._redis.hset(f"prod:ticket:meta:{tenant}:{ticket}", mapping={"status": "open"})

    await agent._scan_once()
    assert any(e[3]["signal"]["type"] == "ticket_aging" for e in emitted)

    await agent.ingest_event(topic="crm.tickets.resolved", event={"tenantid": tenant, "type": "crm.tickets.resolved", "data": {"ticketId": ticket}})
    emitted.clear()
    await agent._scan_once()
    assert not emitted


@pytest.mark.asyncio
async def test_consumer_restart_preserves_suppression_state():
    shared = FakeRedis()
    tenant = "11111111-1111-1111-1111-111111111111"
    lead = "22222222-2222-2222-2222-222222222222"
    await shared.sadd("prod:tenants", tenant.encode("utf-8"))
    await shared.zadd(f"prod:lead:last_activity:{tenant}", {lead: 0})

    emitted1 = []
    a1 = ProductivitySignalsAgent()
    a1._redis = shared
    async def _emit1(**kw):
        emitted1.append(kw)
    a1.emit_event = _emit1  # type: ignore
    await a1._scan_once()
    assert len(emitted1) == 1

    emitted2 = []
    a2 = ProductivitySignalsAgent()
    a2._redis = shared
    async def _emit2(**kw):
        emitted2.append(kw)
    a2.emit_event = _emit2  # type: ignore
    await a2._scan_once()
    assert len(emitted2) == 0

