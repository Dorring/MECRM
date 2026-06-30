"""
LangGraph workflow for Digital Customer Twins.

Orchestrates twin building, simulation, and event emission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg
import structlog
from langgraph.graph import StateGraph, END
from opentelemetry import trace

from .twin_builder import TwinBuilder, TwinProfile
from .simulator import TwinSimulator, SimulationResult

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass
class TwinDeps:
    """Dependencies for twin graph."""
    conn: asyncpg.Connection


@dataclass
class TwinState:
    """State for twin simulation workflow."""
    tenant_id: str
    customer_id: str
    user_id: str
    scenario: str
    params: dict[str, Any] = field(default_factory=dict)
    
    # Intermediate state
    twin_profile: TwinProfile | None = None
    simulation_result: SimulationResult | None = None
    
    # Output
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


async def load_or_build_twin(state: TwinState, deps: TwinDeps) -> TwinState:
    """Load existing twin or build a new one."""
    with tracer.start_as_current_span("graph.load_or_build_twin"):
        try:
            builder = TwinBuilder(deps.conn)
            
            # Try to get cached twin first
            twin = await builder.get_twin(
                tenant_id=state.tenant_id,
                customer_id=state.customer_id,
            )
            
            # Build fresh if not found or stale
            if not twin:
                twin = await builder.build_twin(
                    tenant_id=state.tenant_id,
                    customer_id=state.customer_id,
                )
            
            state.twin_profile = twin
            logger.debug(
                "Twin loaded",
                customer_id=state.customer_id,
                confidence=twin.confidence if twin else 0,
            )
        except Exception as e:
            logger.error("Failed to load twin", error=str(e))
            state.error = f"Failed to load customer twin: {str(e)}"
        
        return state


async def run_simulation(state: TwinState, deps: TwinDeps) -> TwinState:
    """Run the simulation scenario."""
    if state.error:
        return state
    
    with tracer.start_as_current_span("graph.run_simulation"):
        try:
            simulator = TwinSimulator(deps.conn)
            
            result = await simulator.simulate(
                tenant_id=state.tenant_id,
                customer_id=state.customer_id,
                scenario=state.scenario,
                user_id=state.user_id,
                params=state.params,
            )
            
            state.simulation_result = result
            logger.debug(
                "Simulation completed",
                scenario=state.scenario,
                confidence=result.confidence,
            )
        except Exception as e:
            logger.error("Simulation failed", error=str(e))
            state.error = f"Simulation failed: {str(e)}"
        
        return state


async def format_response(state: TwinState, deps: TwinDeps) -> TwinState:
    """Format the final response."""
    if state.error:
        state.response = {
            "success": False,
            "error": state.error,
        }
        return state
    
    result = state.simulation_result
    if not result:
        state.response = {
            "success": False,
            "error": "No simulation result",
        }
        return state
    
    state.response = {
        "success": True,
        "customer_id": result.customer_id,
        "scenario": result.scenario,
        "outcomes": result.outcomes,
        "explanation": result.explanation,
        "factors": result.factors,
        "confidence": result.confidence,
        "simulated_at": result.simulated_at,
    }
    
    return state


def build_twin_graph(deps: TwinDeps) -> StateGraph:
    """Build the LangGraph workflow for twin simulation."""
    
    async def load_twin_node(state: TwinState) -> TwinState:
        return await load_or_build_twin(state, deps)
    
    async def simulate_node(state: TwinState) -> TwinState:
        return await run_simulation(state, deps)
    
    async def format_node(state: TwinState) -> TwinState:
        return await format_response(state, deps)
    
    # Build graph
    graph = StateGraph(TwinState)
    
    graph.add_node("load_twin", load_twin_node)
    graph.add_node("simulate", simulate_node)
    graph.add_node("format_response", format_node)
    
    graph.set_entry_point("load_twin")
    graph.add_edge("load_twin", "simulate")
    graph.add_edge("simulate", "format_response")
    graph.add_edge("format_response", END)
    
    return graph.compile()
