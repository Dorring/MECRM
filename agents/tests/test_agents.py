"""
Unit tests for AI Agents
"""

import pytest
import json
from unittest.mock import AsyncMock


# Test the sales agent
class TestSalesAgent:
    """Tests for the Sales Agent."""
    
    @pytest.fixture
    def sales_agent(self):
        from agents.sales import SalesAgent
        agent = SalesAgent()
        agent.producer = AsyncMock()
        agent.http_client = AsyncMock()
        return agent
    
    @pytest.mark.asyncio
    async def test_qualify_lead_success(self, sales_agent):
        """Test successful lead qualification."""
        # Mock LLM response
        sales_agent.call_llm = AsyncMock(return_value=json.dumps({
            "score": 85,
            "qualification_status": "qualified",
            "reasoning": "Strong corporate email domain and complete company info",
            "confidence": 0.9,
            "factors": [
                {"name": "email_domain", "impact": "positive", "weight": 0.8}
            ],
            "recommended_actions": ["Schedule discovery call"]
        }))
        
        # Mock OPA check
        sales_agent.check_policy = AsyncMock(return_value={
            "allowed": True,
            "requires_approval": False,
            "deny_reasons": []
        })
        
        event = {
            "type": "crm.leads.created",
            "tenantid": "test-tenant",
            "data": {
                "leadId": "123",
                "name": "John Smith",
                "email": "john@acme.com",
                "company": "Acme Corp",
                "source": "website"
            }
        }
        
        result = await sales_agent.qualify_lead(event)
        
        assert result["status"] == "completed"
        assert result["score"] == 85
        assert result["qualification_status"] == "qualified"
        
    @pytest.mark.asyncio
    async def test_qualify_lead_requires_approval(self, sales_agent):
        """Test lead qualification requiring approval due to low confidence."""
        sales_agent.call_llm = AsyncMock(return_value=json.dumps({
            "score": 60,
            "qualification_status": "needs_info",
            "reasoning": "Insufficient information to qualify",
            "confidence": 0.5,
            "factors": [],
            "recommended_actions": []
        }))
        
        sales_agent.check_policy = AsyncMock(return_value={
            "allowed": True,
            "requires_approval": True,
            "deny_reasons": []
        })
        
        event = {
            "type": "crm.leads.created",
            "tenantid": "test-tenant",
            "data": {
                "leadId": "123",
                "name": "Jane Doe",
            }
        }
        
        result = await sales_agent.qualify_lead(event)
        
        assert result["status"] == "pending_approval"
        
    @pytest.mark.asyncio
    async def test_qualify_lead_policy_denied(self, sales_agent):
        """Test lead qualification denied by policy."""
        sales_agent.check_policy = AsyncMock(return_value={
            "allowed": False,
            "requires_approval": False,
            "deny_reasons": ["Agent rate limit exceeded"]
        })
        
        event = {
            "type": "crm.leads.created",
            "tenantid": "test-tenant",
            "data": {"leadId": "123", "name": "Test"}
        }
        
        result = await sales_agent.qualify_lead(event)
        
        assert result["status"] == "denied"


class TestSupportAgent:
    """Tests for the Support Agent."""
    
    @pytest.fixture
    def support_agent(self):
        from agents.support import SupportAgent
        agent = SupportAgent()
        agent.producer = AsyncMock()
        agent.http_client = AsyncMock()
        return agent
    
    @pytest.mark.asyncio
    async def test_triage_ticket(self, support_agent):
        """Test ticket triage."""
        support_agent.call_llm = AsyncMock(return_value=json.dumps({
            "category": "technical",
            "urgency": "high",
            "sentiment": "frustrated",
            "key_issues": ["Login failure", "Account locked"],
            "suggested_resolution": "Reset user account",
            "requires_escalation": False,
            "confidence": 0.85,
            "reasoning": "Clear technical issue affecting user access"
        }))
        
        support_agent.check_policy = AsyncMock(return_value={
            "allowed": True,
            "requires_approval": False,
            "deny_reasons": []
        })
        
        event = {
            "type": "crm.tickets.created",
            "tenantid": "test-tenant",
            "data": {
                "ticketId": "456",
                "subject": "Cannot login to dashboard",
                "description": "Getting error when trying to login",
                "priority": "high"
            }
        }
        
        result = await support_agent.triage_ticket(event)
        
        assert result["status"] == "completed"
        assert result["category"] == "technical"
        assert result["urgency"] == "high"


