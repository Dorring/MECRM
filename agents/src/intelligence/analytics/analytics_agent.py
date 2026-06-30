from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import asyncpg
import structlog
from opentelemetry import trace

from agents.base import BaseAgent
from orchestrator.config import settings

from .graph import AnalyticsDeps, AnalyticsState, build_analytics_graph


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PredictiveAnalyticsAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_id="predictive-analytics-agent",
            agent_type="analytics_predictive",
            capabilities=["predictions:generate"],
        )
        self._pool: asyncpg.Pool | None = None

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
        tenant_id = str(event.get("tenantid") or "")
        if not tenant_id:
            return {"status": "skipped"}

        if str(event.get("type") or "") != "crm.journey.updated":
            return {"status": "skipped"}

        if not self._pool:
            self._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)

        with tracer.start_as_current_span("predict") as span:
            span.set_attribute("tenant_id", tenant_id)
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                    graph = build_analytics_graph(deps=AnalyticsDeps(conn=conn))
                    out: AnalyticsState = await graph.ainvoke(AnalyticsState(tenant_id=tenant_id, journey_event=event))
                    predictions = out.predictions or []

                    emitted = 0
                    for p in predictions:
                        await self.emit_event(
                            topic="crm.analytics.prediction-generated",
                            event_type="crm.analytics.prediction-generated",
                            tenant_id=tenant_id,
                            correlation_id=event.get("correlationid"),
                            data={
                                "tenant_id": tenant_id,
                                "entity_id": p.entity_id,
                                "entity_type": p.entity_type,
                                "prediction_type": p.prediction_type,
                                "probability": p.probability,
                                "risk_level": p.risk_level,
                                "explanation": p.explanation,
                                "features": p.features,
                                "created_at": _utc_now(),
                                "model_version": p.model_version,
                            },
                        )
                        emitted += 1

                    logger.debug("Predictions emitted", tenant_id=tenant_id, count=emitted)
                    return {"status": "completed", "emitted": emitted}

