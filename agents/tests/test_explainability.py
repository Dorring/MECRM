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
    assert engine.decisions[0].reasoning == {"factors": [{"name": "x"}]}


def test_decision_redaction_removes_chain_of_thought_and_credentials():
    from governance.explainability import _redact

    redacted = _redact(
        {
            "reasoning": "private model rationale",
            "prompt": "private user prompt",
            "api_key": "secret-key",
            "factors": [{"name": "policy_check", "status": "passed"}],
        }
    )

    assert redacted["reasoning"] == "[redacted]"
    assert redacted["prompt"] == "[redacted]"
    assert redacted["api_key"] == "[redacted]"
    assert redacted["factors"] == [{"name": "policy_check", "status": "passed"}]
