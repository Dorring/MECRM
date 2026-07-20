"""Phase 4 Run Idempotency Store.

The :class:`RunStore` Protocol guards against duplicate Handler
invocations for the same ``run_id``.  Phase 4 only ships an in-memory
implementation — database persistence is a Phase 5 concern.

Contract
--------

``begin(run_id, plan_hash) -> RunLease``
    Called by :class:`SupervisorRuntime` *before* any Handler runs.

    * If ``run_id`` is unknown → mark it in-progress and return a
      :class:`RunLease` whose ``cached_result is None``.
    * If ``run_id`` is in-progress → raise
      :class:`RunAlreadyInProgressError`.
    * If ``run_id`` is completed with the same ``plan_hash`` → return
      a :class:`RunLease` whose ``cached_result`` is a **deep copy**
      of the stored result.  The Supervisor returns it directly
      without invoking any Handler.
    * If ``run_id`` is completed with a different ``plan_hash`` →
      raise :class:`RunPlanConflictError`.

``complete(result) -> None``
    Called by the Supervisor after the run reaches a terminal status.
    Stores a deep copy of *result* so later callers receive an
    independent object graph.

The store is intentionally simple — no TTL, no eviction.  Phase 5
will add a Postgres-backed implementation with the same Protocol.
"""

from __future__ import annotations

from typing import Protocol

from multi_agent.execution import SupervisorRunResult
from multi_agent.execution_errors import (
    RunAlreadyInProgressError,
    RunPlanConflictError,
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
    """

    __slots__ = ("run_id", "plan_hash", "cached_result")

    def __init__(
        self,
        *,
        run_id: str,
        plan_hash: str,
        cached_result: SupervisorRunResult | None = None,
    ) -> None:
        self.run_id = run_id
        self.plan_hash = plan_hash
        self.cached_result = cached_result

    @property
    def is_cached(self) -> bool:
        return self.cached_result is not None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RunStore(Protocol):
    """Idempotency boundary for :class:`SupervisorRuntime`."""

    async def begin(
        self,
        run_id: str,
        plan_hash: str,
    ) -> RunLease: ...

    async def complete(
        self,
        result: SupervisorRunResult,
    ) -> None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class _RunEntry:
    """Internal storage for one run.

    Either ``in_progress=True`` (no result yet) or ``result`` is set.
    Both fields are never set simultaneously.
    """

    __slots__ = ("run_id", "plan_hash", "in_progress", "result")

    def __init__(
        self,
        *,
        run_id: str,
        plan_hash: str,
        in_progress: bool,
        result: SupervisorRunResult | None,
    ) -> None:
        self.run_id = run_id
        self.plan_hash = plan_hash
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

    # -- introspection (test helpers) -------------------------------------

    def has_run(self, run_id: str) -> bool:
        return run_id in self._entries

    def is_in_progress(self, run_id: str) -> bool:
        entry = self._entries.get(run_id)
        return entry is not None and entry.in_progress

    def is_completed(self, run_id: str) -> bool:
        entry = self._entries.get(run_id)
        return entry is not None and not entry.in_progress and entry.result is not None

    def clear(self) -> None:
        """Drop all stored entries.  Test-only."""
        self._entries.clear()

    # -- Protocol ---------------------------------------------------------

    async def begin(
        self,
        run_id: str,
        plan_hash: str,
    ) -> RunLease:
        entry = self._entries.get(run_id)
        if entry is None:
            # Fresh run — mark in-progress, no result yet.
            self._entries[run_id] = _RunEntry(
                run_id=run_id,
                plan_hash=plan_hash,
                in_progress=True,
                result=None,
            )
            return RunLease(run_id=run_id, plan_hash=plan_hash, cached_result=None)

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
        result: SupervisorRunResult,
    ) -> None:
        entry = self._entries.get(result.run_id)
        if entry is None:
            # Defensive: complete() without begin().  Treat as a
            # programming error.
            raise RunAlreadyInProgressError(
                f"complete() called for run_id={result.run_id!r} "
                f"but no begin() lease exists"
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
            in_progress=False,
            result=stored,
        )


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