class TestComplianceAgent:
    """Tests for the Compliance Agent."""
    
    @pytest.fixture
    def compliance_agent(self):
        from agents.compliance import ComplianceAgent
        agent = ComplianceAgent()
        agent.producer = AsyncMock()
        return agent
    
    @pytest.mark.asyncio
    async def test_validate_clean_data(self, compliance_agent):
        """Test validation of clean data."""
        event = {
            "type": "crm.leads.created",
            "tenantid": "test-tenant",
            "data": {
                "leadId": "123",
                "name": "John Smith",
                "email": "john@example.com",
                "company": "Acme Corp"
            }
        }
        
        result = await compliance_agent.validate_data(event, entity_type="lead")
        
        assert result["status"] == "completed"
        assert result["compliant"] is True
        assert len(result["issues"]) == 0
        
    @pytest.mark.asyncio
    async def test_detect_pii(self, compliance_agent):
        """Test PII detection."""
        event = {
            "type": "crm.leads.created",
            "tenantid": "test-tenant",
            "data": {
                "leadId": "123",
                "name": "John Smith",
                "ssn": "123-45-6789",
                "credit_card": "4111111111111111"
            }
        }
        
        result = await compliance_agent.validate_data(event, entity_type="lead")
        
        assert result["status"] == "completed"
        assert result["compliant"] is False
        assert any(issue["type"] == "pii_detected" for issue in result["issues"])
        
    @pytest.mark.asyncio
    async def test_detect_suspicious_pattern(self, compliance_agent):
        """Test suspicious pattern detection."""
        event = {
            "type": "crm.leads.created",
            "tenantid": "test-tenant",
            "data": {
                "leadId": "123",
                "name": "Test User Fake",
                "email": "test@tempmail.com"
            }
        }
        
        result = await compliance_agent.validate_data(event, entity_type="lead")
        
        assert len(result["issues"]) > 0


class TestAnalyticsAgent:
    """Tests for the Analytics Agent."""
    
    @pytest.fixture
    def analytics_agent(self):
        from agents.analytics import AnalyticsAgent
        agent = AnalyticsAgent()
        agent.producer = AsyncMock()
        return agent
    
    @pytest.mark.asyncio
    async def test_track_pipeline_normal(self, analytics_agent):
        """Test normal pipeline movement tracking."""
        event = {
            "type": "crm.deals.stage-changed",
            "tenantid": "test-tenant",
            "data": {
                "dealId": "789",
                "previousStage": "prospecting",
                "newStage": "qualification",
                "amount": 50000
            }
        }
        
        result = await analytics_agent.track_pipeline_movement(event)
        
        assert result["status"] == "completed"
        assert result["anomaly"] is None
        
    @pytest.mark.asyncio
    async def test_detect_skipped_stages(self, analytics_agent):
        """Test detection of skipped stages."""
        event = {
            "type": "crm.deals.stage-changed",
            "tenantid": "test-tenant",
            "data": {
                "dealId": "789",
                "previousStage": "prospecting",
                "newStage": "negotiation",  # Skipped qualification and proposal
                "amount": 50000
            }
        }
        
        result = await analytics_agent.track_pipeline_movement(event)
        
        assert result["status"] == "completed"
        assert result["anomaly"] == "skipped_stages"
        
    @pytest.mark.asyncio
    async def test_detect_backwards_movement(self, analytics_agent):
        """Test detection of backwards pipeline movement."""
        event = {
            "type": "crm.deals.stage-changed",
            "tenantid": "test-tenant",
            "data": {
                "dealId": "789",
                "previousStage": "proposal",
                "newStage": "qualification",  # Moved backwards
                "amount": 50000
            }
        }
        
        result = await analytics_agent.track_pipeline_movement(event)

        assert result["status"] == "completed"
        assert result["anomaly"] == "backwards_movement"


