import pytest

from intelligence.productivity.productivity_agent import ProductivitySignalsAgent


class FakeRedis:
    def __init__(self):
        self._sets = {}
        self._kv = {}

    async def sadd(self, key, value):
        self._sets.setdefault(key, set()).add(value)

    async def smembers(self, key):
        return self._sets.get(key, set())

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._kv:
            return None
        self._kv[key] = value
        return True


@pytest.mark.asyncio
async def test_prediction_generated_emits_productivity_signal_once():
    agent = ProductivitySignalsAgent()
    agent._redis = FakeRedis()
    emitted = []

    async def _emit_event(*, topic, event_type, tenant_id, data, correlation_id=None):
        emitted.append((topic, event_type, tenant_id, data))

    agent.emit_event = _emit_event  # type: ignore

    tenant = "11111111-1111-1111-1111-111111111111"
    await agent._redis.sadd("prod:tenants", tenant.encode("utf-8"))

    event = {
        "type": "crm.analytics.prediction-generated",
        "tenantid": tenant,
        "data": {
            "tenant_id": tenant,
            "entity_type": "customer",
            "entity_id": "22222222-2222-2222-2222-222222222222",
            "prediction_type": "churn",
            "probability": 0.8,
            "risk_level": "red",
            "explanation": "High churn risk",
        },
    }

    await agent.ingest_event(topic="crm.analytics.prediction-generated", event=event)
    await agent.ingest_event(topic="crm.analytics.prediction-generated", event=event)

    signals = [e for e in emitted if e[0] == "crm.productivity.signal"]
    assert len(signals) == 1
    assert signals[0][3]["signal"]["type"] == "prediction_churn"

