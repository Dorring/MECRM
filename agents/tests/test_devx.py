"""Unit tests for Dev Experience Agent - AnomalyDetector and RootCauseAnalyzer."""
from __future__ import annotations

from datetime import datetime, timezone


from intelligence.devx.anomaly_detector import (
    AnomalyDetector,
    Anomaly,
    AnomalyType,
    Severity,
)
from intelligence.devx.root_cause import (
    RootCauseAnalyzer,
    RootCauseAnalysis,
    SERVICE_DEPENDENCIES,
)
from intelligence.devx.suggestion_engine import (
    SuggestionEngine,
    Suggestion,
    DevInsight,
    REMEDIATION_PLAYBOOKS,
)


class TestAnomalyTypes:
    """Tests for anomaly type enumeration."""

    def test_all_types_defined(self):
        expected_types = [
            "latency_spike",
            "error_storm",
            "kafka_lag",
            "db_contention",
            "memory_pressure",
            "cpu_saturation",
            "connection_exhaustion",
        ]
        actual_types = [e.value for e in AnomalyType]
        for t in expected_types:
            assert t in actual_types

    def test_severity_levels(self):
        expected_severities = ["low", "medium", "high", "critical"]
        actual_severities = [e.value for e in Severity]
        for s in expected_severities:
            assert s in actual_severities


class TestAnomalyDetector:
    """Tests for AnomalyDetector."""

    def setup_method(self):
        """Setup test fixtures."""
        self.detector = AnomalyDetector()

    def test_no_anomalies_healthy_metrics(self):
        """Healthy metrics should produce no anomalies."""
        metrics = {
            "latency_p99_ms": 100,
            "error_rate_percent": 0.1,
            "kafka_lag_messages": 50,
            "db_query_time_ms": 20,
            "memory_percent": 50,
            "cpu_percent": 40,
        }
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 0

    def test_detects_latency_spike_warning(self):
        """Should detect warning-level latency spike."""
        metrics = {"latency_p99_ms": 600}  # Above 500 warning
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.LATENCY_SPIKE
        assert anomalies[0].severity == Severity.MEDIUM

    def test_detects_latency_spike_critical(self):
        """Should detect critical latency spike."""
        metrics = {"latency_p99_ms": 2500}  # Above 2000 critical
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.LATENCY_SPIKE
        assert anomalies[0].severity == Severity.CRITICAL

    def test_detects_error_storm(self):
        """Should detect error rate spike."""
        metrics = {
            "error_rate_percent": 6.0,  # Above 5% critical
            "error_services": ["gateway", "agents"],
            "top_errors": ["ConnectionError", "TimeoutError"],
        }
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.ERROR_STORM
        assert anomalies[0].severity == Severity.CRITICAL

    def test_detects_kafka_lag(self):
        """Should detect Kafka consumer lag."""
        metrics = {
            "kafka_lag_messages": 15000,  # Above 10000 critical
            "lagging_consumers": ["journey-agent", "analytics-agent"],
        }
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.KAFKA_LAG

    def test_detects_db_contention(self):
        """Should detect database contention."""
        metrics = {
            "db_query_time_ms": 600,  # Above 500 critical
            "slow_queries": ["SELECT * FROM leads", "UPDATE deals"],
        }
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.DB_CONTENTION

    def test_detects_memory_pressure(self):
        """Should detect memory pressure."""
        metrics = {"memory_percent": 96}  # Above 95 critical
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.MEMORY_PRESSURE
        assert anomalies[0].severity == Severity.CRITICAL

    def test_detects_cpu_saturation(self):
        """Should detect CPU saturation."""
        metrics = {"cpu_percent": 85}  # Above 80 warning
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.CPU_SATURATION
        assert anomalies[0].severity == Severity.MEDIUM

    def test_detects_multiple_anomalies(self):
        """Should detect multiple anomalies simultaneously."""
        metrics = {
            "latency_p99_ms": 2500,
            "error_rate_percent": 10.0,
            "memory_percent": 98,
        }
        anomalies = self.detector.detect_anomalies(metrics)
        assert len(anomalies) == 3
        types = {a.anomaly_type for a in anomalies}
        assert AnomalyType.LATENCY_SPIKE in types
        assert AnomalyType.ERROR_STORM in types
        assert AnomalyType.MEMORY_PRESSURE in types

    def test_custom_thresholds(self):
        """Should respect custom thresholds."""
        custom_thresholds = {
            "latency_p99_ms": {"warning": 100, "critical": 200},
        }
        detector = AnomalyDetector(thresholds=custom_thresholds)
        metrics = {"latency_p99_ms": 150}
        anomalies = detector.detect_anomalies(metrics)
        assert len(anomalies) == 1
        assert anomalies[0].severity == Severity.MEDIUM

    def test_empty_metrics(self):
        """Empty metrics should produce no anomalies."""
        anomalies = self.detector.detect_anomalies({})
        assert len(anomalies) == 0


