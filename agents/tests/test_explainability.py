import pytest
from unittest.mock import AsyncMock


class MemoryExplainability:
    def __init__(self):
        self.decisions = []

    async def record_decision(self, decision):
        self.decisions.append(decision)
        return decision.id


@pytest.mark.asyncio
async def test_explainability_artifact_created_on_emit_event():
    from agents.sales import SalesAgent

    agent = SalesAgent()
    agent.producer = AsyncMock()
    agent.http_client = AsyncMock()
    engine = MemoryExplainability()
    agent.set_explainability_engine(engine)

    await agent.emit_event(
        topic="crm.test",
        event_type="crm.test.action",
        tenant_id="11111111-1111-4111-8111-111111111111",
        data={"confidence": 0.8, "reasoning": "test", "factors": [{"name": "x"}]},
    )

    assert len(engine.decisions) == 1
    assert engine.decisions[0].action_type == "crm.test.action"
