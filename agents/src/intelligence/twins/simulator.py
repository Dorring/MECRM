"""
Twin Simulator - Runs simulation scenarios against customer twins.

Provides probability distributions with explanations for business decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import asyncpg
import structlog
from opentelemetry import trace

from .twin_builder import TwinBuilder, TwinProfile
from .behavior_model import BehaviorModel

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


# Supported simulation scenarios
ScenarioType = Literal[
    "price_increase_5",
    "price_increase_10",
    "price_increase_20",
    "feature_removal",
    "contract_renewal",
    "upsell_small",
    "upsell_large",
]

SCENARIO_CONFIGS: dict[str, dict[str, Any]] = {
    "price_increase_5": {"type": "price", "increase_percent": 5},
    "price_increase_10": {"type": "price", "increase_percent": 10},
    "price_increase_20": {"type": "price", "increase_percent": 20},
    "feature_removal": {"type": "feature", "feature_name": "generic"},
    "contract_renewal": {"type": "renewal"},
    "upsell_small": {"type": "upsell", "value": 5000},
    "upsell_large": {"type": "upsell", "value": 25000},
}


@dataclass(frozen=True)
class SimulationResult:
    """Result of a twin simulation."""
    customer_id: str
    tenant_id: str
    scenario: str
    outcomes: dict[str, float]
    explanation: str
    factors: list[str]
    confidence: float
    simulated_at: str


class TwinSimulator:
    """Runs simulations on customer twins."""
    
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn
        self._builder = TwinBuilder(conn)
    
    async def simulate(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        scenario: str,
        user_id: str,
        params: dict[str, Any] | None = None,
    ) -> SimulationResult:
        """
        Run a simulation scenario for a customer.
        
        Args:
            tenant_id: Tenant ID
            customer_id: Customer ID to simulate
            scenario: Scenario type (e.g., "price_increase_10")
            user_id: User running the simulation (for audit)
            params: Optional additional parameters
        
        Returns:
            SimulationResult with probability distribution
        """
        with tracer.start_as_current_span("twin_simulator.simulate") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("customer_id", customer_id)
            span.set_attribute("scenario", scenario)
            
            # Get or build the customer twin
            twin = await self._builder.get_twin(tenant_id=tenant_id, customer_id=customer_id)
            if not twin:
                twin = await self._builder.build_twin(tenant_id=tenant_id, customer_id=customer_id)
            
            # Run the simulation
            model = BehaviorModel(twin.features)
            outcomes = self._run_scenario(model, scenario, params or {})
            
            # Generate explanation
            factors = model.get_explanation_factors(scenario)
            explanation = self._generate_explanation(scenario, outcomes, factors)
            
            confidence = model.get_model_confidence()
            simulated_at = datetime.now(timezone.utc).isoformat()
            
            result = SimulationResult(
                customer_id=customer_id,
                tenant_id=tenant_id,
                scenario=scenario,
                outcomes=outcomes,
                explanation=explanation,
                factors=factors,
                confidence=confidence,
                simulated_at=simulated_at,
            )
            
            # Log the simulation for audit
            await self._log_simulation(result, user_id, params)
            
            logger.info(
                "Simulation completed",
                tenant_id=tenant_id,
                customer_id=customer_id,
                scenario=scenario,
                confidence=confidence,
            )
            
            return result
    
    def _run_scenario(
        self,
        model: BehaviorModel,
        scenario: str,
        params: dict[str, Any],
    ) -> dict[str, float]:
        """Execute the specified scenario."""
        config = SCENARIO_CONFIGS.get(scenario, {})
        scenario_type = config.get("type", "unknown")
        
        if scenario_type == "price":
            increase = params.get("increase_percent") or config.get("increase_percent", 10)
            return model.predict_price_response(float(increase))
        
        elif scenario_type == "feature":
            feature_name = params.get("feature_name") or config.get("feature_name", "generic")
            return model.predict_feature_removal_response(feature_name)
        
        elif scenario_type == "renewal":
            return model.predict_renewal_response()
        
        elif scenario_type == "upsell":
            value = params.get("value") or config.get("value", 10000)
            return model.predict_upsell_response(float(value))
        
        else:
            # Unknown scenario - return uniform distribution
            logger.warning("Unknown scenario type", scenario=scenario)
            return {"unknown": 1.0}
    
    def _generate_explanation(
        self,
        scenario: str,
        outcomes: dict[str, float],
        factors: list[str],
    ) -> str:
        """Generate human-readable explanation for the simulation."""
        # Find the most likely outcome
        top_outcome = max(outcomes.items(), key=lambda x: x[1])
        outcome_name, probability = top_outcome
        
        # Build explanation
        scenario_desc = scenario.replace("_", " ")
        explanation_parts = [
            f"For the '{scenario_desc}' scenario, the most likely outcome is "
            f"'{outcome_name}' with {probability:.0%} probability."
        ]
        
        if factors:
            explanation_parts.append("Key factors considered:")
            for factor in factors[:3]:  # Limit to top 3 factors
                explanation_parts.append(f"• {factor}")
        
        return " ".join(explanation_parts)
    
    async def _log_simulation(
        self,
        result: SimulationResult,
        user_id: str,
        params: dict[str, Any] | None,
    ) -> None:
        """Log simulation to audit table."""
        try:
            await self._conn.execute(
                """
                INSERT INTO twin_simulation_log (
                    tenant_id, customer_id, user_id, scenario,
                    input_params, result, confidence, created_at
                )
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb, $6::jsonb, $7, now())
                """,
                result.tenant_id,
                result.customer_id,
                user_id,
                result.scenario,
                params or {},
                {
                    "outcomes": result.outcomes,
                    "explanation": result.explanation,
                    "factors": result.factors,
                },
                result.confidence,
            )
        except Exception as e:
            # Don't fail simulation if audit logging fails
            logger.error("Failed to log simulation", error=str(e))
    
    async def get_simulation_history(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get recent simulation history for a customer."""
        rows = await self._conn.fetch(
            """
            SELECT id::text, scenario, input_params, result, confidence, created_at
            FROM twin_simulation_log
            WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
            ORDER BY created_at DESC
            LIMIT $3
            """,
            tenant_id,
            customer_id,
            limit,
        )
        
        return [
            {
                "id": row["id"],
                "scenario": row["scenario"],
                "params": dict(row["input_params"] or {}),
                "result": dict(row["result"] or {}),
                "confidence": float(row["confidence"]),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]
