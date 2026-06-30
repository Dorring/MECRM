"""
Root Cause Analyzer - Correlates signals to identify probable root causes.

Analyzes relationships between anomalies and system dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry import trace

from .anomaly_detector import Anomaly, AnomalyType, Severity

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


# Service dependency graph (simplified, would typically come from service mesh)
SERVICE_DEPENDENCIES = {
    "gateway": ["agents", "database", "redis", "kafka"],
    "agents": ["database", "redis", "kafka", "openai"],
    "orchestrator": ["agents", "kafka"],
    "analytics_agent": ["database", "kafka"],
    "journey_agent": ["database", "kafka"],
    "knowledge_agent": ["database", "kafka", "openai"],
    "chat_agent": ["database", "kafka", "openai"],
    "twin_agent": ["database", "kafka"],
    "devx_agent": ["prometheus", "loki", "kafka"],
}

# Common root cause patterns
ROOT_CAUSE_PATTERNS = {
    "kafka_lag_with_high_latency": {
        "conditions": [AnomalyType.KAFKA_LAG, AnomalyType.LATENCY_SPIKE],
        "likely_cause": "Consumer processing bottleneck",
        "confidence_boost": 0.15,
    },
    "db_contention_with_latency": {
        "conditions": [AnomalyType.DB_CONTENTION, AnomalyType.LATENCY_SPIKE],
        "likely_cause": "Database query performance degradation",
        "confidence_boost": 0.2,
    },
    "memory_pressure_with_errors": {
        "conditions": [AnomalyType.MEMORY_PRESSURE, AnomalyType.ERROR_STORM],
        "likely_cause": "Memory exhaustion causing failures",
        "confidence_boost": 0.25,
    },
    "cpu_saturation_cascade": {
        "conditions": [AnomalyType.CPU_SATURATION, AnomalyType.LATENCY_SPIKE],
        "likely_cause": "High CPU load causing processing delays",
        "confidence_boost": 0.2,
    },
}


@dataclass(frozen=True)
class RootCauseAnalysis:
    """Root cause analysis result."""
    primary_cause: str
    confidence: float
    affected_services: list[str]
    root_service: str | None
    contributing_anomalies: list[AnomalyType]
    analysis_reasoning: str
    metadata: dict[str, Any]


class RootCauseAnalyzer:
    """Correlates anomalies to identify root causes."""
    
    def __init__(
        self,
        dependencies: dict[str, list[str]] | None = None,
        patterns: dict[str, dict[str, Any]] | None = None,
    ):
        self._dependencies = dependencies or SERVICE_DEPENDENCIES
        self._patterns = patterns or ROOT_CAUSE_PATTERNS
    
    def analyze(self, anomalies: list[Anomaly]) -> RootCauseAnalysis | None:
        """
        Analyze anomalies to determine root cause.
        
        Args:
            anomalies: List of detected anomalies
        
        Returns:
            Root cause analysis or None if no significant anomalies
        """
        if not anomalies:
            return None
        
        with tracer.start_as_current_span("root_cause.analyze") as span:
            span.set_attribute("anomaly_count", len(anomalies))
            
            # Get unique anomaly types
            anomaly_types = set(a.anomaly_type for a in anomalies)
            
            # Check for known patterns
            pattern_match = self._find_pattern_match(anomaly_types)
            
            # Find most severe anomaly as primary
            primary_anomaly = max(anomalies, key=lambda a: (
                self._severity_score(a.severity),
                a.confidence,
            ))
            
            # Collect all affected services
            all_affected: set[str] = set()
            for a in anomalies:
                all_affected.update(a.affected_services)
            
            # Find root service (most upstream in dependency graph)
            root_service = self._find_root_service(list(all_affected))
            
            # Calculate combined confidence
            base_confidence = primary_anomaly.confidence
            if pattern_match:
                base_confidence += pattern_match.get("confidence_boost", 0)
            confidence = min(1.0, base_confidence)
            
            # Generate reasoning
            reasoning = self._generate_reasoning(
                primary_anomaly,
                anomalies,
                pattern_match,
                root_service,
            )
            
            # Determine primary cause
            if pattern_match:
                primary_cause = pattern_match.get("likely_cause", str(primary_anomaly.anomaly_type.value))
            else:
                primary_cause = self._anomaly_to_cause(primary_anomaly.anomaly_type)
            
            analysis = RootCauseAnalysis(
                primary_cause=primary_cause,
                confidence=confidence,
                affected_services=sorted(all_affected),
                root_service=root_service,
                contributing_anomalies=[a.anomaly_type for a in anomalies],
                analysis_reasoning=reasoning,
                metadata={
                    "pattern_matched": pattern_match.get("likely_cause") if pattern_match else None,
                    "anomaly_count": len(anomalies),
                    "highest_severity": primary_anomaly.severity.value,
                },
            )
            
            logger.info(
                "Root cause analysis complete",
                primary_cause=primary_cause,
                confidence=confidence,
                root_service=root_service,
            )
            
            return analysis
    
    def _find_pattern_match(self, anomaly_types: set[AnomalyType]) -> dict[str, Any] | None:
        """Check if anomalies match a known pattern."""
        for pattern in self._patterns.values():
            conditions = set(pattern.get("conditions", []))
            if conditions.issubset(anomaly_types):
                return pattern
        return None
    
    def _find_root_service(self, services: list[str]) -> str | None:
        """Find the most upstream affected service."""
        if not services:
            return None
        
        # Score services by how many other services depend on them
        dependency_scores: dict[str, int] = {}
        for service in services:
            score = 0
            for dependent, deps in self._dependencies.items():
                if service in deps and dependent in services:
                    score += 1
            dependency_scores[service] = score
        
        # Return service with highest score (most depended upon)
        if dependency_scores:
            return max(dependency_scores.items(), key=lambda x: x[1])[0]
        return services[0] if services else None
    
    def _severity_score(self, severity: Severity) -> int:
        """Convert severity to numeric score."""
        return {
            Severity.LOW: 1,
            Severity.MEDIUM: 2,
            Severity.HIGH: 3,
            Severity.CRITICAL: 4,
        }.get(severity, 0)
    
    def _anomaly_to_cause(self, anomaly_type: AnomalyType) -> str:
        """Convert anomaly type to human-readable cause."""
        causes = {
            AnomalyType.LATENCY_SPIKE: "High latency in request processing",
            AnomalyType.ERROR_STORM: "Elevated error rate across services",
            AnomalyType.KAFKA_LAG: "Message processing backlog",
            AnomalyType.DB_CONTENTION: "Database performance degradation",
            AnomalyType.MEMORY_PRESSURE: "Memory exhaustion",
            AnomalyType.CPU_SATURATION: "CPU resource exhaustion",
            AnomalyType.CONNECTION_EXHAUSTION: "Connection pool exhaustion",
        }
        return causes.get(anomaly_type, str(anomaly_type.value))
    
    def _generate_reasoning(
        self,
        primary: Anomaly,
        all_anomalies: list[Anomaly],
        pattern: dict[str, Any] | None,
        root_service: str | None,
    ) -> str:
        """Generate human-readable reasoning for the analysis."""
        parts = []
        
        # Primary anomaly
        parts.append(
            f"Primary issue detected: {primary.anomaly_type.value} "
            f"({primary.severity.value} severity, {primary.confidence:.0%} confidence)."
        )
        
        # Pattern match
        if pattern:
            parts.append(
                f"Pattern identified: {pattern.get('likely_cause')}."
            )
        
        # Multiple anomalies
        if len(all_anomalies) > 1:
            other_types = [a.anomaly_type.value for a in all_anomalies if a != primary]
            parts.append(
                f"Contributing factors: {', '.join(other_types)}."
            )
        
        # Root service
        if root_service:
            parts.append(
                f"Most upstream affected component: {root_service}."
            )
        
        # Affected services
        if primary.affected_services:
            parts.append(
                f"Services impacted: {', '.join(primary.affected_services[:5])}."
            )
        
        return " ".join(parts)
