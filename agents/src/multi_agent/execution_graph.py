"""Phase 5B — LangGraph thin adapter for the Governed Executor.

A 6-node LangGraph that wraps :class:`GovernedExecutor.execute` so
callers that already use LangGraph can compose the executor
uniformly.  The graph does NOT re-implement any step of the 18-step
pipeline — every node delegates to :class:`GovernedExecutor` public
methods or simply surfaces errors as persistable
:class:`ReviewGraphError` records.

Nodes (Phase 5B Section 21):

* ``verify_review`` — trace boundary; verification is delegated to
  :meth:`GovernedExecutor.execute` (P1-1: Direct/Graph Error Parity).
* ``authorize`` — build authorizations for executable Proposals.
* ``resolve_approval`` — approval gate (create / consume decisions).
* ``reserve_idempotency`` — idempotency reservation.
* ``execute_actions`` — invoke the adapter via the executor.
* ``finalize_execution`` — receipt verification + batch assembly.

Routing: every node checks ``state.graph_error`` and routes to END
on failure so the graph never proceeds past a broken step.

Graph output is byte-for-byte identical to direct
:meth:`GovernedExecutor.execute` output — verified by
``test_execution_graph.py``.

P1-1 (R2): runtime dependencies (ApprovalStore, ExecutionStore,
ActionAdapterRegistry, KillSwitch, Clock, GovernedExecutor,
ExecutionOptions) are injected via a :class:`RuntimeDependencies`
closure object, NOT stored in :class:`ExecutionGraphState`.  The
state only carries serializable IDs, hashes, and the final result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, StateGraph

from multi_agent.action_adapter import ActionAdapterRegistry
from multi_agent.approval_contracts import Clock
from multi_agent.approval_gate import ApprovalStore
from multi_agent.execution_error_codes import ExecutionError
from multi_agent.execution_store import ExecutionStore
from multi_agent.governed_executor import (
    ExecutionBatchResult,
    ExecutionOptions,
    GovernedExecutor,
)
from multi_agent.review_contracts import (
    ReviewBatchResult,
    ReviewGraphError,
    ReviewRequest,
)

# ---------------------------------------------------------------------------
# Runtime dependencies (P1-1: closure-injected, never in graph state)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeDependencies:
    """P1-1: runtime dependencies injected via closure, not graph state.

    These objects are NOT serializable business state — they are live
    stores, registries, and executors that must not be persisted by
    LangGraph checkpointing.  ``build_execution_graph`` captures this
    object in the graph closure.
    """

    approval_store: ApprovalStore
    execution_store: ExecutionStore
    adapter_registry: ActionAdapterRegistry
    kill_switch: Any
    clock: Clock
    executor: GovernedExecutor
    options: ExecutionOptions = field(default_factory=ExecutionOptions)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


@dataclass
class ExecutionGraphState:
    """P1-1: only serializable IDs, hashes, and results.

    Runtime dependencies (ApprovalStore, ExecutionStore,
    ActionAdapterRegistry, KillSwitch, Clock, GovernedExecutor,
    ExecutionOptions) are injected via :class:`RuntimeDependencies`
    closure, not stored in graph state.  ``graph_error`` carries a
    persistable :class:`ReviewGraphError` when a node fails — no raw
    :class:`Exception` enters the State (Phase 5B Section 21,
    mirroring Phase 5A R2.1 P1-1).
    """

    request: ReviewRequest
    review_result: ReviewBatchResult
    # Runtime deps passed via closure, not state
    result: ExecutionBatchResult | None = None
    graph_error: ReviewGraphError | None = None


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_execution_graph(deps: RuntimeDependencies):
    """Build a LangGraph that wraps :class:`GovernedExecutor.execute`.

    Runtime dependencies are captured in the graph closure (P1-1),
    not in the graph state.

    The returned graph is compiled but not started.  Callers invoke
    it via ``graph.ainvoke(initial_state)``.

    The graph is **not** registered in any application startup —
    Phase 5C will wire it into the orchestrator.
    """

    async def verify_review(state: ExecutionGraphState) -> dict[str, Any]:
        # P1-1: Direct/Graph Error Parity — GovernedExecutor.execute
        # already performs all ReviewRequest / ReviewBatchResult
        # verification (pipeline steps 1-4).  Making this node a
        # no-op ensures the graph delegates to the executor so both
        # paths return the same BLOCKED batch result on invalid
        # inputs instead of the graph short-circuiting to END with
        # graph_error and result=None.
        return {}

    async def authorize(state: ExecutionGraphState) -> dict[str, Any]:
        if state.graph_error is not None:
            return {"graph_error": state.graph_error}
        # Authorization is built inside GovernedExecutor.execute; this
        # node is a trace boundary / future extension point.
        return {}

    async def resolve_approval(state: ExecutionGraphState) -> dict[str, Any]:
        if state.graph_error is not None:
            return {"graph_error": state.graph_error}
        # Approval resolution is inside GovernedExecutor.execute; this
        # node is a trace boundary / future extension point (e.g. a
        # human-in-the-loop escalation hook).
        return {}

    async def reserve_idempotency(state: ExecutionGraphState) -> dict[str, Any]:
        if state.graph_error is not None:
            return {"graph_error": state.graph_error}
        # Idempotency reservation is inside GovernedExecutor.execute.
        return {}

    async def execute_actions(state: ExecutionGraphState) -> dict[str, Any]:
        if state.graph_error is not None:
            return {"graph_error": state.graph_error}
        try:
            result = await deps.executor.execute(
                request=state.request,
                review_result=state.review_result,
                approval_store=deps.approval_store,
                execution_store=deps.execution_store,
                adapter_registry=deps.adapter_registry,
                kill_switch=deps.kill_switch,
                clock=deps.clock,
                options=deps.options,
            )
        except ExecutionError as e:
            state.graph_error = ReviewGraphError(
                error_code=e.error_code,
                message=str(e),
            )
            return {"graph_error": state.graph_error}
        except Exception as e:
            state.graph_error = ReviewGraphError(
                error_code="execution.graph.unexpected",
                message=str(e),
            )
            return {"graph_error": state.graph_error}
        state.result = result
        return {"result": result}

    async def finalize_execution(state: ExecutionGraphState) -> dict[str, Any]:
        if state.graph_error is not None:
            return {"graph_error": state.graph_error}
        if state.result is None:
            state.graph_error = ReviewGraphError(
                error_code="execution.graph.missing_result",
                message="result is None at finalize_execution",
            )
            return {"graph_error": state.graph_error}
        try:
            state.result.verify_integrity()
            state.result.verify_against_review(state.request, state.review_result)
        except Exception as e:
            state.graph_error = ReviewGraphError(
                error_code="execution.graph.finalize_failed",
                message=str(e),
            )
            return {"graph_error": state.graph_error}
        return {"result": state.result}

    def _should_continue(state: ExecutionGraphState) -> str:
        if state.graph_error is not None:
            return END
        return "continue"

    graph = StateGraph(ExecutionGraphState)
    graph.add_node("verify_review", verify_review)
    graph.add_node("authorize", authorize)
    graph.add_node("resolve_approval", resolve_approval)
    graph.add_node("reserve_idempotency", reserve_idempotency)
    graph.add_node("execute_actions", execute_actions)
    graph.add_node("finalize_execution", finalize_execution)

    graph.set_entry_point("verify_review")
    graph.add_conditional_edges(
        "verify_review",
        _should_continue,
        {"continue": "authorize", END: END},
    )
    graph.add_conditional_edges(
        "authorize",
        _should_continue,
        {"continue": "resolve_approval", END: END},
    )
    graph.add_conditional_edges(
        "resolve_approval",
        _should_continue,
        {"continue": "reserve_idempotency", END: END},
    )
    graph.add_conditional_edges(
        "reserve_idempotency",
        _should_continue,
        {"continue": "execute_actions", END: END},
    )
    graph.add_conditional_edges(
        "execute_actions",
        _should_continue,
        {"continue": "finalize_execution", END: END},
    )
    graph.add_edge("finalize_execution", END)

    return graph.compile()


__all__ = [
    "ExecutionGraphState",
    "RuntimeDependencies",
    "build_execution_graph",
]
