"""Phase 5B — LangGraph thin adapter for the Governed Executor.

A 6-node LangGraph that wraps :class:`GovernedExecutor.execute` so
callers that already use LangGraph can compose the executor
uniformly.  The graph does NOT re-implement any step of the 18-step
pipeline — every node delegates to :class:`GovernedExecutor` public
methods or simply surfaces errors as persistable
:class:`ReviewGraphError` records.

Nodes (Phase 5B Section 21):

* ``verify_review`` — request/result integrity + binding.
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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, StateGraph

from multi_agent.action_adapter import ActionAdapterRegistry
from multi_agent.approval_contracts import Clock, FrozenClock
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


_DEFAULT_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


@dataclass
class ExecutionGraphState:
    """Mutable state passed between graph nodes.

    Carries the inputs and the final :class:`ExecutionBatchResult`.
    ``graph_error`` carries a persistable :class:`ReviewGraphError`
    when a node fails — no raw :class:`Exception` enters the State
    (Phase 5B Section 21, mirroring Phase 5A R2.1 P1-1).
    """

    request: ReviewRequest
    review_result: ReviewBatchResult
    approval_store: ApprovalStore
    execution_store: ExecutionStore
    adapter_registry: ActionAdapterRegistry
    kill_switch: Any = None
    clock: Clock = field(default_factory=lambda: _DEFAULT_CLOCK)
    options: ExecutionOptions = field(default_factory=ExecutionOptions)
    executor: GovernedExecutor | None = None
    result: ExecutionBatchResult | None = None
    graph_error: ReviewGraphError | None = None


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_execution_graph():
    """Build a LangGraph that wraps :class:`GovernedExecutor.execute`.

    The returned graph is compiled but not started.  Callers invoke
    it via ``graph.ainvoke(initial_state)``.

    The graph is **not** registered in any application startup —
    Phase 5C will wire it into the orchestrator.
    """

    async def verify_review(state: ExecutionGraphState) -> dict[str, Any]:
        if state.graph_error is not None:
            return {"graph_error": state.graph_error}
        try:
            state.request.verify_integrity()
            state.review_result.verify_integrity()
            state.review_result.verify_against_request(state.request)
        except Exception as e:
            state.graph_error = ReviewGraphError(
                error_code="execution.graph.review_verify",
                message=str(e),
            )
            return {"graph_error": state.graph_error}
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
        executor = state.executor or GovernedExecutor()
        try:
            result = await executor.execute(
                request=state.request,
                review_result=state.review_result,
                approval_store=state.approval_store,
                execution_store=state.execution_store,
                adapter_registry=state.adapter_registry,
                kill_switch=state.kill_switch,
                clock=state.clock,
                options=state.options,
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
                message="ExecutionGraphState.result is None at finalize_execution",
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
    "build_execution_graph",
]