class TestSupportAgentSuggestResolution:
    """Tests for SupportAgent.suggest_resolution (Phase 6 P1 fix)."""

    @pytest.fixture
    def support_agent(self):
        from agents.support import SupportAgent
        agent = SupportAgent()
        agent.producer = AsyncMock()
        agent.http_client = AsyncMock()
        # Disable KB retrieval so the test exercises the LLM+validation path
        # without requiring Weaviate/Ollama to be reachable.
        agent._get_vector_search = lambda: None
        return agent

    @pytest.mark.asyncio
    async def test_suggest_resolution_success(self, support_agent):
        """Valid LLM output is Pydantic-validated and emitted as a proposal."""
        support_agent.call_llm = AsyncMock(return_value=json.dumps({
            "summary": "Reset the user password and re-enable the account.",
            "steps": [
                {"order": 1, "action": "Verify user identity", "rationale": "Security"},
                {"order": 2, "action": "Reset password", "rationale": "Restore access"},
            ],
            "confidence": 0.8,
            "sources": [],
            "requires_human": False,
            "reasoning": "Login failure due to locked account.",
        }))
        support_agent.check_policy = AsyncMock(return_value={
            "allowed": True,
            "requires_approval": False,
            "deny_reasons": [],
        })
        support_agent.emit_reasoning = AsyncMock()
        support_agent.emit_event = AsyncMock()

        event = {
            "type": "crm.tickets.created",
            "tenantid": "test-tenant",
            "correlationid": "corr-1",
            "data": {
                "ticketId": "t-100",
                "subject": "Cannot login",
                "description": "Account locked out",
                "priority": "high",
            },
        }

        result = await support_agent.suggest_resolution(event)

        assert result["status"] == "completed"
        assert result["step_count"] == 2
        assert result["requires_human"] is False
        # The validated suggestion must be emitted as a structured action proposal.
        assert support_agent.emit_event.await_count == 1
        kwargs = support_agent.emit_event.await_args.kwargs
        assert kwargs["topic"] == "crm.agents.action-proposed"
        assert kwargs["event_type"] == "crm.agents.resolution-suggested"
        assert kwargs["tenant_id"] == "test-tenant"
        assert kwargs["data"]["ticketId"] == "t-100"
        assert len(kwargs["data"]["steps"]) == 2

    @pytest.mark.asyncio
    async def test_suggest_resolution_rejects_invalid_model_outputs(self, support_agent):
        """Malformed LLM output must not drive a downstream write."""
        # Missing required "summary" and wrong confidence type -> ValidationError.
        support_agent.call_llm = AsyncMock(return_value=json.dumps({
            "steps": [],
            "confidence": "high",
        }))
        support_agent.check_policy = AsyncMock()
        support_agent.emit_reasoning = AsyncMock()
        support_agent.emit_event = AsyncMock()

        event = {
            "type": "crm.tickets.created",
            "tenantid": "test-tenant",
            "data": {"ticketId": "t-101", "subject": "Broken", "description": "x"},
        }

        result = await support_agent.suggest_resolution(event)

        assert result["status"] == "failed"
        assert result["reason"] == "invalid_model_output"
        assert result["requires_human"] is True
        # Nothing should be emitted when validation fails.
        support_agent.emit_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suggest_resolution_policy_denied(self, support_agent):
        """OPA denial blocks the suggestion emission."""
        support_agent.call_llm = AsyncMock(return_value=json.dumps({
            "summary": "Reset password.",
            "steps": [{"order": 1, "action": "Reset password", "rationale": "Access"}],
            "confidence": 0.7,
            "sources": [],
            "requires_human": False,
            "reasoning": "ok",
        }))
        support_agent.check_policy = AsyncMock(return_value={
            "allowed": False,
            "requires_approval": False,
            "deny_reasons": ["capability_not_allowed"],
        })
        support_agent.emit_reasoning = AsyncMock()
        support_agent.emit_event = AsyncMock()

        event = {
            "type": "crm.tickets.created",
            "tenantid": "test-tenant",
            "data": {"ticketId": "t-102", "subject": "Login", "description": "fail"},
        }

        result = await support_agent.suggest_resolution(event)

        assert result["status"] == "denied"
        # Reasoning may be emitted (transparency), but no action proposal.
        support_agent.emit_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suggest_resolution_kb_retrieval_degrades_gracefully(self, support_agent):
        """A failing VectorSearch must not block the LLM-based suggestion."""
        # Inject a stub object whose search() raises to simulate Weaviate down.
        class _BrokenVS:
            async def search(self, *, tenant_id, query, top_k, entity):
                raise RuntimeError("weaviate down")

        support_agent._get_vector_search = lambda: _BrokenVS()

        support_agent.call_llm = AsyncMock(return_value=json.dumps({
            "summary": "Reset password.",
            "steps": [{"order": 1, "action": "Reset password", "rationale": "Access"}],
            "confidence": 0.6,
            "sources": [],
            "requires_human": False,
            "reasoning": "ok",
        }))
        support_agent.check_policy = AsyncMock(return_value={
            "allowed": True,
            "requires_approval": False,
            "deny_reasons": [],
        })
        support_agent.emit_reasoning = AsyncMock()
        support_agent.emit_event = AsyncMock()

        event = {
            "type": "crm.tickets.created",
            "tenantid": "test-tenant",
            "data": {"ticketId": "t-103", "subject": "Login", "description": "fail"},
        }

        result = await support_agent.suggest_resolution(event)

        assert result["status"] == "completed"
        assert support_agent.emit_event.await_count == 1
        assert support_agent.emit_event.await_args.kwargs["data"]["kbHits"] == 0


