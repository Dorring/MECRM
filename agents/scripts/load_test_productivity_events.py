import argparse
import asyncio
import time
from uuid import uuid4

from intelligence.productivity.productivity_agent import ProductivitySignalsAgent


class NullRedis:
    def __init__(self):
        self._sets = {"prod:tenants": set()}
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
        return self._zsets.get(key, {}).get(str(member))

    async def zrem(self, key, member):
        self._zsets.get(key, {}).pop(str(member), None)

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


async def run(*, events: int, tenants: int) -> None:
    agent = ProductivitySignalsAgent()
    agent._redis = NullRedis()
    agent.emit_event = (lambda **_: None)  # type: ignore

    tenant_ids = [str(uuid4()) for _ in range(tenants)]
    for t in tenant_ids:
        await agent._redis.sadd("prod:tenants", t.encode("utf-8"))

    t0 = time.perf_counter()
    for i in range(events):
        tenant = tenant_ids[i % tenants]
        lead_id = str(uuid4())
        await agent._ingest_event(topic="crm.leads.updated", event={"tenantid": tenant, "type": "crm.leads.updated", "data": {"leadId": lead_id, "changes": {"status": "contacted"}, "newStatus": "contacted"}})
        if i % 1000 == 0:
            await agent._scan_once()
    await agent._scan_once()
    dt = time.perf_counter() - t0
    rate = events / dt
    print({"events": events, "tenants": tenants, "seconds": dt, "events_per_second": rate, "events_per_hour": int(rate * 3600)})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--events", type=int, default=100_000)
    p.add_argument("--tenants", type=int, default=50)
    args = p.parse_args()
    asyncio.run(run(events=args.events, tenants=args.tenants))


if __name__ == "__main__":
    main()

