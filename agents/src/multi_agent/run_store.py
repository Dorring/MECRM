"""Phase 4 Run Idempotency Store.

The :class:`RunStore` Protocol guards against duplicate Handler
invocations for the same ``run_id``.  Phase 4 only ships an in-memory
implementation — database persistence is a Phase 5 concern.

Contract
--------

``lookup_run_identity(run_id, plan_hash) -> RunIdentity | None``
    R3 P1-1: read-only probe that determines cache/conflict/in-progress
    status in one call.  Returns ``None`` for unknown runs.  The
    Supervisor calls this *before* any side-effect-bearing pre-flight
    so it can pick the right path (cache hit / conflict / in-progress
    / new run) without interleaving with another coroutine's begin().

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
      of the stored result.  (Defensive; ``lookup_run_identity`` is
      the preferred cache path.)
    * If ``run_id`` is completed with a different ``plan_hash`` →
      raise :class:`RunPlanConflictError`.

``complete(lease, result) -> None``
    R3 P0-3: verifies the *three-part* lease identity
    (``run_id`` + ``plan_hash`` + ``lease_id``) against the in-progress
    entry.  Any mismatch is rejected — a stale callback cannot corrupt
    a newer run.

``abort(lease, *, error_code) -> None``
    R3 P0-3: same three-part identity check as ``complete``.  If the
    lease is stale (a newer lease has been issued for the same
    ``run_id``), the abort is rejected so an old callback cannot
    delete a newer run's lease.

The store is intentionally simple — no TTL, no eviction.  Phase 5
will add a Postgres-backed implementation with the same Protocol.
"""

from __future__ import annotations

import secrets
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from multi_agent.contracts import StrictContract
from multi_agent.execution import SupervisorRunResult
from multi_agent.execution_errors import (
    RunAlreadyInProgressError,
    RunPlanConflictError,
    SupervisorError,
)


# ---------------------------------------------------------------------------
# Lease — R3 P0-3: frozen StrictContract
# ---------------------------------------------------------------------------


class RunLease(StrictContract):
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

    R3 P0-3: ``RunLease`` is now a frozen :class:`StrictContract`
    (``frozen=True``, ``extra='forbid'``).  Previously it was a plain
    mutable class, which allowed callers to mutate ``lease.plan_hash``
    after ``begin()`` and trick ``complete()`` into accepting a
    mismatched result.  The frozen contract makes the three-part
    identity (``run_id`` + ``plan_hash`` + ``lease_id``) immutable
    for the lifetime of the lease.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    plan_hash: str
    lease_id: str = Field(default_factory=lambda: _generate_lease_id())
    cached_result: SupervisorRunResult | None = None

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
# RunIdentity — R3 P1-1: read-only probe result
# ---------------------------------------------------------------------------


RunIdentityStatus = Literal["in_progress", "completed", "conflict"]


