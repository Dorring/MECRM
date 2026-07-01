"""
Analytics Agent

Responsible for:
- Trend analysis
- Pipeline metrics
- Forecasting
- Anomaly detection
"""

import json
from typing import Dict, Any, Optional
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from .base import BaseAgent

logger = structlog.get_logger()


class ForecastPrediction(BaseModel):
    """A single forecast period prediction produced by the LLM."""

    period: str = Field(min_length=1, max_length=200)
    value: float
    confidence_range: Optional[list[float]] = Field(default=None, max_length=2)

    @field_validator("confidence_range")
    @classmethod
    def _validate_confidence_range(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is None:
            return v
        if len(v) != 2:
            raise ValueError("confidence_range must contain exactly two values [low, high]")
        if v[0] > v[1]:
            raise ValueError("confidence_range low must be <= high")
        return v


class ForecastResult(BaseModel):
    """Strongly-typed contract for LLM forecast output.

    Model output is validated against this schema before any event is emitted,
    so a malformed LLM response (e.g. predictions not a list, confidence not a
    number) can never drive a downstream write. Mirrors the ResolutionSuggestion
    pattern used by SupportAgent.suggest_resolution.
    """

    forecast_type: str = Field(min_length=1, max_length=200)
    time_range: str = Field(min_length=1, max_length=200)
    predictions: list[ForecastPrediction] = Field(default_factory=list, max_length=100)
    factors: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("predictions")
    @classmethod
    def _validate_predictions(cls, v: list[ForecastPrediction]) -> list[ForecastPrediction]:
        if not isinstance(v, list):
            raise ValueError("predictions must be a list")
        return v


class AnalyticsAgent(BaseAgent):
    """AI agent for analytics and insights."""
    
    def __init__(self):
        super().__init__(
            agent_id="analytics-agent",
            agent_type="analytics",
            capabilities=[
                "reports:generate",
                "trends:analyze",
                "forecasts:create",
            ],
        )
        
    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Process a generic event."""
        event_type = event.get("type", "")
        
        if "leads" in event_type:
            return await self.track_lead_progression(event)
        elif "deals" in event_type:
            return await self.track_pipeline_movement(event)
        elif "tickets" in event_type:
            return await self.track_ticket_metrics(event)
            
        return {"status": "skipped"}
        
    async def track_lead_progression(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Track lead status changes for analytics."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        
        # Record the metric
        await self.emit_event(
            topic="crm.agents.action-executed",
            event_type="crm.agents.metric-recorded",
            tenant_id=tenant_id,
            data={
                "metricType": "lead_progression",
                "entityType": "lead",
                "entityId": data.get("leadId"),
                "previousStatus": data.get("previousStatus"),
                "newStatus": data.get("newStatus"),
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "recordedBy": self.agent_id,
            },
        )
        
        logger.debug("Lead progression tracked", lead_id=data.get("leadId"))
        return {"status": "completed"}
        
    async def track_pipeline_movement(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Track deal pipeline movements."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        
        # Detect anomalies (e.g., skipped stages, backwards movement)
        previous_stage = data.get("previousStage", "")
        new_stage = data.get("newStage", "")
        
        stage_order = [
            "prospecting",
            "qualification", 
            "proposal",
            "negotiation",
            "closed_won",
            "closed_lost",
        ]
        
        anomaly = None
        prev_idx = stage_order.index(previous_stage) if previous_stage in stage_order else -1
        new_idx = stage_order.index(new_stage) if new_stage in stage_order else -1
        
        if prev_idx > new_idx and new_stage not in ["closed_lost"]:
            anomaly = "backwards_movement"
        elif new_idx - prev_idx > 1:
            anomaly = "skipped_stages"
            
        await self.emit_event(
            topic="crm.agents.action-executed",
            event_type="crm.agents.metric-recorded",
            tenant_id=tenant_id,
            data={
                "metricType": "pipeline_movement",
                "entityType": "deal",
                "entityId": data.get("dealId"),
                "previousStage": previous_stage,
                "newStage": new_stage,
                "amount": data.get("amount"),
                "anomaly": anomaly,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "recordedBy": self.agent_id,
            },
        )
        
        # Emit anomaly alert if detected
        if anomaly:
            await self.emit_event(
                topic="crm.agents.action-proposed",
                event_type="crm.agents.anomaly-detected",
                tenant_id=tenant_id,
                data={
                    "anomalyType": anomaly,
                    "entityType": "deal",
                    "entityId": data.get("dealId"),
                    "details": f"Deal moved from {previous_stage} to {new_stage}",
                    "detectedBy": self.agent_id,
                },
            )
            
        logger.debug(
            "Pipeline movement tracked",
            deal_id=data.get("dealId"),
            anomaly=anomaly,
        )
        
        return {"status": "completed", "anomaly": anomaly}
        
    async def track_ticket_metrics(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Track ticket metrics."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        
        await self.emit_event(
            topic="crm.agents.action-executed",
            event_type="crm.agents.metric-recorded",
            tenant_id=tenant_id,
            data={
                "metricType": "ticket_update",
                "entityType": "ticket",
                "entityId": data.get("ticketId"),
                "changes": data.get("changes", {}),
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "recordedBy": self.agent_id,
            },
        )
        
        return {"status": "completed"}
        
    async def generate_forecast(
        self,
        tenant_id: str,
        forecast_type: str,
        time_range: str,
    ) -> Dict[str, Any]:
        """Generate a forecast using historical data.

        The LLM response is Pydantic-validated (ForecastResult) before being
        returned. On ValidationError or JSON parse failure the method returns
        ``status="failed"`` with ``reason="invalid_model_output"``; the caller
        (router._handle_forecast_requested) gates emission on
        ``status == "completed"``, so a malformed model response can never
        drive a downstream write.
        """

        prompt = f"""Based on historical CRM data patterns, generate a {forecast_type} forecast
for the next {time_range}.

Consider:
1. Seasonal trends
2. Recent growth rates
3. Market conditions

Provide forecast in JSON format:
{{
    "forecast_type": "{forecast_type}",
    "time_range": "{time_range}",
    "predictions": [
        {{"period": "<period>", "value": <predicted_value>, "confidence_range": [<low>, <high>]}}
    ],
    "factors": ["<factor1>", "<factor2>"],
    "confidence": <0.0-1.0>
}}
"""

        try:
            response = await self.call_llm(prompt, tenant_id=tenant_id)
            raw = self._extract_json_object(response)
            forecast = ForecastResult.model_validate(raw)
        except ValidationError as e:
            logger.error(
                "Forecast failed schema validation",
                tenant_id=tenant_id,
                forecast_type=forecast_type,
                error=str(e),
            )
            return {"status": "failed", "reason": "invalid_model_output"}
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(
                "Forecast LLM response was not valid JSON",
                tenant_id=tenant_id,
                forecast_type=forecast_type,
                error=str(e),
            )
            return {"status": "failed", "reason": "invalid_model_output"}
        except Exception as e:
            logger.error("Forecast generation failed", error=str(e))
            return {"status": "failed", "error": str(e)}

        return {"status": "completed", "forecast": forecast.model_dump()}

    @staticmethod
    def _extract_json_object(response: str) -> Dict[str, Any]:
        """Extract the first JSON object from an LLM response.

        Raises ``json.JSONDecodeError`` if no valid JSON object is found, so the
        caller can route malformed output through the validation-failure path
        rather than silently emitting an empty forecast.
        """
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(response[start:end])
        raise json.JSONDecodeError("no JSON object in LLM response", response, 0)

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Parse JSON from LLM response (best-effort; returns {} on failure)."""
        try:
            return self._extract_json_object(response)
        except (json.JSONDecodeError, ValueError):
            return {}
