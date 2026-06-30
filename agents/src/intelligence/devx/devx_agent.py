"""
Dev Experience Agent - AI-powered operational insights.

Monitors system health and provides advisory recommendations.
This is READ-ONLY - no automatic remediation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import asyncpg
import structlog
from opentelemetry import trace

from agents.base import BaseAgent
from orchestrator.config import settings

from .anomaly_detector import AnomalyDetector
from .root_cause import RootCauseAnalyzer
from .suggestion_engine import SuggestionEngine, DevInsight

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DevExperienceAgent(BaseAgent):
    """AI SRE agent for operational insights."""
    
    def __init__(self):
        super().__init__(
            agent_id="devx-agent",
            agent_type="devx",
            capabilities=["devx:analyze", "devx:insights"],
        )
        self._pool: asyncpg.Pool | None = None
        self._detector = AnomalyDetector()
        self._analyzer = RootCauseAnalyzer()
        self._suggester = SuggestionEngine()
    
    async def initialize(self, producer):
        await super().initialize(producer)
        if not self._pool:
            self._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
    
    async def cleanup(self):
        if self._pool:
            await self._pool.close()
        self._pool = None
        await super().cleanup()
    
    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process observability signals and generate insights.
        
        This would typically be triggered periodically or by metric alerts.
        """
        with tracer.start_as_current_span("devx_agent.process") as span:
            # Extract metrics from event (would come from Prometheus/OTEL)
            metrics = event.get("data", {}).get("metrics", {})
            if not metrics:
                return {"status": "skipped", "reason": "no metrics"}
            
            span.set_attribute("metric_count", len(metrics))
            
            # Detect anomalies
            anomalies = self._detector.detect_anomalies(metrics)
            if not anomalies:
                return {"status": "healthy", "anomaly_count": 0}
            
            # Analyze root cause
            analysis = self._analyzer.analyze(anomalies)
            if not analysis:
                return {"status": "partial", "anomaly_count": len(anomalies)}
            
            # Generate insight
            anomaly_types = [a.anomaly_type for a in anomalies]
            insight = self._suggester.generate_insight(analysis, anomaly_types)
            
            # Persist insight
            if self._pool:
                await self._persist_insight(insight)
            
            # Emit event
            await self.emit_event(
                topic="crm.intelligence.dev-insight-generated",
                event_type="crm.intelligence.dev-insight-generated",
                tenant_id="system",  # DevX is system-wide
                data={
                    "incident_type": insight.incident_type,
                    "severity": insight.severity,
                    "confidence": insight.confidence,
                    "suspected_services": insight.suspected_services,
                    "root_cause": insight.root_cause,
                    "suggestion_count": len(insight.suggested_actions),
                    "created_at": _utc_now(),
                },
            )
            
            logger.info(
                "DevX insight generated",
                incident_type=insight.incident_type,
                severity=insight.severity,
            )
            
            return {
                "status": "insight_generated",
                "incident_type": insight.incident_type,
                "severity": insight.severity,
                "confidence": insight.confidence,
            }
    
    async def _persist_insight(self, insight: DevInsight) -> None:
        """Store insight in database."""
        if not self._pool:
            return
        
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO devx_insights (
                    incident_type, severity, confidence,
                    suspected_services, suggested_actions,
                    signals, status, created_at
                )
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, 'active', now())
                """,
                insight.incident_type,
                insight.severity,
                insight.confidence,
                insight.suspected_services,
                [
                    {
                        "action": s.action,
                        "priority": s.priority,
                        "category": s.category,
                        "impact": s.estimated_impact,
                        "requires_approval": s.requires_approval,
                        "docs": s.documentation_link,
                    }
                    for s in insight.suggested_actions
                ],
                insight.metadata,
            )
    
    async def get_active_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get active insights for dashboard."""
        if not self._pool:
            return []
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, incident_type, severity, confidence,
                       suspected_services, suggested_actions, signals,
                       status, created_at
                FROM devx_insights
                WHERE status = 'active'
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            
            return [
                {
                    "id": row["id"],
                    "incident_type": row["incident_type"],
                    "severity": row["severity"],
                    "confidence": float(row["confidence"]),
                    "suspected_services": list(row["suspected_services"] or []),
                    "suggested_actions": list(row["suggested_actions"] or []),
                    "metadata": dict(row["signals"] or {}),
                    "status": row["status"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
                for row in rows
            ]
    
    async def acknowledge_insight(self, insight_id: str, user_id: str) -> bool:
        """Mark an insight as acknowledged."""
        if not self._pool:
            return False
        
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE devx_insights
                SET status = 'acknowledged', acknowledged_by = $2::uuid
                WHERE id = $1::uuid AND status = 'active'
                """,
                insight_id,
                user_id,
            )
            return "UPDATE 1" in result
    
    async def resolve_insight(self, insight_id: str) -> bool:
        """Mark an insight as resolved."""
        if not self._pool:
            return False
        
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE devx_insights
                SET status = 'resolved', resolved_at = now()
                WHERE id = $1::uuid
                """,
                insight_id,
            )
            return "UPDATE 1" in result
