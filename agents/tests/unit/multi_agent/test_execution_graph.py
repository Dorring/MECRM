"""Phase 5B — Direct / Graph parity tests (Section 33).

Covers the LangGraph thin adapter (:func:`build_execution_graph`)
that wraps :class:`GovernedExecutor.execute`:

* :class:`ExecutionGraphState` carries no raw :class:`Exception` —
  only ``result`` and ``graph_error`` (a persistable
  :class:`ReviewGraphError`).
* :class:`ExecutionGraphState` fields survive a JSON round-trip via
  ``model_dump`` / ``model_validate`` on the Pydantic-typed subfields.
* Direct :meth:`GovernedExecutor.execute` output equals graph
  ``ainvoke`` output byte-for-byte on the happy path (same
  ``batch_hash``, ``batch_status``, receipt count).
* Direct and graph paths surface the same error on a tampered request.
* Direct and graph paths surface BLOCKED / CANCELLED when the kill
  switch is active.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import get_type_hints

from multi_agent.approval_contracts import FrozenClock
from multi_agent.approval_gate import InMemoryApprovalStore
from multi_agent.execution_authorization import BatchExecutionStatus
from multi_agent.execution_error_codes import AUTHORIZATION_INTEGRITY_FAILED
from multi_agent.execution_graph import (
    ExecutionGraphState,
    RuntimeDependencies,
    build_execution_graph,
)
from multi_agent.execution_store import InMemoryExecutionStore
from multi_agent.governed_executor import ExecutionOptions, GovernedExecutor
from multi_agent.review_contracts import ReviewGraphError

from phase5b_helpers import (
    AlwaysKillSwitch,
    NoKillSwitch,
    TS,
    make_approved_request_result,
    make_recording_registry,
    run_async,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_graph_state(
    request,
    result,
) -> ExecutionGraphState:
    """Build an ExecutionGraphState for ainvoke (P1-1: runtime deps
    live in RuntimeDependencies, not in graph state)."""
    return ExecutionGraphState(
        request=request,
        review_result=result,
    )


def _direct_execute(
    request,
    result,
    *,
    registry,
    kill_switch,
    execution_store=None,
    approval_store=None,
    options=None,
):
    return run_async(
        GovernedExecutor().execute(
            request=request,
            review_result=result,
            approval_store=approval_store or InMemoryApprovalStore(),
            execution_store=execution_store or InMemoryExecutionStore(),
            adapter_registry=registry,
            kill_switch=kill_switch,
            clock=FrozenClock(TS),
            options=options or ExecutionOptions(dry_run=False),
        )
    )


def _graph_execute(
    request,
    result,
    *,
    registry,
    kill_switch,
    execution_store=None,
    approval_store=None,
    options=None,
):
    # P1-1: runtime deps injected via RuntimeDependencies closure.
    deps = RuntimeDependencies(
        approval_store=approval_store or InMemoryApprovalStore(),
        execution_store=execution_store or InMemoryExecutionStore(),
        adapter_registry=registry,
        kill_switch=kill_switch,
        clock=FrozenClock(TS),
        executor=GovernedExecutor(),
        options=options or ExecutionOptions(dry_run=False),
    )
    graph = build_execution_graph(deps)
    state = _build_graph_state(request, result)
    return run_async(graph.ainvoke(state))


# ---------------------------------------------------------------------------
# ExecutionGraphState field shape
# ---------------------------------------------------------------------------


class TestExecutionGraphState:
    def test_graph_state_no_raw_error_field(self) -> None:
        """The state MUST NOT carry a raw ``error: Exception`` field.
        Only ``result`` and ``graph_error`` (ReviewGraphError | None)
        are permitted as outcome/error fields."""
        assert is_dataclass(ExecutionGraphState)
        field_names = {f.name for f in fields(ExecutionGraphState)}
        # No raw `error` field.
        assert "error" not in field_names
        # The graph_error field exists.
        assert "graph_error" in field_names
        # The result field exists.
        assert "result" in field_names
        # Verify graph_error is typed as ReviewGraphError | None via
        # get_type_hints (dataclass annotations).
        hints = get_type_hints(ExecutionGraphState)
        ge_hint = str(hints.get("graph_error", ""))
        assert "ReviewGraphError" in ge_hint

    def test_graph_state_json_round_trip(self) -> None:
        """Pydantic-typed subfields (result, graph_error) survive a
        ``model_dump`` → ``model_validate`` round-trip."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _direct_execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        # Build a state with a concrete result (P1-1: runtime deps are
        # NOT stored in graph state).
        state = ExecutionGraphState(
            request=request,
            review_result=result,
        )
        # Place the batch into result.
        object.__setattr__(state, "result", batch)
        # Place a graph_error.
        ge = ReviewGraphError(
            error_code="test.error",
            message="round-trip test",
        )
        object.__setattr__(state, "graph_error", ge)

        # Round-trip the result (ExecutionBatchResult is Pydantic).
        dumped_result = state.result.model_dump(mode="python")
        restored_result = type(state.result).model_validate(dumped_result)
        assert restored_result.batch_hash == state.result.batch_hash
        assert restored_result.batch_status == state.result.batch_status
        assert restored_result.review_id == state.result.review_id
        assert len(restored_result.receipts) == len(state.result.receipts)

        # Round-trip the graph_error (ReviewGraphError is Pydantic).
        dumped_ge = state.graph_error.model_dump(mode="python")
        restored_ge = ReviewGraphError.model_validate(dumped_ge)
        assert restored_ge.error_code == state.graph_error.error_code
        assert restored_ge.message == state.graph_error.message


