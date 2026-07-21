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
    SupervisorError,
)
from multi_agent.run_store import (
    InMemoryRunStore,
    RunIdentity,
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
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(lease, result)

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
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(lease, result)

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
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result())

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
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(lease, result)

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
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result(plan_hash="a" * 64))

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
        lease = RunLease(run_id="run-001", plan_hash="a" * 64)
        with pytest.raises(RunAlreadyInProgressError):
            await store.complete(lease, _make_result())


# ---------------------------------------------------------------------------
# Idempotent re-complete
# ---------------------------------------------------------------------------


class TestIdempotentReComplete:
    @pytest.mark.asyncio
    async def test_re_complete_same_result_silently_ignored(self):
        """Calling ``complete()`` twice with the same plan_hash is
        idempotent — the second call is silently ignored."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result()
        await store.complete(lease, result)

        # Second complete with the same plan_hash should not raise.
        await store.complete(lease, result)

        lease = await store.begin("run-001", "a" * 64)
        assert lease.cached_result is not None
        assert lease.cached_result.plan_hash == "a" * 64

    @pytest.mark.asyncio
    async def test_re_complete_different_plan_rejected(self):
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result(plan_hash="a" * 64))

        with pytest.raises(RunPlanConflictError):
            conflicting_lease = RunLease(run_id="run-001", plan_hash="b" * 64)
            await store.complete(conflicting_lease, _make_result(plan_hash="b" * 64))


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
        lease = await store.begin("run-001", "a" * 64)
        assert store.is_in_progress("run-001")
        await store.complete(lease, _make_result())
        assert not store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_is_completed(self):
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        assert not store.is_completed("run-001")
        await store.complete(lease, _make_result())
        assert store.is_completed("run-001")

    @pytest.mark.asyncio
    async def test_clear(self):
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result())
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


# ---------------------------------------------------------------------------
# R3 P0-3: Frozen RunLease + three-part identity
# ---------------------------------------------------------------------------


class TestRunLeaseFrozen:
    """R3 P0-3: RunLease is a frozen StrictContract — its identity
    fields cannot be mutated after construction."""

    def test_run_lease_is_frozen(self):
        """Assigning to ``lease.plan_hash`` after construction must
        raise ``ValidationError`` — the three-part identity
        (run_id + plan_hash + lease_id) is immutable for the lifetime
        of the lease."""
        from pydantic import ValidationError

        lease = RunLease(run_id="run-001", plan_hash="a" * 64)
        with pytest.raises(ValidationError):
            lease.plan_hash = "b" * 64  # type: ignore[misc]

    def test_run_lease_rejects_extra_fields(self):
        """``extra='forbid'`` prevents smuggling in extra fields."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RunLease(  # type: ignore[call-arg]
                run_id="run-001",
                plan_hash="a" * 64,
                extra_field="sneaky",  # type: ignore[call-arg]
            )

    def test_run_lease_has_lease_id(self):
        """Each RunLease carries a non-empty ``lease_id`` generated by
        ``secrets.token_hex``."""
        lease = RunLease(run_id="run-001", plan_hash="a" * 64)
        assert isinstance(lease.lease_id, str)
        assert len(lease.lease_id) > 0

    def test_two_leases_have_different_lease_ids(self):
        """Two leases for the same (run_id, plan_hash) must have
        different ``lease_id`` values — the id is unpredictable."""
        l1 = RunLease(run_id="run-001", plan_hash="a" * 64)
        l2 = RunLease(run_id="run-001", plan_hash="a" * 64)
        assert l1.lease_id != l2.lease_id


