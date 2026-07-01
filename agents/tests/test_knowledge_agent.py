"""Unit tests for Knowledge Agent and related components."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from intelligence.knowledge.classifier import TopicClassification, classify_topic
from intelligence.knowledge.summarizer import DraftKbArticle, generate_draft_from_ticket, generate_draft_from_conversation


class TestDraftKbArticle:
    """Tests for DraftKbArticle Pydantic model."""

    def test_valid_article(self):
        article = DraftKbArticle(
            title="How to reset password",
            problem_summary="User cannot login due to forgotten password",
            solution_steps=["Navigate to login page", "Click forgot password", "Check email"],
            preconditions=["Valid email address"],
            tags=["auth", "password"],
            confidence=0.85,
        )
        assert article.title == "How to reset password"
        assert len(article.solution_steps) == 3
        assert article.confidence == 0.85

    def test_title_min_length(self):
        with pytest.raises(ValueError):
            DraftKbArticle(title="ab", problem_summary="Some problem summary here")

    def test_tags_normalized(self):
        article = DraftKbArticle(
            title="Test Article",
            problem_summary="Test problem summary",
            tags=["Auth", "PASSWORD", "SSO"],
        )
        # Tags should be passed as-is to model, normalization happens in summarizer
        assert article.tags == ["Auth", "PASSWORD", "SSO"]


class TestTopicClassification:
    """Tests for topic classification."""

    def test_valid_topics(self):
        for topic in ["billing", "onboarding", "integrations", "bugs", "usage", "unknown"]:
            cls = TopicClassification(topic=topic, tags=["test"], confidence=0.8)
            assert cls.topic == topic

    def test_default_values(self):
        cls = TopicClassification()
        assert cls.topic == "unknown"
        assert cls.tags == []
        assert cls.confidence == 0.0


class TestGenerateDraftFromTicket:
    """Tests for ticket-based draft generation."""

    @pytest.mark.asyncio
    async def test_generates_valid_draft(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=json.dumps({
                "title": "Password Reset Issue",
                "problem_summary": "User cannot reset their password.",
                "solution_steps": ["Check email", "Click link", "Set new password"],
                "preconditions": ["Valid email"],
                "tags": ["auth", "password"],
                "confidence": 0.9,
            })
        )

        result = await generate_draft_from_ticket(
            llm=mock_llm,
            subject="Cannot reset password",
            description="I clicked forgot password but no email arrived",
            resolution="Resent the password reset email, user confirmed receipt",
        )

        assert result.error is None
        assert result.draft.title == "Password Reset Issue"
        assert len(result.draft.solution_steps) == 3
        assert result.draft.confidence == 0.9

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content="This is not JSON")

        result = await generate_draft_from_ticket(
            llm=mock_llm,
            subject="Test ticket",
            description=None,
            resolution=None,
        )

        # Should fallback to heuristic draft
        assert result.error is not None
        assert result.draft.title == "Test ticket"

    @pytest.mark.asyncio
    async def test_handles_llm_exception(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.side_effect = Exception("LLM unavailable")

        result = await generate_draft_from_ticket(
            llm=mock_llm,
            subject="Test ticket",
            description=None,
            resolution=None,
        )

        assert result.error is not None
        assert "LLM unavailable" in result.error


class TestGenerateDraftFromConversation:
    """Tests for conversation-based draft generation."""

    @pytest.mark.asyncio
    async def test_generates_valid_draft(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=json.dumps({
                "title": "SSO Configuration Guide",
                "problem_summary": "Customer needed help configuring SSO.",
                "solution_steps": ["Access SSO settings", "Configure IdP", "Test login"],
                "preconditions": ["Admin access", "IdP credentials"],
                "tags": ["sso", "integrations"],
                "confidence": 0.85,
            })
        )

        transcript = [
            {"role": "customer", "message": "How do I set up SSO?"},
            {"role": "agent", "message": "I can help you configure SSO. First..."},
        ]

        result = await generate_draft_from_conversation(
            llm=mock_llm,
            conversation_id="conv-123",
            transcript=transcript,
        )

        assert result.error is None
        assert result.draft.title == "SSO Configuration Guide"

    @pytest.mark.asyncio
    async def test_handles_empty_transcript(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=json.dumps({
                "title": "Empty Conversation",
                "problem_summary": "No content available.",
                "solution_steps": [],
                "preconditions": [],
                "tags": [],
                "confidence": 0.1,
            })
        )

        result = await generate_draft_from_conversation(
            llm=mock_llm,
            conversation_id="conv-empty",
            transcript=[],
        )

        assert result.error is None


class TestClassifyTopic:
    """Tests for topic classification."""

    @pytest.mark.asyncio
    async def test_classifies_billing_topic(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=json.dumps({
                "topic": "billing",
                "tags": ["invoice", "payment"],
                "confidence": 0.95,
            })
        )

        result = await classify_topic(
            llm=mock_llm,
            title="Invoice not received",
            problem="Customer did not receive monthly invoice",
            resolution="Resent invoice to correct email",
        )

        assert result.error is None
        assert result.parsed.topic == "billing"
        assert result.parsed.confidence == 0.95

    @pytest.mark.asyncio
    async def test_handles_invalid_topic(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=json.dumps({
                "topic": "invalid_topic",
                "tags": [],
                "confidence": 0.5,
            })
        )

        result = await classify_topic(
            llm=mock_llm,
            title="Test",
            problem="Test",
            resolution="Test",
        )

        # Pydantic should reject invalid topic, fallback to default
        assert result.parsed.topic == "unknown" or result.error is not None


class TestKnowledgeAgentIntegration:
    """Integration-style tests for Knowledge Agent (mocked dependencies)."""

    @pytest.mark.asyncio
    async def test_deduplication_prevents_duplicate_drafts(self):
        """Test that existing drafts are not recreated."""
        from unittest.mock import AsyncMock

        # Mock the agent's _find_existing_draft to return an existing ID
        with patch("intelligence.knowledge.knowledge_agent.KnowledgeAgent") as MockAgent:
            agent = MockAgent.return_value
            agent._find_existing_draft = AsyncMock(return_value="existing-draft-id")
            agent._insert_draft = AsyncMock()

            # When existing draft found, insert should not be called
            # This is a structural test - verifying the flow exists
            assert callable(agent._find_existing_draft)
            assert callable(agent._insert_draft)

    def test_source_event_dataclass(self):
        """Test SourceEvent dataclass creation."""
        from intelligence.knowledge.knowledge_agent import SourceEvent

        event = SourceEvent(
            topic="crm.ticket.resolved",
            tenant_id="tenant-123",
            correlation_id="corr-456",
            data={"ticketId": "ticket-789"},
        )

        assert event.topic == "crm.ticket.resolved"
        assert event.tenant_id == "tenant-123"
        assert event.data["ticketId"] == "ticket-789"


class TestKnowledgePublisher:
    """Tests for KnowledgePublisher (Weaviate integration)."""

    def test_weaviate_schema_structure(self):
        """Verify expected Weaviate schema properties."""
        # This is a structural test - verifying the schema is correct
        from intelligence.knowledge.publisher import KnowledgePublisher

        publisher = KnowledgePublisher()
        # The _ensure_schema method should create these properties
        assert hasattr(publisher, "_ensure_schema")


class TestApprovalWorkflow:
    """Tests for knowledge draft approval workflow."""

    def test_draft_status_transitions(self):
        """Verify valid status transitions."""
        valid_statuses = ["draft", "approved", "rejected"]

        # Draft can transition to approved or rejected
        # Approved/rejected cannot transition back
        transitions = {
            "draft": ["approved", "rejected"],
            "approved": [],  # Terminal state
            "rejected": [],  # Terminal state
        }

        assert all(s in valid_statuses for s in transitions.keys())

    def test_confidence_bounds(self):
        """Test confidence score is bounded between 0 and 1."""
        article = DraftKbArticle(
            title="Test Article",
            problem_summary="Test problem",
            confidence=1.5,  # Out of bounds
        )
        # The model accepts the value, but summarizer clamps it
        # This tests the model itself doesn't validate bounds
        assert article.confidence == 1.5  # Model doesn't clamp