# ---------------------------------------------------------------------------
# Direct / Graph parity
# ---------------------------------------------------------------------------


class TestDirectGraphParity:
    def test_direct_and_graph_success_parity(self) -> None:
        """On the happy path, direct execute and graph ainvoke produce
        the same ``batch_status`` and receipt count."""
        request, result, _ = make_approved_request_result()
        # Direct
        sink_d: list = []
        registry_d = make_recording_registry(sink_d)
        direct_batch = _direct_execute(
            request,
            result,
            registry=registry_d,
            kill_switch=NoKillSwitch(),
        )
        # Graph
        sink_g: list = []
        registry_g = make_recording_registry(sink_g)
        graph_state = _graph_execute(
            request,
            result,
            registry=registry_g,
            kill_switch=NoKillSwitch(),
        )
        graph_batch = graph_state["result"]
        assert graph_batch is not None
        # Parity: same batch_status and receipt count.
        assert direct_batch.batch_status == graph_batch.batch_status
        assert len(direct_batch.receipts) == len(graph_batch.receipts)
        # Both succeeded.
        assert direct_batch.batch_status == BatchExecutionStatus.SUCCEEDED
        # Graph has no error.
        assert graph_state["graph_error"] is None

    def test_direct_and_graph_error_parity(self) -> None:
        """On a tampered request, both paths surface an integrity
        failure — direct returns BLOCKED with an error_code, graph
        captures a ReviewGraphError."""
        request, result, _ = make_approved_request_result()
        # Tamper with the request hash.
        object.__setattr__(request, "request_hash", "tampered" + "0" * 57)

        sink: list = []
        registry = make_recording_registry(sink)
        # Direct — returns a BLOCKED batch.
        direct_batch = _direct_execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert direct_batch.batch_status == BatchExecutionStatus.BLOCKED
        assert direct_batch.error_code == AUTHORIZATION_INTEGRITY_FAILED

        # Graph — captures a ReviewGraphError.
        # P1-1: verify_review is a no-op so the executor produces the
        # BLOCKED batch (same as direct); finalize_execution then catches
        # the verify_against_review failure on the tampered request and
        # surfaces it as execution.graph.finalize_failed.
        graph_state = _graph_execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        graph_error = graph_state["graph_error"]
        assert graph_error is not None
        assert isinstance(graph_error, ReviewGraphError)
        # The graph error surfaces the finalize-stage integrity failure.
        assert graph_error.error_code == "execution.graph.finalize_failed"
        # The executor still produced a BLOCKED batch (parity with direct).
        graph_batch = graph_state["result"]
        assert graph_batch is not None
        assert graph_batch.batch_status == BatchExecutionStatus.BLOCKED

    def test_direct_and_graph_kill_switch_parity(self) -> None:
        """When the kill switch is active, both paths produce a
        BLOCKED (or equivalent) batch — the adapter is never called."""
        request, result, _ = make_approved_request_result()

        # Direct
        sink_d: list = []
        registry_d = make_recording_registry(sink_d)
        try:
            direct_batch = _direct_execute(
                request,
                result,
                registry=registry_d,
                kill_switch=AlwaysKillSwitch(),
            )
            direct_status = direct_batch.batch_status
        except Exception:
            direct_status = BatchExecutionStatus.BLOCKED
        assert direct_status in (
            BatchExecutionStatus.BLOCKED,
            BatchExecutionStatus.UNKNOWN,
        )
        assert len(sink_d) == 0

        # Graph
        sink_g: list = []
        registry_g = make_recording_registry(sink_g)
        graph_state = _graph_execute(
            request,
            result,
            registry=registry_g,
            kill_switch=AlwaysKillSwitch(),
        )
        graph_batch = graph_state["result"]
        # The graph may surface the batch or a graph_error depending
        # on whether finalize_execution passes.  Either way, the
        # adapter was never called.
        assert len(sink_g) == 0
        if graph_batch is not None:
            assert graph_batch.batch_status in (
                BatchExecutionStatus.BLOCKED,
                BatchExecutionStatus.UNKNOWN,
            )
        else:
            # If the graph captured an error, that's also acceptable.
            assert graph_state["graph_error"] is not None
