"""
Suggestion Engine - Generates actionable remediation suggestions.

Provides specific, actionable recommendations based on root cause analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry import trace

from .anomaly_detector import AnomalyType
from .root_cause import RootCauseAnalysis

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class Suggestion:
    """Actionable remediation suggestion."""
    action: str
    priority: int  # 1 = highest
    category: str
    estimated_impact: str
    requires_approval: bool
    documentation_link: str | None


@dataclass(frozen=True)
class DevInsight:
    """Complete insight with analysis and suggestions."""
    incident_type: str
    severity: str
    confidence: float
    suspected_services: list[str]
    root_cause: str
    analysis: str
    suggested_actions: list[Suggestion]
    metadata: dict[str, Any]


# Remediation playbooks by anomaly type
REMEDIATION_PLAYBOOKS: dict[AnomalyType, list[dict[str, Any]]] = {
    AnomalyType.LATENCY_SPIKE: [
        {
            "action": "Check for slow database queries in logs",
            "priority": 1,
            "category": "investigation",
            "impact": "Identify query bottlenecks",
            "approval": False,
            "docs": "/docs/runbooks/slow-queries.md",
        },
        {
            "action": "Scale affected service horizontally",
            "priority": 2,
            "category": "scaling",
            "impact": "Distribute load across more instances",
            "approval": True,
            "docs": "/docs/runbooks/scaling.md",
        },
        {
            "action": "Review recent deployments for regressions",
            "priority": 3,
            "category": "investigation",
            "impact": "Identify code changes causing slowdown",
            "approval": False,
            "docs": None,
        },
    ],
    AnomalyType.ERROR_STORM: [
        {
            "action": "Check error logs for root cause exceptions",
            "priority": 1,
            "category": "investigation",
            "impact": "Identify the error source",
            "approval": False,
            "docs": "/docs/runbooks/error-investigation.md",
        },
        {
            "action": "Enable circuit breaker for failing downstream",
            "priority": 2,
            "category": "mitigation",
            "impact": "Prevent cascading failures",
            "approval": True,
            "docs": "/docs/runbooks/circuit-breakers.md",
        },
        {
            "action": "Rollback recent deployment if regression detected",
            "priority": 3,
            "category": "remediation",
            "impact": "Restore previous stable version",
            "approval": True,
            "docs": "/docs/runbooks/rollback.md",
        },
    ],
    AnomalyType.KAFKA_LAG: [
        {
            "action": "Scale consumer group by adding replicas",
            "priority": 1,
            "category": "scaling",
            "impact": "Increase processing throughput",
            "approval": True,
            "docs": "/docs/runbooks/kafka-scaling.md",
        },
        {
            "action": "Inspect offset commits for stuck consumers",
            "priority": 2,
            "category": "investigation",
            "impact": "Identify blocked consumers",
            "approval": False,
            "docs": "/docs/runbooks/kafka-debugging.md",
        },
        {
            "action": "Check for message processing errors in DLQ",
            "priority": 3,
            "category": "investigation",
            "impact": "Identify poison messages",
            "approval": False,
            "docs": "/docs/runbooks/dlq-analysis.md",
        },
    ],
    AnomalyType.DB_CONTENTION: [
        {
            "action": "Identify and optimize slow queries",
            "priority": 1,
            "category": "optimization",
            "impact": "Reduce query execution time",
            "approval": False,
            "docs": "/docs/runbooks/query-optimization.md",
        },
        {
            "action": "Check for table lock contention",
            "priority": 2,
            "category": "investigation",
            "impact": "Identify blocking transactions",
            "approval": False,
            "docs": "/docs/runbooks/lock-analysis.md",
        },
        {
            "action": "Consider read replica for read-heavy workloads",
            "priority": 3,
            "category": "architecture",
            "impact": "Offload read traffic from primary",
            "approval": True,
            "docs": "/docs/runbooks/read-replicas.md",
        },
    ],
    AnomalyType.MEMORY_PRESSURE: [
        {
            "action": "Analyze heap dumps for memory leaks",
            "priority": 1,
            "category": "investigation",
            "impact": "Identify memory leak source",
            "approval": False,
            "docs": "/docs/runbooks/memory-analysis.md",
        },
        {
            "action": "Increase memory limits for affected pods",
            "priority": 2,
            "category": "scaling",
            "impact": "Provide more memory headroom",
            "approval": True,
            "docs": "/docs/runbooks/resource-limits.md",
        },
        {
            "action": "Restart affected services to clear memory",
            "priority": 3,
            "category": "mitigation",
            "impact": "Temporary relief, may lose state",
            "approval": True,
            "docs": None,
        },
    ],
    AnomalyType.CPU_SATURATION: [
        {
            "action": "Profile application for CPU hotspots",
            "priority": 1,
            "category": "investigation",
            "impact": "Identify inefficient code paths",
            "approval": False,
            "docs": "/docs/runbooks/cpu-profiling.md",
        },
        {
            "action": "Scale service horizontally",
            "priority": 2,
            "category": "scaling",
            "impact": "Distribute CPU load",
            "approval": True,
            "docs": "/docs/runbooks/scaling.md",
        },
        {
            "action": "Increase CPU limits for affected pods",
            "priority": 3,
            "category": "scaling",
            "impact": "Allow more CPU usage",
            "approval": True,
            "docs": "/docs/runbooks/resource-limits.md",
        },
    ],
    AnomalyType.CONNECTION_EXHAUSTION: [
        {
            "action": "Increase connection pool size",
            "priority": 1,
            "category": "configuration",
            "impact": "Allow more concurrent connections",
            "approval": True,
            "docs": "/docs/runbooks/connection-pools.md",
        },
        {
            "action": "Check for connection leaks in code",
            "priority": 2,
            "category": "investigation",
            "impact": "Identify unclosed connections",
            "approval": False,
            "docs": "/docs/runbooks/connection-leaks.md",
        },
        {
            "action": "Implement connection timeout and retry logic",
            "priority": 3,
            "category": "code",
            "impact": "More resilient connection handling",
            "approval": False,
            "docs": None,
        },
    ],
}


class SuggestionEngine:
    """Generates remediation suggestions based on analysis."""
    
    def __init__(self, playbooks: dict[AnomalyType, list[dict[str, Any]]] | None = None):
        self._playbooks = playbooks or REMEDIATION_PLAYBOOKS
    
    def generate_insight(
        self,
        analysis: RootCauseAnalysis,
        anomaly_types: list[AnomalyType],
    ) -> DevInsight:
        """
        Generate a complete insight with suggestions.
        
        Args:
            analysis: Root cause analysis result
            anomaly_types: List of detected anomaly types
        
        Returns:
            Complete DevInsight with suggestions
        """
        with tracer.start_as_current_span("suggestion_engine.generate"):
            # Collect suggestions from all relevant playbooks
            all_suggestions: list[Suggestion] = []
            for anomaly_type in anomaly_types:
                playbook = self._playbooks.get(anomaly_type, [])
                for item in playbook:
                    suggestion = Suggestion(
                        action=item["action"],
                        priority=item["priority"],
                        category=item["category"],
                        estimated_impact=item["impact"],
                        requires_approval=item["approval"],
                        documentation_link=item.get("docs"),
                    )
                    all_suggestions.append(suggestion)
            
            # Deduplicate and sort by priority
            seen_actions: set[str] = set()
            unique_suggestions: list[Suggestion] = []
            for s in sorted(all_suggestions, key=lambda x: x.priority):
                if s.action not in seen_actions:
                    seen_actions.add(s.action)
                    unique_suggestions.append(s)
            
            # Limit to top 5 suggestions
            top_suggestions = unique_suggestions[:5]
            
            # Determine severity
            severity = self._determine_severity(anomaly_types, analysis.confidence)
            
            insight = DevInsight(
                incident_type=anomaly_types[0].value if anomaly_types else "unknown",
                severity=severity,
                confidence=analysis.confidence,
                suspected_services=analysis.affected_services,
                root_cause=analysis.primary_cause,
                analysis=analysis.analysis_reasoning,
                suggested_actions=top_suggestions,
                metadata={
                    "root_service": analysis.root_service,
                    "contributing_factors": [a.value for a in analysis.contributing_anomalies],
                },
            )
            
            logger.info(
                "Insight generated",
                incident_type=insight.incident_type,
                suggestion_count=len(top_suggestions),
            )
            
            return insight
    
    def _determine_severity(
        self,
        anomaly_types: list[AnomalyType],
        confidence: float,
    ) -> str:
        """Determine overall incident severity."""
        # Critical if multiple severe anomalies or high confidence
        critical_types = {
            AnomalyType.ERROR_STORM,
            AnomalyType.MEMORY_PRESSURE,
            AnomalyType.CPU_SATURATION,
        }
        
        critical_count = sum(1 for a in anomaly_types if a in critical_types)
        
        if critical_count >= 2 or (critical_count >= 1 and confidence >= 0.9):
            return "critical"
        elif critical_count >= 1 or confidence >= 0.8:
            return "high"
        elif len(anomaly_types) >= 2:
            return "medium"
        else:
            return "low"
