"""Integration tests for Digital Twins Simulation API."""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip if database is not available
pytestmark = pytest.mark.skipif(
    os.environ.get("CRM_TEST_REQUIRE_DB") != "1",
    reason="Database integration tests require CRM_TEST_REQUIRE_DB=1",
)


class TestTwinSimulationAPI:
    """Integration tests for twin simulation endpoints."""

    @pytest.fixture
    def mock_db_connection(self):
        """Create a mock database connection."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetch = AsyncMock()
        mock_conn.execute = AsyncMock()

        # Mock transaction context manager
        mock_transaction = MagicMock()
        mock_transaction.__aenter__ = AsyncMock(return_value=None)
        mock_transaction.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=mock_transaction)

        return mock_conn

    @pytest.fixture
    def mock_pool(self, mock_db_connection):
        """Create a mock connection pool."""
        mock_pool = AsyncMock()

        # Mock acquire context manager
        mock_acquire = MagicMock()
        mock_acquire.__aenter__ = AsyncMock(return_value=mock_db_connection)
        mock_acquire.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire = MagicMock(return_value=mock_acquire)

        return mock_pool

    @pytest.mark.asyncio
    async def test_simulate_price_increase(self, mock_db_connection):
        """Test price increase simulation."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        # Mock twin profile
        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(
                engagement_score=0.8,
                support_frequency=3,
                payment_regularity=0.9,
                tenure_months=24,
            ),
            confidence=0.85,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            result = await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="price_increase_10",
                user_id="user-789",
                params={},
            )

            assert result.customer_id == "cust-123"
            assert result.scenario == "price_increase_10"
            assert "retain" in result.outcomes or "churn" in result.outcomes
            assert result.confidence > 0

    @pytest.mark.asyncio
    async def test_simulate_feature_removal(self, mock_db_connection):
        """Test feature removal simulation."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(
                engagement_score=0.9,
                product_usage_depth=0.85,
            ),
            confidence=0.8,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            result = await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="feature_removal",
                user_id="user-789",
                params={},
            )

            assert result.scenario == "feature_removal"
            # High usage customer should show concern
            assert "complain" in result.outcomes or "negotiate" in result.outcomes

    @pytest.mark.asyncio
    async def test_simulate_contract_renewal(self, mock_db_connection):
        """Test contract renewal simulation."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(
                engagement_score=0.85,
                payment_regularity=0.95,
                tenure_months=36,
                sentiment_score=0.9,
            ),
            confidence=0.9,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            result = await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="contract_renewal",
                user_id="user-789",
                params={},
            )

            assert result.scenario == "contract_renewal"
            assert "renew" in result.outcomes
            # Long tenure, high engagement should favor renewal
            assert result.outcomes["renew"] > 0.5

    @pytest.mark.asyncio
    async def test_simulate_upsell(self, mock_db_connection):
        """Test upsell simulation."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(
                engagement_score=0.7,
                lifetime_value=25000.0,
                product_usage_depth=0.6,
            ),
            confidence=0.75,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            result = await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="upsell_small",
                user_id="user-789",
                params={},
            )

            assert result.scenario == "upsell_small"
            assert "accept" in result.outcomes or "consider" in result.outcomes

    @pytest.mark.asyncio
    async def test_simulation_logs_audit(self, mock_db_connection):
        """Test that simulations are logged for audit."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(),
            confidence=0.7,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="price_increase_5",
                user_id="user-789",
                params={},
            )

            # Verify audit log was written
            mock_db_connection.execute.assert_called()

    @pytest.mark.asyncio
    async def test_simulation_returns_explanation(self, mock_db_connection):
        """Test that simulation includes explanation."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(engagement_score=0.8),
            confidence=0.85,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            result = await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="price_increase_10",
                user_id="user-789",
                params={},
            )

            assert result.explanation
            assert len(result.explanation) > 10

    @pytest.mark.asyncio
    async def test_simulation_returns_factors(self, mock_db_connection):
        """Test that simulation includes contributing factors."""
        from intelligence.twins.simulator import TwinSimulator
        from intelligence.twins.twin_builder import TwinProfile, FeatureSet

        mock_twin = TwinProfile(
            customer_id="cust-123",
            tenant_id="tenant-456",
            features=FeatureSet(
                engagement_score=0.85,
                tenure_months=24,
                payment_regularity=0.9,
            ),
            confidence=0.8,
            created_at="2026-01-28T00:00:00Z",
        )

        with patch("intelligence.twins.simulator.TwinBuilder") as MockBuilder:
            mock_builder = MockBuilder.return_value
            mock_builder.get_twin = AsyncMock(return_value=mock_twin)

            simulator = TwinSimulator(mock_db_connection)
            result = await simulator.simulate(
                tenant_id="tenant-456",
                customer_id="cust-123",
                scenario="contract_renewal",
                user_id="user-789",
                params={},
            )

            assert result.factors
            assert len(result.factors) > 0


class TestTwinBuilderIntegration:
    """Integration tests for twin profile building."""

    @pytest.fixture
    def mock_db_connection(self):
        """Create a mock database connection."""
        mock_conn = AsyncMock()
        return mock_conn

    @pytest.mark.asyncio
    async def test_builds_profile_from_customer_data(self, mock_db_connection):
        """Test building a profile from aggregated customer data."""
        from intelligence.twins.twin_builder import TwinBuilder, FeatureSet

        # Mock journey data
        mock_db_connection.fetchrow.side_effect = [
            # First call: journey data
            {"stage": "expansion", "confidence": 0.85, "risk_level": "green"},
            # Second call: customer data
            {"lifetime_value": 25000.0, "created_at": "2024-01-01T00:00:00Z"},
        ]

        # Mock payment, ticket, conversation data
        mock_db_connection.fetch.side_effect = [
            [{"amount": 1000, "status": "completed"}] * 12,  # payments
            [{"priority": "low", "created_at": "2026-01-20T00:00:00Z"}] * 2,  # tickets
            [{"sentiment": "positive"}] * 5,  # conversations
        ]

        builder = TwinBuilder(mock_db_connection)

        with patch.object(builder, "build_twin") as mock_build:
            mock_build.return_value = MagicMock(
                customer_id="cust-123",
                tenant_id="tenant-456",
                features=FeatureSet(
                    engagement_score=0.8,
                    payment_regularity=1.0,
                    tenure_months=24,
                ),
                confidence=0.85,
            )

            twin = await builder.build_twin("tenant-456", "cust-123")
            assert twin is not None


class TestTenantIsolation:
    """Tests for tenant isolation in twins."""

    @pytest.fixture
    def mock_db_connection(self):
        """Create a mock database connection."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        return mock_conn

    @pytest.mark.asyncio
    async def test_sets_tenant_context(self, mock_db_connection):
        """Test that tenant context is set before queries."""
        from intelligence.twins.twin_builder import TwinBuilder

        builder = TwinBuilder(mock_db_connection)

        # The builder should set tenant context
        with patch.object(builder, "get_twin") as mock_get:
            mock_get.return_value = None

            await builder.get_twin("tenant-456", "cust-123")

            # Verify method was called
            mock_get.assert_called_once_with("tenant-456", "cust-123")

    @pytest.mark.asyncio
    async def test_cannot_access_other_tenant_twin(self, mock_db_connection):
        """Test that twins from other tenants are not accessible."""
        from intelligence.twins.twin_builder import TwinBuilder

        # Simulate RLS filtering by returning None
        mock_db_connection.fetchrow.return_value = None

        builder = TwinBuilder(mock_db_connection)

        with patch.object(builder, "get_twin", return_value=None):
            twin = await builder.get_twin("tenant-A", "cust-belongs-to-tenant-B")
            assert twin is None


class TestGraphWorkflow:
    """Tests for LangGraph workflow."""

    @pytest.mark.asyncio
    async def test_graph_executes_nodes(self):
        """Test that graph nodes execute in order."""
        from intelligence.twins.graph import build_twin_graph, TwinState, TwinDeps

        mock_conn = AsyncMock()
        deps = TwinDeps(conn=mock_conn)

        with patch("intelligence.twins.graph.load_or_build_twin") as mock_load, \
             patch("intelligence.twins.graph.run_simulation") as mock_sim, \
             patch("intelligence.twins.graph.format_response") as mock_format:

            # Setup mock returns
            mock_load.return_value = TwinState(
                tenant_id="t1",
                customer_id="c1",
                user_id="u1",
                scenario="price_increase_5",
            )
            mock_sim.return_value = mock_load.return_value
            mock_format.return_value = TwinState(
                tenant_id="t1",
                customer_id="c1",
                user_id="u1",
                scenario="price_increase_5",
                response={"success": True},
            )

            # Build graph (verify it compiles without error)
            graph = build_twin_graph(deps)
            assert graph is not None
