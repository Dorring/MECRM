"""Phase 4 Run Idempotency Store.

The :class:`RunStore` Protocol guards against duplicate Handler
invocations for the same ``run_id``.  Phase 4 only ships an in-memory
implementation — database persistence is a Phase 5 concern.

Contract
--------

``lookup_completed(run_id, plan_hash) -> SupervisorRunResult | None``
    R2 P0-1: Called *before* any registry pre-flight.  Returns a
    deep copy of the stored result when ``run_id`` is already
    completed with the same ``plan_hash``.  Returns ``None`` for
    unknown / in-progress / mismatched-plan runs.  This is the cache
    lookup path — a cached result must be returned even if the live
    Registry has since drifted from ``plan.registry_version``.

``begin(run_id, plan_hash) -> RunLease``
    Called by :class:`SupervisorRuntime` *after* all side-effect-free
    pre-flight checks (plan integrity, registry version, PlanValidator,
    handler resolution) have passed.

    * If ``run_id`` is unknown → mark it in-progress and return a
      :class:`RunLease` whose ``cached_result is None``.
    * If ``run_id`` is in-progress → raise
      :class:`RunAlreadyInProgressError`.
    * If ``run_id`` is completed with the same ``plan_hash`` → return
      a :class:`RunLease` whose ``cached_result`` is a **deep copy**
      of the stored result.  (Defensive; ``lookup_completed`` is the
      preferred cache path.)
    * If ``run_id`` is completed with a different ``plan_hash`` →
      raise :class:`RunPlanConflictError`.

``complete(lease, result) -> None``
    R2 P1-1: now takes the :class:`RunLease` returned by ``begin``.
    The store validates ``lease.run_id`` / ``lease.plan_hash`` /
    ``lease.lease_id`` against the in-progress entry and rejects a
    ``complete`` call whose lease identity does not match (stale
    callback after abort, etc.).

``abort(lease, *, error_code) -> None``
    R2 P1-1: now takes the :class:`RunLease` returned by ``begin``.
    Same identity check as ``complete``.  If the lease is stale (a
    newer lease has been issued for the same ``run_id``), the abort
    is rejected so an old callback cannot delete a newer run's lease.

The store is intentionally simple — no TTL, no eviction.  Phase 5
will add a Postgres-backed implementation with the same Protocol.
"""

from __future__ import annotations

import secrets
from typing import Protocol

from multi_agent.execution import SupervisorRunResult
from multi_agent.execution_errors import (
    RunAlreadyInProgressError,
    RunPlanConflictError,
    SupervisorError,
)


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


class RunLease:
    """Returned by :meth:`RunStore.begin`.

    A lease is either *fresh* (``cached_result is None`` — the caller
    must execute the plan and call :meth:`RunStore.complete`) or
    *cached* (``cached_result is not None`` — the caller must return
    the cached result without invoking any Handler).

    R2 P1-1: each lease carries an unpredictable ``lease_id`` so
    ``complete`` and ``abort`` can verify they are operating on the
    *same* lease that ``begin`` issued.  This prevents a stale
    callback (e.g. a cancelled coroutine that resumed after abort)
    from deleting or completing a newer lease for the same run_id.
    """

    __slots__ = ("run_id", "plan_hash", "lease_id", "cached_result")

    def __init__(
        self,
        *,
        run_id: str,
        plan_hash: str,
        lease_id: str | None = None,
        cached_result: SupervisorRunResult | None = None,
    ) -> None:
        self.run_id = run_id
        self.plan_hash = plan_hash
        self.lease_id = lease_id or _generate_lease_id()
        self.cached_result = cached_result

    @property
    def is_cached(self) -> bool:
        return self.cached_result is not None


def _generate_lease_id() -> str:
    """Generate an unpredictable lease identifier.

    Uses :func:`secrets.token_hex` so the id is unguessable by a stale
    coroutine — a leaked reference to an old RunLease cannot be used
    to abort or complete a newer lease for the same run_id.
    """
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RunStore(Protocol):
    """Idempotency boundary for :class:`SupervisorRuntime`."""

    async def lookup_completed(
        self,
        run_id: str,
        plan_hash: str,
    ) -> SupervisorRunResult | None:
        """R2 P0-1: cache lookup path.

        Returns a deep copy of the stored result when ``run_id`` is
        completed with the same ``plan_hash``; ``None`` otherwise.
        Must NOT raise for in-progress or mismatched-plan runs.
        """

    async def begin(
        self,
        run_id: str,
        plan_hash: str,
    ) -> RunLease: ...

    async def complete(
        self,
        lease: RunLease,
        result: SupervisorRunResult,
    ) -> None: ...

    async def abort(
        self,
        lease: RunLease,
        *,
        error_code: str,
    ) -> None: ...



# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class _RunEntry:
    """Internal storage for one run.

    Either ``in_progress=True`` (no result yet) or ``result`` is set.
    Both fields are never set simultaneously.

    R2 P1-1: ``lease_id`` binds an in-progress entry to a specific
    :class:`RunLease`.  ``complete`` and ``abort`` must verify they
    are operating on the same lease; a stale callback whose lease_id
    does not match is rejected so it cannot corrupt a newer run.
    """

    __slots__ = ("run_id", "plan_hash", "lease_id", "in_progress", "result")

    def __init__(
        self,
        *,
        run_id: str,
        plan_hash: str,
        lease_id: str,
        in_progress: bool,
        result: SupervisorRunResult | None,
    ) -> None:
        self.run_id = run_id
        self.plan_hash = plan_hash
        self.lease_id = lease_id
        self.in_progress = in_progress
        self.result = result


def _deep_copy_result(result: SupervisorRunResult) -> SupervisorRunResult:
    """Return an independent copy of *result*.

    Uses Pydantic's ``model_validate(model_dump(mode="python"))`` so
    nested ``frozenset`` / ``Decimal`` / ``datetime`` fields survive
    the round-trip with their original types intact.  This matters
    because callers may mutate the returned object (e.g. attach it to
    a trace) without corrupting the store's internal copy.
    """
    return SupervisorRunResult.model_validate(result.model_dump(mode="python"))