class TestAnalyticsForecast:
    """Tests for AnalyticsAgent.generate_forecast output shape."""

    @pytest.fixture
    def analytics_agent(self):
        from agents.analytics import AnalyticsAgent
        agent = AnalyticsAgent()
        agent.producer = AsyncMock()
        agent.http_client = AsyncMock()
        return agent

    @pytest.mark.asyncio
    async def test_generate_forecast_completed(self, analytics_agent):
        analytics_agent.call_llm = AsyncMock(return_value=json.dumps({
            "forecast_type": "revenue",
            "time_range": "next_30_days",
            "predictions": [
                {"period": "week1", "value": 12000, "confidence_range": [10000, 14000]}
            ],
            "factors": ["seasonal uptick"],
            "confidence": 0.72,
        }))

        result = await analytics_agent.generate_forecast(
            tenant_id="test-tenant", forecast_type="revenue", time_range="next_30_days"
        )

        assert result["status"] == "completed"
        assert result["forecast"]["predictions"][0]["value"] == 12000

    @pytest.mark.asyncio
    async def test_generate_forecast_llm_failure(self, analytics_agent):
        analytics_agent.call_llm = AsyncMock(side_effect=RuntimeError("ollama down"))

        result = await analytics_agent.generate_forecast(
            tenant_id="test-tenant", forecast_type="revenue", time_range="next_30_days"
        )

        assert result["status"] == "failed"


class TestOrchestratorDLQ:
    """Tests for the orchestrator dead-letter routing (Phase 6 P1 fix)."""

    def _make_orchestrator(self):
        from orchestrator.main import AgentOrchestrator
        orch = AgentOrchestrator()
        orch.producer = AsyncMock()
        return orch

    def _make_message(self, value, topic="crm.tickets.created", partition=0, offset=5):
        from types import SimpleNamespace

        return SimpleNamespace(
            topic=topic,
            partition=partition,
            offset=offset,
            value=value,
        )

    @pytest.mark.asyncio
    async def test_dlq_preserves_tenant_context(self):
        """DLQ envelope must carry tenant_id in both the CloudEvents field and the Kafka key."""
        orch = self._make_orchestrator()
        original = json.dumps({
            "specversion": "1.0",
            "type": "crm.tickets.created",
            "tenantid": "tenant-abc",
            "correlationid": "corr-9",
            "data": {"ticketId": "t-9", "subject": "Boom"},
        })
        msg = self._make_message(value=original)

        sent = await orch._send_to_dlq(msg, RuntimeError("processing exploded"))

        assert sent is True
        args, kwargs = orch.producer.send_and_wait.await_args
        assert args[0] == "crm.dlq.agents"
        # Kafka key must be the tenant id (tenant isolation preserved).
        assert kwargs["key"] == b"tenant-abc"
        envelope = json.loads(kwargs["value"])
        assert envelope["tenantid"] == "tenant-abc"
        assert envelope["type"] == "crm.agents.dlq"
        assert envelope["data"]["original_topic"] == "crm.tickets.created"
        assert envelope["data"]["original_offset"] == 5
        assert "processing exploded" in envelope["data"]["error_reason"]
        assert envelope["data"]["original_value"] == original

    @pytest.mark.asyncio
    async def test_dlq_handles_non_json_payload(self):
        """A non-JSON poison message is still routed with empty tenant context."""
        orch = self._make_orchestrator()
        msg = self._make_message(value="not-json-at-all")

        sent = await orch._send_to_dlq(msg, ValueError("bad json"))

        assert sent is True
        args, kwargs = orch.producer.send_and_wait.await_args
        envelope = json.loads(kwargs["value"])
        assert envelope["tenantid"] == ""
        assert kwargs["key"] is None
        assert envelope["data"]["original_value"] == "not-json-at-all"
        assert "bad json" in envelope["data"]["error_reason"]

    @pytest.mark.asyncio
    async def test_dlq_send_failure_returns_false(self):
        """If the DLQ send itself fails, the orchestrator reports failure (no silent drop)."""
        orch = self._make_orchestrator()
        orch.producer.send_and_wait = AsyncMock(side_effect=RuntimeError("kafka down"))
        msg = self._make_message(value=json.dumps({"tenantid": "t1", "data": {}}))

        sent = await orch._send_to_dlq(msg, RuntimeError("orig"))

        assert sent is False
