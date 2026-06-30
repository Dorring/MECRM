"""
LangGraph workflow for Dev Experience Agent.

Orchestrates anomaly detection, root cause analysis, and suggestion generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from langgraph.graph import StateGraph, END
from opentelemetry import trace

from .anomaly_detector import AnomalyDetector, Anomaly
from .root_cause import RootCauseAnalyzer, RootCauseAnalysis
from .suggestion_engine import SuggestionEngine, DevInsight

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass
class DevXState:
    """State for DevX analysis workflow."""
    metrics: dict[str, Any] = field(default_factory=dict)
    
    # Intermediate state
    anomalies: list[Anomaly] = field(default_factory=list)
    root_cause: RootCauseAnalysis | None = None
    insight: DevInsight | None = None
    
    # Output
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


async def detect_anomalies(state: DevXState) -> DevXState:
    """Detect anomalies from metrics."""
    with tracer.start_as_current_span("graph.detect_anomalies"):
        try:
            detector = AnomalyDetector()
            state.anomalies = detector.detect_anomalies(state.metrics)
            logger.debug("Anomalies detected", count=len(state.anomalies))
        except Exception as e:
            logger.error("Anomaly detection failed", error=str(e))
            state.error = f"Anomaly detection failed: {str(e)}"
        
        return state


async def analyze_root_cause(state: DevXState) -> DevXState:
    """Analyze root cause from anomalies."""
    if state.error or not state.anomalies:
        return state
    
    with tracer.start_as_current_span("graph.analyze_root_cause"):
        try:
            analyzer = RootCauseAnalyzer()
            state.root_cause = analyzer.analyze(state.anomalies)
            logger.debug(
                "Root cause analyzed",
                primary=state.root_cause.primary_cause if state.root_cause else None,
            )
        except Exception as e:
            logger.error("Root cause analysis failed", error=str(e))
            state.error = f"Root cause analysis failed: {str(e)}"
        
        return state


async def generate_suggestions(state: DevXState) -> DevXState:
    """Generate remediation suggestions."""
    if state.error or not state.root_cause:
        return state
    
    with tracer.start_as_current_span("graph.generate_suggestions"):
        try:
            suggester = SuggestionEngine()
            anomaly_types = [a.anomaly_type for a in state.anomalies]
            state.insight = suggester.generate_insight(state.root_cause, anomaly_types)
            logger.debug(
                "Suggestions generated",
                count=len(state.insight.suggested_actions) if state.insight else 0,
            )
        except Exception as e:
            logger.error("Suggestion generation failed", error=str(e))
            state.error = f"Suggestion generation failed: {str(e)}"
        
        return state


async def format_response(state: DevXState) -> DevXState:
    """Format the final response."""
    if state.error:
        state.response = {
            "success": False,
            "error": state.error,
        }
        return state
    
    if not state.anomalies:
        state.response = {
            "success": True,
            "status": "healthy",
            "message": "No anomalies detected",
        }
        return state
    
    if not state.insight:
        state.response = {
            "success": True,
            "status": "partial",
            "anomaly_count": len(state.anomalies),
            "message": "Anomalies detected but no root cause identified",
        }
        return state
    
    state.response = {
        "success": True,
        "status": "insight_generated",
        "incident_type": state.insight.incident_type,
        "severity": state.insight.severity,
        "confidence": state.insight.confidence,
        "suspected_services": state.insight.suspected_services,
        "root_cause": state.insight.root_cause,
        "analysis": state.insight.analysis,
        "suggested_actions": [
            {
                "action": s.action,
                "priority": s.priority,
                "category": s.category,
                "estimated_impact": s.estimated_impact,
                "requires_approval": s.requires_approval,
                "documentation_link": s.documentation_link,
            }
            for s in state.insight.suggested_actions
        ],
    }
    
    return state


def build_devx_graph() -> StateGraph:
    """Build the LangGraph workflow for DevX analysis."""
    
    # Build graph
    graph = StateGraph(DevXState)
    
    graph.add_node("detect_anomalies", detect_anomalies)
    graph.add_node("analyze_root_cause", analyze_root_cause)
    graph.add_node("generate_suggestions", generate_suggestions)
    graph.add_node("format_response", format_response)
    
    graph.set_entry_point("detect_anomalies")
    graph.add_edge("detect_anomalies", "analyze_root_cause")
    graph.add_edge("analyze_root_cause", "generate_suggestions")
    graph.add_edge("generate_suggestions", "format_response")
    graph.add_edge("format_response", END)
    
    return graph.compile()
