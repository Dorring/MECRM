"""Phase 4 RunStore idempotency tests.

Covers:

* Fresh run returns a lease with ``cached_result=None``.
* Same run_id + same plan_hash after completion returns a **deep copy**.
* Same run_id + different plan_hash → :class:`RunPlanConflictError`.
* Concurrent duplicate run → :class:`RunAlreadyInProgressError`.
* ``complete()`` stores a deep copy so callers cannot mutate the
  store's internal state.
* ``defensive_copy_result`` returns an independent object graph.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from multi_agent.contracts import ExecutionUsage
from multi_agent.execution import (
    ExecutionTraceEvent,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TaskExecutionRecord,
)
from multi_agent.execution_errors import (
    RunAlreadyInProgressError,
    RunPlanConflictError,
)
from multi_agent.run_store import (
    InMemoryRunStore,
    RunLease,
    defensive_copy_result,
)
from multi_agent.state import MergedState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_result(
    *,
    run_id: str = "run-001",
    plan_hash: str = "a" * 64,
    registry_version: str = "reg-v-001",
    status: SupervisorRunStatus = SupervisorRunStatus.COMPLETED,
    task_status: str = "completed",
) -> SupervisorRunResult:
    """Build a minimal :class:`SupervisorRunResult` for store tests."""
    task_record = TaskExecutionRecord(
        task_id="task-001",
        agent_id="agent_001",
        status=task_status,  # type: ignore[arg-type]
        attempts=[
            TaskAttemptRecord(
                task_id="task-001",
                agent_id="agent_001",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="completed",
                duration_ms=10,
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
        plan_hash=plan_hash,
        registry_version=registry_version,
        status=status,
        task_records=[task_record],
        merged_state=MergedState(),
        usage=ExecutionUsage(),
        trace=[trace_event],
        started_at=_FIXED_TS,
        completed_at=_FIXED_TS,
        duration_ms=10,
    )


# ---------------------------------------------------------------------------
# Fresh run
# ---------------------------------------------------------------------------


class TestFreshRun:
    @pytest.mark.asyncio
    async def test_begin_returns_fresh_lease_for_new_run(self):
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        assert isinstance(lease, RunLease)
        assert lease.run_id == "run-001"
        assert lease.plan_hash == "a" * 64
        assert lease.cached_result is None
        assert not lease.is_cached

    @pytest.mark.asyncio
    async def test_begin_marks_run_in_progress(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        assert store.is_in_progress("run-001")
        assert not store.is_completed("run-001")


# ---------------------------------------------------------------------------
# Cached result
# ---------------------------------------------------------------------------


class TestCachedResult:
    @pytest.mark.asyncio
    async def test_completed_run_returns_cached_result(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(result)

        lease = await store.begin("run-001", "a" * 64)
        assert lease.is_cached
        assert lease.cached_result is not None
        assert lease.cached_result.run_id == "run-001"
        assert lease.cached_result.plan_hash == "a" * 64

    @pytest.mark.asyncio
    async def test_cached_result_is_defensive_copy(self):
        """Mutating the original result after ``complete()`` must not
        affect what a later ``begin()`` returns."""
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(result)

        # Mutate the original result's task_records.
        result.task_records[0].__dict__["status"] = "tampered"

        lease = await store.begin("run-001", "a" * 64)
        assert lease.cached_result is not None
        assert lease.cached_result.task_records[0].status == "completed"

    @pytest.mark.asyncio
    async def test_cached_result_independent_from_returned_lease(self):
        """Mutating the lease's cached_result must not affect the
        store's internal copy."""
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        await store.complete(_make_result())

        lease1 = await store.begin("run-001", "a" * 64)
        assert lease1.cached_result is not None
        lease1.cached_result.task_records[0].__dict__["status"] = "tampered"

        lease2 = await store.begin("run-001", "a" * 64)
        assert lease2.cached_result is not None
        assert lease2.cached_result.task_records[0].status == "completed"

    @pytest.mark.asyncio
    async def test_complete_stores_deep_copy(self):
        """``complete()`` must store a deep copy so the caller cannot
        later mutate the store's internal state."""
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(result)

        # Mutate the original result's trace.
        result.trace[0].__dict__["event_type"] = "tampered"

        lease = await store.begin("run-001", "a" * 64)
        assert lease.cached_result is not None
        assert lease.cached_result.trace[0].event_type == "run_started"


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    @pytest.mark.asyncio
    async def test_same_run_different_plan_rejected(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        await store.complete(_make_result(plan_hash="a" * 64))

        with pytest.raises(RunPlanConflictError):
            await store.begin("run-001", "b" * 64)

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_run_rejected(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)

        with pytest.raises(RunAlreadyInProgressError):
            await store.begin("run-001", "a" * 64)

    @pytest.mark.asyncio
    async def test_complete_without_begin_raises(self):
        store = InMemoryRunStore()
        with pytest.raises(RunAlreadyInProgressError):
            await store.complete(_make_result())


# ---------------------------------------------------------------------------
# Idempotent re-complete
# ---------------------------------------------------------------------------


class TestIdempotentReComplete:
    @pytest.mark.asyncio
    async def test_re_complete_same_result_silently_ignored(self):
        """Calling ``complete()`` twice with the same plan_hash is
        idempotent — the second call is silently ignored."""
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(result)

        # Second complete with the same plan_hash should not raise.
        await store.complete(result)

        lease = await store.begin("run-001", "a" * 64)
        assert lease.cached_result is not None
        assert lease.cached_result.plan_hash == "a" * 64

    @pytest.mark.asyncio
    async def test_re_complete_different_plan_rejected(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        await store.complete(_make_result(plan_hash="a" * 64))

        with pytest.raises(RunPlanConflictError):
            await store.complete(_make_result(plan_hash="b" * 64))


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


class TestIntrospection:
    @pytest.mark.asyncio
    async def test_has_run(self):
        store = InMemoryRunStore()
        assert not store.has_run("run-001")
        await store.begin("run-001", "a" * 64)
        assert store.has_run("run-001")

    @pytest.mark.asyncio
    async def test_is_in_progress(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        assert store.is_in_progress("run-001")
        await store.complete(_make_result())
        assert not store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_is_completed(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        assert not store.is_completed("run-001")
        await store.complete(_make_result())
        assert store.is_completed("run-001")

    @pytest.mark.asyncio
    async def test_clear(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)
        await store.complete(_make_result())
        assert store.has_run("run-001")
        store.clear()
        assert not store.has_run("run-001")


# ---------------------------------------------------------------------------
# defensive_copy_result helper
# ---------------------------------------------------------------------------


class TestDefensiveCopyResult:
    def test_returns_independent_copy(self):
        result = _make_result()
        copied = defensive_copy_result(result)
        assert copied is not result
        assert copied.run_id == result.run_id
        assert copied.plan_hash == result.plan_hash

    def test_mutation_does_not_propagate(self):
        result = _make_result()
        copied = defensive_copy_result(result)
        result.task_records[0].__dict__["status"] = "tampered"
        assert copied.task_records[0].status == "completed"

    def test_decimal_and_datetime_preserved(self):
        """``model_dump(mode="python")`` preserves ``Decimal`` and
        timezone-aware ``datetime`` types."""
        result = _make_result()
        result.usage.cost_usd = Decimal("1.23")
        copied = defensive_copy_result(result)
        assert copied.usage.cost_usd == Decimal("1.23")
        assert isinstance(copied.started_at, datetime)
        assert copied.started_at.tzinfo is not None
