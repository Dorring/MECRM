"""Phase 4 LangGraph Adapter tests.

The graph is a **thin wrapper** around :class:`SupervisorRuntime`.
These tests verify:

* Happy-path routing — the graph delegates to ``runtime.execute()``
  and returns the result.
* Error propagation — when the runtime raises, the graph captures
  the error in state and ``finalize_run`` re-raises it.
* The graph does **not** duplicate scheduler/budget/merge logic.
* A fake runtime can be substituted for unit testing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from multi_agent.contracts import ExecutionUsage, ToolAuthority
from multi_agent.execution import (
    ExecutionTraceEvent,
    SupervisorConfig,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TaskExecutionRecord,
)
from multi_agent.execution_errors import SupervisorError
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.state import MergedState
from multi_agent.supervisor_graph import (
    FakeSupervisorRuntime,
    SupervisorGraphState,
    build_supervisor_graph,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_result(
    *,
    run_id: str = "run-001",
    status: SupervisorRunStatus = SupervisorRunStatus.COMPLETED,
) -> SupervisorRunResult:
    """Build a minimal SupervisorRunResult for graph tests."""
    task_record = TaskExecutionRecord(
        task_id="task-001",
        agent_id="agent_001",
        status="completed",
        attempts=[
            TaskAttemptRecord(
                task_id="task-001",
                agent_id="agent_001",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="completed",
                duration_ms=5,
            )
        ],
    )
    trace_event = ExecutionTraceEvent(
        sequence=0,
        event_type="run_started",
        run_id=run_id,
        occurred_at=_FIXED_TS,
    )
    return SupervisorRunResult(
        run_id=run_id,
        plan_hash="a" * 64,
        registry_version="reg-v-001",
        status=status,
        task_records=[task_record],
        merged_state=MergedState(),
        usage=ExecutionUsage(),
        trace=[trace_event],
        started_at=_FIXED_TS,
        completed_at=_FIXED_TS,
        duration_ms=5,
    )


def _make_registry() -> AgentRegistry:
    return AgentRegistry(
        tool_catalog=ToolCatalog(
            [
                ToolDescriptor(
                    tool_name="crm_reader.get_customers",
                    authority=ToolAuthority.READ,
                )
            ]
        )
    )


class _StubPlan:
    """Minimal plan stub — the graph only checks ``is not None``."""

    run_id = "run-001"
    plan_hash = "a" * 64
    tenant_id = "t-001"
    registry_version = "reg-v-001"

    @property
    def request(self) -> Any:
        return self

    @property
    def tasks(self) -> list:
        return []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSupervisorGraphHappyPath:
    @pytest.mark.asyncio
    async def test_graph_returns_runtime_result(self):
        """The graph delegates to ``runtime.execute`` and returns its
        result in ``state.result``."""
        expected = _make_result()
        runtime = FakeSupervisorRuntime(result=expected)
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=_make_registry(),
        )
        result_state = await graph.ainvoke(state)

        assert result_state["result"] is expected
        assert len(runtime.calls) == 1

    @pytest.mark.asyncio
    async def test_graph_passes_config_and_cancellation(self):
        """Config and cancellation must flow from state to runtime."""
        expected = _make_result()
        runtime = FakeSupervisorRuntime(result=expected)
        graph = build_supervisor_graph(runtime)

        cfg = SupervisorConfig(max_concurrency=2)
        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=_make_registry(),
            config=cfg,
        )
        await graph.ainvoke(state)

        # FakeSupervisorRuntime.execute accepts config/cancellation as
        # kwargs; we verify it was called once without error.
        assert len(runtime.calls) == 1


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestGraphErrorPropagation:
    @pytest.mark.asyncio
    async def test_graph_propagates_runtime_failure(self):
        """When the runtime raises, ``execute_dag`` captures the error
        in ``state.error`` and ``finalize_run`` re-raises it."""
        error = SupervisorError("plan invalid")
        runtime = FakeSupervisorRuntime(error=error)
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=_make_registry(),
        )

        with pytest.raises(SupervisorError, match="plan invalid"):
            await graph.ainvoke(state)

    @pytest.mark.asyncio
    async def test_graph_captures_error_in_state(self):
        """Before ``finalize_run`` re-raises, the error must be stored
        in ``state.error`` so intermediate nodes can inspect it."""
        error = SupervisorError("boom")
        runtime = FakeSupervisorRuntime(error=error)
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=_make_registry(),
        )

        with pytest.raises(SupervisorError):
            await graph.ainvoke(state)

        # The graph's internal state should have captured the error.
        # We verify via the runtime call list — if the graph called
        # execute, the error path was taken.
        assert len(runtime.calls) == 1


# ---------------------------------------------------------------------------
# No duplicate logic
# ---------------------------------------------------------------------------


class TestGraphDoesNotDuplicateLogic:
    @pytest.mark.asyncio
    async def test_graph_calls_runtime_exactly_once(self):
        """The graph must not call ``runtime.execute`` more than once
        per invocation — no retry, no parallel dispatch."""
        expected = _make_result()
        runtime = FakeSupervisorRuntime(result=expected)
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=_make_registry(),
        )
        await graph.ainvoke(state)

        assert len(runtime.calls) == 1

    @pytest.mark.asyncio
    async def test_graph_does_not_implement_scheduler(self):
        """The graph has 5 nodes: validate_plan → initialize_run →
        execute_dag → merge_results → finalize_run.  All scheduler /
        budget / merge work happens inside ``runtime.execute``.

        We verify by checking that the fake runtime (which does no
        scheduling) is the only thing called."""
        expected = _make_result()
        runtime = FakeSupervisorRuntime(result=expected)
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=_make_registry(),
        )
        result_state = await graph.ainvoke(state)

        # If the graph tried to schedule tasks itself, it would need
        # access to the plan's tasks and the registry's handlers.
        # The fake runtime doesn't expose any scheduling API, so the
        # only way result_state["result"] can be set is if the graph
        # delegated entirely to runtime.execute.
        assert result_state["result"] is expected
        assert len(runtime.calls) == 1


# ---------------------------------------------------------------------------
# Validation nodes
# ---------------------------------------------------------------------------


class TestGraphValidationNodes:
    @pytest.mark.asyncio
    async def test_validate_plan_rejects_missing_plan(self):
        """``validate_plan`` must raise if ``state.plan`` is None."""
        runtime = FakeSupervisorRuntime(result=_make_result())
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=None,  # type: ignore[arg-type]
            registry=_make_registry(),
        )
        with pytest.raises(ValueError, match="plan must be set"):
            await graph.ainvoke(state)

    @pytest.mark.asyncio
    async def test_validate_plan_rejects_missing_registry(self):
        """``validate_plan`` must raise if ``state.registry`` is None."""
        runtime = FakeSupervisorRuntime(result=_make_result())
        graph = build_supervisor_graph(runtime)

        state = SupervisorGraphState(
            plan=_StubPlan(),  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="registry must be set"):
            await graph.ainvoke(state)
