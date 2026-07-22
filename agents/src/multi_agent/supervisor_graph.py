"""Phase 4 LangGraph Adapter for :class:`SupervisorRuntime`.

This module is intentionally thin — it wraps the Runtime in a 5-node
LangGraph so callers that already use LangGraph (e.g. the existing
Chat Graph) can compose the Supervisor uniformly.

Nodes:

* ``validate_plan`` — run pre-flight validation.
* ``initialize_run`` acquire the idempotency lease.
* ``execute_dag`` — hand off to :class:`DagScheduler`.
* ``merge_results`` — call :func:`merge_parallel_results`.
* ``finalize_run`` — persist via :class:`RunStore`.

Important: the graph **does not** re-implement any Scheduler, budget,
or retry logic.  Every node delegates to the Runtime.  A fake Runtime
can be substituted in tests to verify the graph routes correctly
without performing real work.

The graph is **not** registered in any application startup.  Phase 5
will wire it into the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, StateGraph

from multi_agent.execution import (
    ExecutionCancellation,
    SupervisorConfig,
    SupervisorRunResult,
)
from multi_agent.invocation import AgentInvoker
from multi_agent.planning import PlanDraft
from multi_agent.registry import AgentRegistry
from multi_agent.run_store import RunStore


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


@dataclass
class SupervisorGraphState:
    """Mutable state passed between graph nodes.

    The graph intentionally reuses the Runtime's return value rather
    than mirroring fields — the state just carries inputs and the
    final result.
    """

    plan: PlanDraft
    registry: AgentRegistry
    config: SupervisorConfig | None = None
    cancellation: ExecutionCancellation | None = None
    invoker: AgentInvoker | None = None
    run_store: RunStore | None = None
    result: SupervisorRunResult | None = None
    error: Exception | None = None


# ---------------------------------------------------------------------------
# Fake runtime for tests
# ---------------------------------------------------------------------------


@dataclass
class FakeSupervisorRuntime:
    """Test double that records calls and returns a preset result.

    Tests substitute this for :class:`SupervisorRuntime` to verify
    graph routing without performing real Handler invocations.
    """

    result: SupervisorRunResult | None = None
    error: Exception | None = None
    calls: list[tuple[PlanDraft, AgentRegistry]] = field(default_factory=list)

    async def execute(
        self,
        plan: PlanDraft,
        registry: AgentRegistry,
        *,
        config: SupervisorConfig | None = None,
        cancellation: ExecutionCancellation | None = None,
    ) -> SupervisorRunResult:
        self.calls.append((plan, registry))
        if self.error is not None:
            raise self.error
        assert self.result is not None, "FakeSupervisorRuntime.result must be set"
        return self.result


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


# Type alias for any object that quacks like SupervisorRuntime.execute.
# We accept the fake too, so tests do not need to monkeypatch.
_SupervisorLike = Any  # SupervisorRuntime | FakeSupervisorRuntime


def build_supervisor_graph(runtime: _SupervisorLike):
    """Build a LangGraph that wraps *runtime*.

    The returned graph is compiled but not started.  Callers invoke
    it via ``graph.ainvoke(initial_state)``.
    """

    async def validate_plan(state: SupervisorGraphState) -> dict[str, Any]:
        # The Runtime performs the real validation inside ``execute``;
        # the graph node exists so the trace has a clear boundary
        # and tests can assert the node was visited.  We do not
        # duplicate validation here.
        if state.plan is None:
            raise ValueError("SupervisorGraphState.plan must be set")
        if state.registry is None:
            raise ValueError("SupervisorGraphState.registry must be set")
        return {}

    async def initialize_run(state: SupervisorGraphState) -> dict[str, Any]:
        # Lease acquisition happens inside ``Runtime.execute`` — this
        # node is a routing marker only.
        return {}

    async def execute_dag(state: SupervisorGraphState) -> dict[str, Any]:
        try:
            result = await runtime.execute(
                state.plan,
                state.registry,
                config=state.config,
                cancellation=state.cancellation,
            )
        except Exception as exc:  # noqa: BLE001
            # Capture so ``finalize_run`` can re-raise cleanly
            # rather than aborting LangGraph's executor.
            #
            # R1 P1: only catch ``Exception`` — let
            # ``asyncio.CancelledError``, ``KeyboardInterrupt`` and
            # ``SystemExit`` propagate to the caller so task
            # cancellation works correctly.
            return {"error": exc}
        return {"result": result}

    async def merge_results(state: SupervisorGraphState) -> dict[str, Any]:
        # Merging happens inside ``Runtime.execute`` (which calls
        # ``merge_parallel_results``).  This node exists so the graph
        # exposes a stable seam between execution and finalization —
        # a Phase 5 reviewer / synthesizer node can hook in here.
        return {}

    async def finalize_run(state: SupervisorGraphState) -> dict[str, Any]:
        if state.error is not None:
            # Re-raise so callers see the original exception type.
            raise state.error
        return {}

    g: StateGraph = StateGraph(SupervisorGraphState)
    g.add_node("validate_plan", validate_plan)
    g.add_node("initialize_run", initialize_run)
    g.add_node("execute_dag", execute_dag)
    g.add_node("merge_results", merge_results)
    g.add_node("finalize_run", finalize_run)

    g.set_entry_point("validate_plan")
    g.add_edge("validate_plan", "initialize_run")
    g.add_edge("initialize_run", "execute_dag")
    g.add_edge("execute_dag", "merge_results")
    g.add_edge("merge_results", "finalize_run")
    g.add_edge("finalize_run", END)

    return g.compile()


__all__ = [
    "FakeSupervisorRuntime",
    "SupervisorGraphState",
    "build_supervisor_graph",
]