class TestCompleteThreePartIdentity:
    """R3 P0-3: ``complete()`` verifies the three-part lease identity
    (run_id + plan_hash + lease_id) against the in-progress entry."""

    @pytest.mark.asyncio
    async def test_complete_rejects_entry_plan_hash_mismatch(self):
        """If the lease's ``plan_hash`` does not match the entry's
        ``plan_hash``, ``complete()`` must reject — even if the
        ``lease_id`` matches.

        Reproduction for the R3 P0-3 bug: previously only
        ``lease_id`` was checked, so a caller could mutate
        ``lease.plan_hash`` after ``begin()`` and trick ``complete()``
        into accepting a result with a different plan_hash.  The
        frozen contract prevents the mutation; this test verifies
        the entry-side check is also in place by constructing a lease
        with the right ``lease_id`` but a wrong ``plan_hash`` via
        ``object.__setattr__`` (simulating a tampered or stale lease).
        """
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)

        # Tamper with the lease's plan_hash at the Python level
        # (bypassing the frozen contract) to simulate a stale or
        # corrupted lease that has the right lease_id but a wrong
        # plan_hash.
        object.__setattr__(lease, "plan_hash", "b" * 64)

        # Build a result whose plan_hash matches the *tampered* lease.
        result = _make_result(run_id="run-001", plan_hash="b" * 64)

        with pytest.raises(SupervisorError, match="identity"):
            await store.complete(lease, result)

        # The run must still be in-progress (complete was rejected).
        assert store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_complete_rejects_entry_run_id_mismatch(self):
        """If the lease's ``run_id`` does not match the entry's
        ``run_id``, ``complete()`` must reject.  This is a defensive
        check — ``begin()`` should have stored the entry under the
        correct run_id, but a stale lease from a different run must
        not be able to complete this entry."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)

        # Tamper with the lease's run_id to simulate a stale lease
        # from a different run that happens to guess the lease_id.
        object.__setattr__(lease, "run_id", "run-999")

        result = _make_result(run_id="run-999", plan_hash="a" * 64)

        with pytest.raises((SupervisorError, RunAlreadyInProgressError)):
            await store.complete(lease, result)

        # The original run must still be in-progress.
        assert store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_complete_accepts_matching_three_part_identity(self):
        """A lease with matching (run_id, plan_hash, lease_id)
        completes successfully."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result(run_id="run-001", plan_hash="a" * 64)
        # Must not raise.
        await store.complete(lease, result)
        assert store.is_completed("run-001")

    @pytest.mark.asyncio
    async def test_stale_lease_cannot_complete_different_plan(self):
        """R3 P0-3 reverse test: a stale lease (with a stale
        ``lease_id``) cannot complete a newer run even if the
        ``plan_hash`` matches — the ``lease_id`` must also match."""
        store = InMemoryRunStore()

        # First lease — aborted.
        lease1 = await store.begin("run-001", "a" * 64)
        await store.abort(lease1, error_code="first_failure")

        # New lease for the same run.
        lease2 = await store.begin("run-001", "a" * 64)
        assert lease1.lease_id != lease2.lease_id

        # Stale lease1 tries to complete — must be rejected because
        # its lease_id does not match the new entry's lease_id.
        result = _make_result(run_id="run-001", plan_hash="a" * 64)
        with pytest.raises(SupervisorError, match="identity"):
            await store.complete(lease1, result)

        # The new run must still be in-progress.
        assert store.is_in_progress("run-001")

        # The fresh lease can complete.
        await store.complete(lease2, result)
        assert store.is_completed("run-001")


class TestAbortThreePartIdentity:
    """R3 P0-3: ``abort()`` verifies the three-part lease identity."""

    @pytest.mark.asyncio
    async def test_abort_rejects_entry_plan_hash_mismatch(self):
        """``abort()`` with a lease whose ``plan_hash`` does not match
        the entry's ``plan_hash`` must be rejected."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)

        # Tamper with the lease's plan_hash.
        object.__setattr__(lease, "plan_hash", "b" * 64)

        with pytest.raises(SupervisorError, match="identity"):
            await store.abort(lease, error_code="stale")

        # The run must still be in-progress (abort was rejected).
        assert store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_abort_accepts_matching_three_part_identity(self):
        """A lease with matching (run_id, plan_hash, lease_id)
        aborts successfully."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        await store.abort(lease, error_code="ok")
        assert not store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_stale_lease_cannot_abort_newer_lease(self):
        """After abort + re-begin, the old lease cannot abort the new
        entry — the ``lease_id`` does not match."""
        store = InMemoryRunStore()
        lease1 = await store.begin("run-001", "a" * 64)
        await store.abort(lease1, error_code="first")

        await store.begin("run-001", "a" * 64)

        with pytest.raises(SupervisorError, match="identity"):
            await store.abort(lease1, error_code="stale_callback")

        # The new run must still be in-progress.
        assert store.is_in_progress("run-001")