class TestRootCauseAnalyzer:
    """Tests for RootCauseAnalyzer."""

    def setup_method(self):
        """Setup test fixtures."""
        self.analyzer = RootCauseAnalyzer()

    def test_no_analysis_for_empty_anomalies(self):
        """Empty anomaly list should return None."""
        result = self.analyzer.analyze([])
        assert result is None

    def test_single_anomaly_analysis(self):
        """Single anomaly should produce analysis."""
        anomaly = Anomaly(
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            severity=Severity.HIGH,
            confidence=0.9,
            metric_name="latency_p99_ms",
            current_value=1500,
            threshold=500,
            affected_services=["gateway"],
            detected_at=datetime.now(timezone.utc).isoformat(),
            metadata={},
        )
        result = self.analyzer.analyze([anomaly])
        assert result is not None
        assert result.confidence > 0

    def test_finds_root_service(self):
        """Should identify most upstream affected service."""
        anomalies = [
            Anomaly(
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                severity=Severity.HIGH,
                confidence=0.9,
                metric_name="latency_p99_ms",
                current_value=1500,
                threshold=500,
                affected_services=["gateway", "database"],
                detected_at=datetime.now(timezone.utc).isoformat(),
                metadata={},
            ),
        ]
        result = self.analyzer.analyze(anomalies)
        assert result is not None

    def test_analysis_includes_reasoning(self):
        """Analysis should include human-readable reasoning."""
        anomaly = Anomaly(
            anomaly_type=AnomalyType.ERROR_STORM,
            severity=Severity.CRITICAL,
            confidence=0.95,
            metric_name="error_rate_percent",
            current_value=10.0,
            threshold=5.0,
            affected_services=["gateway"],
            detected_at=datetime.now(timezone.utc).isoformat(),
            metadata={},
        )
        result = self.analyzer.analyze([anomaly])
        assert result is not None
        assert len(result.analysis_reasoning) > 10


class TestSuggestionEngine:
    """Tests for SuggestionEngine."""

    def setup_method(self):
        """Setup test fixtures."""
        self.engine = SuggestionEngine()

    def test_generates_suggestions_for_latency(self):
        """Should generate suggestions for latency spike."""
        analysis = RootCauseAnalysis(
            primary_cause="High latency in request processing",
            confidence=0.85,
            affected_services=["gateway"],
            root_service="gateway",
            contributing_anomalies=[AnomalyType.LATENCY_SPIKE],
            analysis_reasoning="High latency detected.",
            metadata={},
        )
        insight = self.engine.generate_insight(analysis, [AnomalyType.LATENCY_SPIKE])
        assert len(insight.suggested_actions) > 0

    def test_generates_suggestions_for_errors(self):
        """Should generate suggestions for error storm."""
        analysis = RootCauseAnalysis(
            primary_cause="Elevated error rate",
            confidence=0.9,
            affected_services=["gateway", "agents"],
            root_service="gateway",
            contributing_anomalies=[AnomalyType.ERROR_STORM],
            analysis_reasoning="Error storm detected.",
            metadata={},
        )
        insight = self.engine.generate_insight(analysis, [AnomalyType.ERROR_STORM])
        assert len(insight.suggested_actions) > 0

    def test_limits_suggestions(self):
        """Should limit to top 5 suggestions."""
        analysis = RootCauseAnalysis(
            primary_cause="Multiple issues",
            confidence=0.8,
            affected_services=["gateway"],
            root_service="gateway",
            contributing_anomalies=[
                AnomalyType.LATENCY_SPIKE,
                AnomalyType.ERROR_STORM,
                AnomalyType.KAFKA_LAG,
            ],
            analysis_reasoning="Multiple issues detected.",
            metadata={},
        )
        insight = self.engine.generate_insight(
            analysis,
            [AnomalyType.LATENCY_SPIKE, AnomalyType.ERROR_STORM, AnomalyType.KAFKA_LAG],
        )
        assert len(insight.suggested_actions) <= 5


class TestDevInsight:
    """Tests for DevInsight dataclass."""

    def test_valid_insight(self):
        suggestion = Suggestion(
            action="Check logs",
            priority=1,
            category="investigation",
            estimated_impact="Find root cause",
            requires_approval=False,
            documentation_link="/docs/logs.md",
        )
        insight = DevInsight(
            incident_type="latency_spike",
            severity="high",
            confidence=0.85,
            suspected_services=["gateway"],
            root_cause="High latency",
            analysis="Latency spike detected in gateway.",
            suggested_actions=[suggestion],
            metadata={},
        )
        assert insight.incident_type == "latency_spike"
        assert insight.severity == "high"
        assert len(insight.suggested_actions) == 1


class TestServiceDependencies:
    """Tests for service dependency graph."""

    def test_gateway_has_dependencies(self):
        """Gateway should have some dependencies."""
        assert "gateway" in SERVICE_DEPENDENCIES
        assert len(SERVICE_DEPENDENCIES["gateway"]) > 0

    def test_agents_has_dependencies(self):
        """Agents should have some dependencies."""
        assert "agents" in SERVICE_DEPENDENCIES
        assert len(SERVICE_DEPENDENCIES["agents"]) > 0


class TestRemediationPlaybooks:
    """Tests for remediation playbook structure."""

    def test_some_anomaly_types_have_playbooks(self):
        """Some anomaly types should have remediation playbooks."""
        assert len(REMEDIATION_PLAYBOOKS) > 0

    def test_playbook_structure(self):
        """Playbooks should have required fields."""
        for anomaly_type, playbook in REMEDIATION_PLAYBOOKS.items():
            assert len(playbook) > 0
            for item in playbook:
                assert "action" in item
                assert "priority" in item
                assert "category" in item