class InMemoryRunStore:
    """Process-local :class:`RunStore` for tests and Phase 4 demos.

    Thread-safety: the store uses a single ``dict`` guarded by the
    GIL.  Async callers do not need additional locking because
    ``begin`` and ``complete`` are synchronous bodies wrapped in an
    ``async def`` — they cannot be interleaved by another coroutine
    at an ``await`` point.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RunEntry] = {}
        self._aborts: dict[str, str] = {}

    # -- introspection (test helpers) -------------------------------------

    def has_run(self, run_id: str) -> bool:
        return run_id in self._entries

    def is_in_progress(self, run_id: str) -> bool:
        entry = self._entries.get(run_id)
        return entry is not None and entry.in_progress

    def is_completed(self, run_id: str) -> bool:
        entry = self._entries.get(run_id)
        return entry is not None and not entry.in_progress and entry.result is not None

    def last_error_code(self, run_id: str) -> str | None:
        """Return the ``error_code`` recorded by the most recent
        ``abort()`` for *run_id*, or ``None`` if no abort happened.
        Test-only introspection helper.
        """
        return self._aborts.get(run_id)

    def clear(self) -> None:
        """Drop all stored entries.  Test-only."""
        self._entries.clear()
        self._aborts.clear()

    # -- Protocol ---------------------------------------------------------

    async def lookup_completed(
        self,
        run_id: str,
        plan_hash: str,
    ) -> SupervisorRunResult | None:
        """R2 P0-1: cache lookup path.

        Returns a deep copy of the stored result when the run is
        completed with the same ``plan_hash``.  Returns ``None`` for
        unknown, in-progress, or mismatched-plan runs — no exceptions
        raised, so callers can use this before any pre-flight check.
        """
        entry = self._entries.get(run_id)
        if entry is None or entry.in_progress:
            return None
        assert entry.result is not None
        if entry.plan_hash != plan_hash:
            return None
        return _deep_copy_result(entry.result)

    async def begin(
        self,
        run_id: str,
        plan_hash: str,
    ) -> RunLease:
        entry = self._entries.get(run_id)
        if entry is None:
            # Fresh run — mark in-progress, no result yet.  Generate
            # a fresh lease_id so complete/abort can verify identity.
            lease = RunLease(run_id=run_id, plan_hash=plan_hash)
            self._entries[run_id] = _RunEntry(
                run_id=run_id,
                plan_hash=plan_hash,
                lease_id=lease.lease_id,
                in_progress=True,
                result=None,
            )
            return lease

        if entry.in_progress:
            raise RunAlreadyInProgressError(f"run_id={run_id!r} is already in progress")

        # Completed run — plan_hash must match.
        assert entry.result is not None
        if entry.plan_hash != plan_hash:
            raise RunPlanConflictError(
                f"run_id={run_id!r} already completed with plan_hash="
                f"{entry.plan_hash[:12]!r}; incoming plan_hash="
                f"{plan_hash[:12]!r}"
            )
        return RunLease(
            run_id=run_id,
            plan_hash=plan_hash,
            cached_result=_deep_copy_result(entry.result),
        )

    async def complete(
        self,
        lease: RunLease,
        result: SupervisorRunResult,
    ) -> None:
        # R2 P1-1: verify lease identity before mutating the entry.
        # A stale ``complete`` (e.g. from a cancelled coroutine that
        # resumed after abort) must not corrupt a newer run.
        if lease.run_id != result.run_id or lease.plan_hash != result.plan_hash:
            raise SupervisorError(
                "complete() called with a lease whose identity does "
                f"not match result: lease=(run_id={lease.run_id!r}, "
                f"plan_hash={lease.plan_hash[:12]!r}) result=(run_id="
                f"{result.run_id!r}, plan_hash={result.plan_hash[:12]!r})"
            )
        entry = self._entries.get(result.run_id)
        if entry is None:
            # Defensive: complete() without begin().  Treat as a
            # programming error.
            raise RunAlreadyInProgressError(
                f"complete() called for run_id={result.run_id!r} "
                f"but no begin() lease exists"
            )
        if entry.in_progress and entry.lease_id != lease.lease_id:
            raise SupervisorError(
                f"complete() rejected: lease_id={lease.lease_id[:12]!r} "
                f"does not match in-progress entry lease_id="
                f"{entry.lease_id[:12]!r} for run_id={result.run_id!r}"
            )
        if not entry.in_progress:
            # Idempotent re-complete with the same result is allowed;
            # a different result for the same run is a conflict.
            assert entry.result is not None
            if entry.plan_hash == result.plan_hash:
                # Same plan_hash — silently ignore the duplicate.
                return
            raise RunPlanConflictError(
                f"run_id={result.run_id!r} already completed with "
                f"plan_hash={entry.plan_hash[:12]!r}; incoming="
                f"{result.plan_hash[:12]!r}"
            )

        # Store a deep copy so the caller cannot mutate the store's
        # internal state by mutating *result* after complete().
        stored = _deep_copy_result(result)
        self._entries[result.run_id] = _RunEntry(
            run_id=result.run_id,
            plan_hash=result.plan_hash,
            lease_id=lease.lease_id,
            in_progress=False,
            result=stored,
        )

    async def abort(
        self,
        lease: RunLease,
        *,
        error_code: str,
    ) -> None:
        """R2 P1-1: Release an in-progress lease.

        Behaviour:

        * If the run is unknown → no-op (record error_code for tests).
        * If the run is completed → no-op (a cached result wins;
          aborting cannot revoke a completed run).  We still record
          the error_code for test introspection.
        * If the run is in-progress with a *matching* ``lease_id`` →
          drop the entry entirely so a later ``begin`` succeeds.
        * If the run is in-progress with a *mismatched* ``lease_id``
          → reject the abort so a stale callback cannot delete a
          newer run's lease.

        ``plan_hash`` is informational — we do not enforce a match
        because the abort path is reached after a failure and tests
        may tamper with the plan.  ``lease_id`` is the authoritative
        identity check.
        """
        entry = self._entries.get(lease.run_id)
        if entry is None or not entry.in_progress:
            # No-op: nothing to release.
            self._aborts[lease.run_id] = error_code
            return
        if entry.lease_id != lease.lease_id:
            raise SupervisorError(
                f"abort() rejected: lease_id={lease.lease_id[:12]!r} "
                f"does not match in-progress entry lease_id="
                f"{entry.lease_id[:12]!r} for run_id={lease.run_id!r}; "
                f"a newer lease has been issued — refusing to delete it"
            )
        # Drop the in-progress entry.  A subsequent begin() with the
        # same run_id will create a fresh lease.
        del self._entries[lease.run_id]
        self._aborts[lease.run_id] = error_code


# ---------------------------------------------------------------------------
# Helpers for callers that need a sync deep copy without going through
# the Protocol (e.g. unit tests asserting defensive-copy semantics).
# ---------------------------------------------------------------------------


def defensive_copy_result(result: SupervisorRunResult) -> SupervisorRunResult:
    """Public alias for the internal deep-copy helper."""
    return _deep_copy_result(result)


__all__ = [
    "InMemoryRunStore",
    "RunLease",
    "RunStore",
    "defensive_copy_result",
]
