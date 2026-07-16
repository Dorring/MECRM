import pytest
from unittest.mock import AsyncMock, MagicMock


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


@pytest.mark.asyncio
async def test_policy_denial_records_a_safe_denied_decision():
    from agents.sales import SalesAgent

    agent = SalesAgent()
    engine = MemoryExplainability()
    agent.set_explainability_engine(engine)
    core_response = MagicMock()
    core_response.json.return_value = {"result": {"allow": False}}
    approval_response = MagicMock()
    approval_response.json.return_value = {"result": {"requires_approval": True}}
    agent.http_client = MagicMock()
    agent.http_client.post = AsyncMock(side_effect=[core_response, approval_response])

    result = await agent.check_policy(
        tenant_id="11111111-1111-4111-8111-111111111111",
        action="leads:qualify",
        resource={"lead_id": "not-persisted"},
        confidence=0.4,
    )

    assert result["allowed"] is False
    assert len(engine.decisions) == 1
    decision = engine.decisions[0]
    assert decision.status == "denied"
    assert decision.reasoning == {"factors": [{"name": "policy", "outcome": "denied"}]}
    assert decision.evidence == [{"type": "opa_policy", "source_id": "denied"}]


@pytest.mark.asyncio
async def test_emit_event_records_explicit_degraded_status():
    from agents.sales import SalesAgent

    agent = SalesAgent()
    agent.producer = AsyncMock()
    engine = MemoryExplainability()
    agent.set_explainability_engine(engine)

    await agent.emit_event(
        topic="crm.agents.action-proposed",
        event_type="crm.agents.resolution-suggested",
        tenant_id="11111111-1111-4111-8111-111111111111",
        data={"confidence": 0.8},
        decision_status="degraded",
        decision_evidence=[{"type": "knowledge_retrieval", "source_id": "unavailable"}],
    )

    assert engine.decisions[0].status == "degraded"
    assert engine.decisions[0].evidence[0] == {"type": "knowledge_retrieval", "source_id": "unavailable"}