class RunIdentity(StrictContract):
    """R3 P1-1: identity probe result.

    Returned by :meth:`RunStore.lookup_run_identity`.  Encapsulates
    the run's status with respect to a *specific* ``plan_hash`` so
    the Supervisor can pick the right path without calling ``begin()``
    (which has side effects).

    * ``status == "completed"`` and ``plan_hash_matches == True`` →
      cache hit; ``cached_result`` is a deep copy of the stored
      result.
    * ``status == "completed"`` and ``plan_hash_matches == False`` →
      conflict; the caller should raise ``RunPlanConflictError``.
    * ``status == "in_progress"`` → the caller should raise
      ``RunAlreadyInProgressError``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    plan_hash: str
    status: RunIdentityStatus
    plan_hash_matches: bool
    cached_result: SupervisorRunResult | None = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed" and self.plan_hash_matches


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RunStore(Protocol):
    """Idempotency boundary for :class:`SupervisorRuntime`."""

    async def lookup_run_identity(
        self,
        run_id: str,
        plan_hash: str,
    ) -> RunIdentity | None:
        """R3 P1-1: read-only identity probe.

        Returns ``None`` for unknown runs.  For known runs, returns a
        :class:`RunIdentity` describing the run's status with respect
        to the given ``plan_hash``.  Must NOT raise and must NOT
        mutate the store — this is the cache/conflict/in-progress
        determination step that runs *before* any side-effect-bearing
        pre-flight.
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

    async def lookup_run_identity(
        self,
        run_id: str,
        plan_hash: str,
    ) -> RunIdentity | None:
        """R3 P1-1: read-only identity probe.

        Determines cache/conflict/in-progress status for *run_id*
        with respect to *plan_hash* in one call, without side effects.
        """
        entry = self._entries.get(run_id)
        if entry is None:
            return None
        if entry.in_progress:
            return RunIdentity(
                run_id=run_id,
                plan_hash=plan_hash,
                status="in_progress",
                plan_hash_matches=(entry.plan_hash == plan_hash),
                cached_result=None,
            )
        # Completed run.
        assert entry.result is not None
        matches = entry.plan_hash == plan_hash
        return RunIdentity(
            run_id=run_id,
            plan_hash=plan_hash,
            status="completed",
            plan_hash_matches=matches,
            cached_result=(_deep_copy_result(entry.result) if matches else None),
        )

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
        # R3 P0-3: verify three-part lease identity (run_id +
        # plan_hash + lease_id) against the in-progress entry.
        # Any mismatch is rejected so a stale or tampered lease
        # cannot corrupt a newer run.
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
        # R3 P0-3: verify entry.plan_hash matches lease.plan_hash.
        # Previously only lease_id was checked, allowing a caller to
        # mutate lease.plan_hash after begin() and trick complete()
        # into accepting a result with a different plan_hash.
        if entry.in_progress and (
            entry.lease_id != lease.lease_id
            or entry.plan_hash != lease.plan_hash
            or entry.run_id != lease.run_id
        ):
            raise SupervisorError(
                f"complete() rejected: lease identity "
                f"(run_id={lease.run_id!r}, "
                f"plan_hash={lease.plan_hash[:12]!r}, "
                f"lease_id={lease.lease_id[:12]!r}) does not match "
                f"in-progress entry (run_id={entry.run_id!r}, "
                f"plan_hash={entry.plan_hash[:12]!r}, "
                f"lease_id={entry.lease_id[:12]!r})"
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
        """R3 P0-3: Release an in-progress lease with three-part
        identity verification.

        Behaviour:

        * If the run is unknown → no-op (record error_code for tests).
        * If the run is completed → no-op (a cached result wins;
          aborting cannot revoke a completed run).  We still record
          the error_code for test introspection.
        * If the run is in-progress with a *matching* three-part
          identity → drop the entry entirely so a later ``begin``
          succeeds.
        * If the run is in-progress with a *mismatched* identity →
          reject the abort so a stale callback cannot delete a
          newer run's lease.

        R3 P0-3: ``plan_hash`` is no longer informational — it is
        part of the authoritative identity check alongside
        ``run_id`` and ``lease_id``.
        """
        entry = self._entries.get(lease.run_id)
        if entry is None or not entry.in_progress:
            # No-op: nothing to release.
            self._aborts[lease.run_id] = error_code
            return
        if (
            entry.lease_id != lease.lease_id
            or entry.plan_hash != lease.plan_hash
            or entry.run_id != lease.run_id
        ):
            raise SupervisorError(
                f"abort() rejected: lease identity "
                f"(run_id={lease.run_id!r}, "
                f"plan_hash={lease.plan_hash[:12]!r}, "
                f"lease_id={lease.lease_id[:12]!r}) does not match "
                f"in-progress entry (run_id={entry.run_id!r}, "
                f"plan_hash={entry.plan_hash[:12]!r}, "
                f"lease_id={entry.lease_id[:12]!r}); a newer lease "
                f"has been issued — refusing to delete it"
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
    "RunIdentity",
    "RunIdentityStatus",
    "RunLease",
    "RunStore",
    "defensive_copy_result",
]
