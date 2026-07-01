"""Unit tests for Digital Twins - TwinBuilder and BehaviorModel."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from intelligence.twins.twin_builder import TwinBuilder, TwinProfile
from intelligence.twins.behavior_model import BehaviorModel, BehaviorPrediction


class TestTwinProfile:
    """Tests for TwinProfile dataclass."""

    def test_valid_profile(self):
        profile = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            engagement_score=0.8,
            price_sensitivity=0.3,
            churn_sensitivity=0.2,
            conversion_likelihood=0.7,
            confidence=0.85,
        )
        assert profile.customer_id == "cust-123"
        assert profile.confidence == 0.85
        assert profile.engagement_score == 0.8

    def test_profile_default_values(self):
        """Test default values for optional fields."""
        profile = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
        )
        assert profile.engagement_score == 0.0
        assert profile.price_sensitivity == 0.5
        assert profile.churn_sensitivity == 0.5
        assert profile.confidence == 0.0


class TestBehaviorModel:
    """Tests for BehaviorModel prediction engine."""

    def setup_method(self):
        """Setup test fixtures."""
        self.high_engagement_features = {
            "engagement_score": 0.9,
            "price_sensitivity": 0.2,
            "churn_sensitivity": 0.1,
            "conversion_likelihood": 0.85,
            "support_dependency": 0.2,
            "tenure_months": 36,
            "payment_reliability": 0.98,
        }
        self.low_engagement_features = {
            "engagement_score": 0.2,
            "price_sensitivity": 0.8,
            "churn_sensitivity": 0.7,
            "conversion_likelihood": 0.3,
            "support_dependency": 0.8,
            "tenure_months": 3,
            "payment_reliability": 0.5,
        }

    def test_price_increase_high_engagement(self):
        """High engagement customer should tolerate small price increase."""
        model = BehaviorModel(self.high_engagement_features)
        prediction = model.predict_price_response(increase_percent=5.0)
        
        assert isinstance(prediction, dict)
        assert "retain" in prediction
        assert prediction["retain"] > 0.5

    def test_price_increase_low_engagement(self):
        """Low engagement customer likely to churn on large price increase."""
        model = BehaviorModel(self.low_engagement_features)
        prediction = model.predict_price_response(increase_percent=20.0)
        
        assert "churn" in prediction
        assert prediction["churn"] > prediction.get("retain", 0)

    def test_feature_removal_high_usage(self):
        """High usage customer more likely to complain on feature removal."""
        model = BehaviorModel(self.high_engagement_features)
        prediction = model.predict_feature_removal_response(feature_name="reports")
        
        assert isinstance(prediction, dict)
        # Should have some response categories
        assert len(prediction) > 0

    def test_contract_renewal_long_tenure(self):
        """Long tenure customer more likely to renew."""
        model = BehaviorModel(self.high_engagement_features)
        prediction = model.predict_renewal_response()
        
        assert "renew" in prediction
        assert prediction["renew"] > 0.5

    def test_upsell_high_value(self):
        """High value customer may accept upsell."""
        model = BehaviorModel(self.high_engagement_features)
        prediction = model.predict_upsell_response(upsell_value=500.0)
        
        assert "accept" in prediction or "defer" in prediction

    def test_upsell_low_value_customer(self):
        """Low value customer likely to decline upsell."""
        model = BehaviorModel(self.low_engagement_features)
        prediction = model.predict_upsell_response(upsell_value=5000.0)
        
        assert "decline" in prediction

    def test_model_confidence(self):
        """Model should return confidence score."""
        model = BehaviorModel(self.high_engagement_features)
        confidence = model.get_model_confidence()
        
        assert 0.0 <= confidence <= 1.0

    def test_explanation_factors(self):
        """Model should return explanation factors."""
        model = BehaviorModel(self.high_engagement_features)
        factors = model.get_explanation_factors("price_increase")
        
        assert isinstance(factors, list)

    def test_probability_bounds(self):
        """All probabilities should be between 0 and 1."""
        model = BehaviorModel(self.high_engagement_features)
        prediction = model.predict_price_response(increase_percent=10.0)
        
        for prob in prediction.values():
            assert 0.0 <= prob <= 1.0


class TestBehaviorPrediction:
    """Tests for BehaviorPrediction dataclass."""

    def test_valid_prediction(self):
        prediction = BehaviorPrediction(
            prediction_type="price_response",
            probability=0.75,
            confidence=0.85,
            explanation="Customer likely to retain due to high engagement",
            features_used=["engagement_score", "price_sensitivity"],
        )
        assert prediction.prediction_type == "price_response"
        assert prediction.probability == 0.75
        assert len(prediction.features_used) == 2


class TestTwinBuilder:
    """Tests for TwinBuilder."""

    @pytest.fixture
    def mock_conn(self):
        """Create a mock database connection."""
        mock = AsyncMock()
        mock.fetchrow = AsyncMock()
        mock.fetch = AsyncMock()
        mock.execute = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_get_twin_returns_none_when_not_found(self, mock_conn):
        """Test get_twin returns None when no profile exists."""
        mock_conn.fetchrow.return_value = None
        
        builder = TwinBuilder(mock_conn)
        result = await builder.get_twin(tenant_id="tenant-456", customer_id="cust-123")
        
        assert result is None

    @pytest.mark.asyncio
    async def test_get_twin_returns_profile_when_found(self, mock_conn):
        """Test get_twin returns profile when exists."""
        # Mock DB row structure as expected by TwinBuilder.get_twin
        mock_conn.fetchrow.return_value = {
            "customer_id": "cust-123",
            "tenant_id": "tenant-456",
            "embedding_profile": None,
            "behavior_features": {
                "engagement_score": 0.8,
                "price_sensitivity": 0.3,
                "churn_sensitivity": 0.2,
                "conversion_likelihood": 0.7,
                "support_dependency": 0.2,
                "tickets_30d": 2,
                "confidence": 0.85,
            },
            "last_updated": datetime.now(timezone.utc),
        }
        
        builder = TwinBuilder(mock_conn)
        result = await builder.get_twin(tenant_id="tenant-456", customer_id="cust-123")
        
        assert result is not None
        assert result.customer_id == "cust-123"

    @pytest.mark.asyncio
    async def test_build_twin_creates_profile(self, mock_conn):
        """Test build_twin creates a new profile."""
        # Mock feature extraction queries
        mock_conn.fetchrow.side_effect = [
            # Journey features
            {"stage": "expansion", "confidence": 0.8, "risk_level": "green"},
            # Other features return None
            None, None, None,
        ]
        mock_conn.fetch.side_effect = [
            # Ticket features
            [],
            # Payment features
            [],
        ]
        
        builder = TwinBuilder(mock_conn)
        
        # Mock the persist method
        with patch.object(builder, "_persist_twin", return_value=None):
            result = await builder.build_twin(tenant_id="tenant-456", customer_id="cust-123")
        
        assert result is not None
        assert result.customer_id == "cust-123"
        assert result.tenant_id == "tenant-456"

    def test_builder_has_required_methods(self, mock_conn):
        """Test TwinBuilder has all required methods."""
        builder = TwinBuilder(mock_conn)
        
        assert hasattr(builder, "build_twin")
        assert hasattr(builder, "get_twin")
        assert callable(builder.build_twin)
        assert callable(builder.get_twin)