# ---------------------------------------------------------------------------
# R3 P1-1: lookup_run_identity read-only probe
# ---------------------------------------------------------------------------


class TestLookupRunIdentity:
    """R3 P1-1: ``lookup_run_identity`` is a read-only probe that
    determines cache/conflict/in-progress status in one call."""

    @pytest.mark.asyncio
    async def test_unknown_run_returns_none(self):
        store = InMemoryRunStore()
        identity = await store.lookup_run_identity("run-001", "a" * 64)
        assert identity is None

    @pytest.mark.asyncio
    async def test_completed_same_hash_returns_cached_identity(self):
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result(run_id="run-001", plan_hash="a" * 64))

        identity = await store.lookup_run_identity("run-001", "a" * 64)
        assert identity is not None
        assert identity.status == "completed"
        assert identity.plan_hash_matches is True
        assert identity.is_completed is True
        assert identity.cached_result is not None
        assert identity.cached_result.run_id == "run-001"
        assert identity.cached_result.plan_hash == "a" * 64

    @pytest.mark.asyncio
    async def test_completed_different_hash_reports_conflict(self):
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result(run_id="run-001", plan_hash="a" * 64))

        identity = await store.lookup_run_identity("run-001", "b" * 64)
        assert identity is not None
        assert identity.status == "completed"
        assert identity.plan_hash_matches is False
        assert identity.is_completed is False
        # No cached result for a conflicting plan_hash.
        assert identity.cached_result is None

    @pytest.mark.asyncio
    async def test_in_progress_same_hash_returns_in_progress_identity(self):
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)

        identity = await store.lookup_run_identity("run-001", "a" * 64)
        assert identity is not None
        assert identity.status == "in_progress"
        assert identity.plan_hash_matches is True
        assert identity.is_completed is False
        assert identity.cached_result is None

    @pytest.mark.asyncio
    async def test_in_progress_different_hash_returns_in_progress(self):
        """R4 P0-1: the store reports an in-progress run with a
        different plan_hash as ``status='in_progress'`` with
        ``plan_hash_matches=False``.  The Supervisor — not the store
        — decides to raise :class:`RunPlanConflictError` based on
        ``plan_hash_matches`` being ``False``; the store itself only
        reports the raw status."""
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)

        identity = await store.lookup_run_identity("run-001", "b" * 64)
        assert identity is not None
        assert identity.status == "in_progress"
        assert identity.plan_hash_matches is False
        assert identity.is_completed is False

    @pytest.mark.asyncio
    async def test_lookup_run_identity_is_readonly(self):
        """The probe must not mutate the store — calling it does not
        create an entry or change the in-progress state."""
        store = InMemoryRunStore()
        await store.begin("run-001", "a" * 64)

        await store.lookup_run_identity("run-002", "b" * 64)  # unknown run
        assert not store.has_run("run-002")

        # The in-progress run-001 is unaffected.
        assert store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_cached_result_from_identity_is_independent_copy(self):
        """The cached_result returned by ``lookup_run_identity`` is a
        deep copy — mutating it must not affect the store's internal
        copy."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        await store.complete(lease, _make_result(run_id="run-001", plan_hash="a" * 64))

        identity1 = await store.lookup_run_identity("run-001", "a" * 64)
        assert identity1 is not None
        assert identity1.cached_result is not None
        identity1.cached_result.task_records[0].__dict__["status"] = "tampered"

        identity2 = await store.lookup_run_identity("run-001", "a" * 64)
        assert identity2 is not None
        assert identity2.cached_result is not None
        assert identity2.cached_result.task_records[0].status == "completed"

    def test_run_identity_is_frozen(self):
        """``RunIdentity`` is a frozen contract — its fields cannot be
        mutated after construction."""
        from pydantic import ValidationError

        identity = RunIdentity(
            run_id="run-001",
            requested_plan_hash="a" * 64,
            stored_plan_hash="a" * 64,
            status="completed",
        )
        with pytest.raises(ValidationError):
            identity.status = "in_progress"  # type: ignore[misc]
