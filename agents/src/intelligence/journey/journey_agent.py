from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import asyncpg
import structlog
from opentelemetry import trace

from agents.base import BaseAgent
from orchestrator.config import settings

from .stage_classifier import classify_stage
from .timeline_builder import build_timeline_entries


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JourneyAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_id="journey-agent",
            agent_type="journey",
            capabilities=["journey:build", "journey:classify"],
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

        if not self._pool:
            self._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)

        with tracer.start_as_current_span("journey_build") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("event_type", str(event.get("type") or ""))
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                    entries = await build_timeline_entries(tenant_id=tenant_id, event=event, conn=conn)
                    if not entries:
                        return {"status": "skipped"}

                    emitted = 0
                    for entry in entries:
                        stage = await classify_stage(tenant_id=tenant_id, customer_id=entry.customer_id, conn=conn)
                        await self.emit_event(
                            topic="crm.journey.updated",
                            event_type="crm.journey.updated",
                            tenant_id=tenant_id,
                            correlation_id=event.get("correlationid"),
                            data={
                                "tenant_id": tenant_id,
                                "customer_id": entry.customer_id,
                                "stage": stage.stage,
                                "confidence": stage.confidence,
                                "updated_at": _utc_now(),
                                "timeline_entry": {
                                    "event_type": entry.event_type,
                                    "event_payload": entry.event_payload,
                                    "timestamp": entry.timestamp,
                                },
                                "features": stage.features,
                            },
                        )
                        emitted += 1

                    logger.debug("Journey updates emitted", tenant_id=tenant_id, count=emitted)
                    return {"status": "completed", "emitted": emitted}

