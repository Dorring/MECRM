"""Phase 5B — Idempotency Store.

The idempotency store is the durable record of every execution
attempt keyed by ``(tenant_id, idempotency_key)``.  It guarantees:

* At most ONE in-flight execution per key (``CALL_STARTED`` blocks a
  second reservation → :class:`ExecutionAlreadyInProgressError`).
* Replay safety: the same key + the same ``execution_fingerprint``
  that previously SUCCEEDED returns the cached ``receipt_id``
  without re-invoking the adapter (``DEDUPLICATED``).
* Conflict detection: the same key + a DIFFERENT fingerprint is a
  fail-closed :class:`IdempotencyConflictError` (Phase 5B Section 11).
* UNKNOWN outcomes are NOT auto-retried — they require human
  intervention (Phase 5B Section 17).

Phase 5B R2 fixes (P0-7 / P0-8 / P0-9):

* **P0-7** — Dry-run executions are namespaced away from real
  executions so a ``dry_run=True`` run NEVER consumes the production
  idempotency key.  Dry-run records transition to
  ``DRY_RUN_SUCCEEDED`` (never ``SUCCEEDED``).  Dry-run uses the store
  key ``(tenant_id, "dry-run", idempotency_key)`` while real execution
  uses ``(tenant_id, "real", idempotency_key)``.
* **P0-8** — Idempotency scope (``GLOBAL`` / ``TENANT`` / ``NONE``)
  controls the store key shape and replay semantics.  ``NONE`` always
  creates a fresh record (no replay / no retry) and appends a unique
  ``reservation_id`` to avoid key collision.
* **P0-9** — Strict compare-and-set state machine.  ``IN_PROGRESS``
  is renamed to ``CALL_STARTED`` (alias retained) and every
  transition validates the current state against a legal-transition
  table.
"""

from __future__ import annotations

import asyncio
import uuid
from enum import StrEnum
from hmac import compare_digest
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.action_adapter import IdempotencyScope
from multi_agent.contracts import StrictContract
from multi_agent.execution_authorization import ExecutionStatus
from multi_agent.execution_error_codes import (
    ExecutionAlreadyInProgressError,
    ExecutionReceiptError,
    IdempotencyConflictError,
)
from multi_agent.serialization import stable_hash

if TYPE_CHECKING:
    from multi_agent.execution_receipts import ActionExecutionReceipt


# ---------------------------------------------------------------------------
# Idempotency state
# ---------------------------------------------------------------------------


class IdempotencyState(StrEnum):
    """Lifecycle of one idempotency record (Phase 5B R2 — P0-9).

    ``RESERVED`` — a reservation was created but the adapter call has
    not started yet.
    ``CALL_STARTED`` — the adapter call is in flight (formerly
    ``IN_PROGRESS``; the alias is retained for backward compatibility).
    ``SUCCEEDED`` — the adapter returned a definitive REAL success;
    the cached ``receipt_id`` is returned on replay.
    ``DRY_RUN_SUCCEEDED`` — a dry-run execution succeeded; this is
    NEVER equivalent to ``SUCCEEDED`` and never blocks a subsequent
    real execution with the same key (P0-7).
    ``FAILED`` — the adapter returned a definitive failure; the key
    MAY be retried (with a new fingerprint if the command changed).
    ``UNKNOWN`` — the outcome could not be confirmed (timeout,
    cancellation, connection loss).  NEVER auto-retried.
    """

    RESERVED = "reserved"
    CALL_STARTED = "call_started"
    # Backward-compatible alias (P0-9): ``IN_PROGRESS`` is now
    # ``CALL_STARTED``.  Both names resolve to the same member.
    IN_PROGRESS = "call_started"
    SUCCEEDED = "succeeded"
    DRY_RUN_SUCCEEDED = "dry_run_succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Legal compare-and-set state transitions (P0-9 strict state machine).
#
# RESERVED          → CALL_STARTED
# CALL_STARTED     → SUCCEEDED / FAILED / UNKNOWN / DRY_RUN_SUCCEEDED
# FAILED           → CALL_STARTED (only for safe retry)
# SUCCEEDED        → (terminal)
# DRY_RUN_SUCCEEDED → (terminal)
# UNKNOWN          → (terminal)
_LEGAL_TRANSITIONS: dict[IdempotencyState, frozenset[IdempotencyState]] = {
    IdempotencyState.RESERVED: frozenset({IdempotencyState.CALL_STARTED}),
    IdempotencyState.CALL_STARTED: frozenset(
        {
            IdempotencyState.SUCCEEDED,
            IdempotencyState.FAILED,
            IdempotencyState.UNKNOWN,
            IdempotencyState.DRY_RUN_SUCCEEDED,
        }
    ),
    IdempotencyState.FAILED: frozenset({IdempotencyState.CALL_STARTED}),
    IdempotencyState.SUCCEEDED: frozenset(),
    IdempotencyState.DRY_RUN_SUCCEEDED: frozenset(),
    IdempotencyState.UNKNOWN: frozenset(),
}


def _assert_transition(
    current: IdempotencyState,
    target: IdempotencyState,
) -> None:
    """Validate a CAS transition against the legal-transition table."""
    allowed = _LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(
            f"illegal idempotency state transition: "
            f"{current.value!r} -> {target.value!r}"
        )


# ---------------------------------------------------------------------------
# IdempotencyRecord
# ---------------------------------------------------------------------------


class IdempotencyRecord(StrictContract):
    """Frozen, hash-stable record of one idempotency slot.

    ``record_hash`` covers every field so a tampered record (e.g. a
    swapped ``receipt_id`` or a flipped ``dry_run`` flag) is detected
    at the boundary.

    P0-8: ``resource_type`` / ``resource_id`` / ``conflict_family``
    carry the optional resource-level conflict identity.  ``dry_run``,
    ``scope`` and ``reservation_id`` make the record self-locating so
    every store mutation can recompute its store key without an extra
    index.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    idempotency_key: str
    execution_fingerprint: str
    state: IdempotencyState
    command_id: str
    receipt_id: str | None = None
    # P0-7 / P0-8 namespace + resource identity (all optional).
    dry_run: bool = False
    scope: IdempotencyScope | None = None
    reservation_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    conflict_family: str | None = None
    record_hash: str = ""

    @field_validator(
        "tenant_id", "idempotency_key", "execution_fingerprint", "command_id"
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("IdempotencyRecord identity fields must not be blank")
        return v

    @model_validator(mode="after")
    def _verify_record_hash(self) -> IdempotencyRecord:
        expected = self.compute_hash()
        if not self.record_hash:
            object.__setattr__(self, "record_hash", expected)
        elif not compare_digest(self.record_hash, expected):
            raise ValueError("IdempotencyRecord.record_hash mismatch")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"record_hash"})

    def verify_integrity(self) -> None:
        if not compare_digest(self.record_hash, self.compute_hash()):
            raise ValueError(
                "IdempotencyRecord.record_hash does not match recomputed content"
            )


# ---------------------------------------------------------------------------
# Scope / resource key helpers (P0-8)
# ---------------------------------------------------------------------------


def compute_scope_key(
    tenant_id: str,
    idempotency_key: str,
    scope: IdempotencyScope,
) -> tuple[str, ...]:
    """Compute the store key for an idempotency slot given its scope.

    * ``GLOBAL`` → ``("global", idempotency_key)`` — unique across tenants.
    * ``TENANT`` → ``(tenant_id, idempotency_key)`` — unique within a tenant.
    * ``NONE``   → ``(tenant_id, idempotency_key, "none")`` — base key for a
      non-idempotent adapter; :meth:`InMemoryExecutionStore.reserve` appends a
      unique ``reservation_id`` so every attempt gets a fresh record (no
      replay / no retry).
    """
    if scope is IdempotencyScope.GLOBAL:
        return ("global", idempotency_key)
    if scope is IdempotencyScope.TENANT:
        return (tenant_id, idempotency_key)
    # NONE — non-idempotent adapter (base key; reserve appends a suffix).
    return (tenant_id, idempotency_key, "none")


def compute_resource_key(
    tenant_id: str,
    resource_type: str | None,
    resource_id: str | None,
    conflict_family: str | None,
) -> tuple[str, ...]:
    """Compute a resource-level conflict key (P0-8).

    Two executions that share the same resource key contend on the same
    external resource regardless of their idempotency key.  Optional
    fields collapse to ``""`` so the key is always a stable 4-tuple.
    """
    return (
        tenant_id,
        resource_type or "",
        resource_id or "",
        conflict_family or "",
    )


# ---------------------------------------------------------------------------
# ExecutionStore Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutionStore(Protocol):
    """Async idempotency store boundary.

    All methods are async so a future Redis-backed adapter can poll
    without changing call sites.  Implementations MUST be safe under
    concurrent ``reserve`` calls for the same key (compare-and-set).

    P0-6: ``complete_with_receipt`` atomically commits the terminal
    idempotency state AND the full :class:`ActionExecutionReceipt`.
    This prevents the crash-window where the store is SUCCEEDED but
    no trusted receipt exists.  ``get_receipt`` returns the original
    receipt for deterministic replay (P0-6 replay).

    P0-7 / P0-8 / P0-9: ``reserve`` accepts ``dry_run`` and ``scope``
    so the store key namespace can be selected at call time, and the
    CAS state machine (P0-9) is enforced on every transition.
    """

    async def reserve(
        self,
        tenant_id: str,
        key: str,
        fingerprint: str,
        command_id: str,
        *,
        dry_run: bool = False,
        scope: IdempotencyScope | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        conflict_family: str | None = None,
    ) -> IdempotencyRecord: ...

    async def mark_started(
        self,
        record: IdempotencyRecord,
        command_id: str,
    ) -> IdempotencyRecord: ...

    async def complete_with_receipt(
        self,
        record: IdempotencyRecord,
        receipt: ActionExecutionReceipt,
    ) -> IdempotencyRecord: ...

    async def mark_unknown(
        self,
        record: IdempotencyRecord,
        *,
        receipt_id: str | None = None,
    ) -> IdempotencyRecord: ...

    async def release_reservation(
        self,
        record: IdempotencyRecord,
    ) -> None: ...

    async def get(
        self,
        tenant_id: str,
        key: str,
        *,
        dry_run: bool = False,
        scope: IdempotencyScope | None = None,
    ) -> IdempotencyRecord | None: ...

    async def get_receipt(
        self,
        tenant_id: str,
        key: str,
        *,
        dry_run: bool = False,
        scope: IdempotencyScope | None = None,
    ) -> ActionExecutionReceipt | None: ...


# ---------------------------------------------------------------------------
# InMemoryExecutionStore
# ---------------------------------------------------------------------------


class InMemoryExecutionStore:
    """Async, compare-and-set, in-memory idempotency store.

    Concurrency-safe via a single :class:`asyncio.Lock`.  The lock is
    coarse (one lock for the whole store) — acceptable for tests and
    single-process executors; a Redis-backed store would use key-
    scoped locks.

    Semantics (Phase 5B Section 11 + R2):

    * ``reserve`` with a brand-new key → ``RESERVED`` record.
    * ``reserve`` with an existing SUCCEEDED key + same fingerprint →
      returns the cached record (caller treats as DEDUPLICATED).
    * ``reserve`` with an existing key + different fingerprint →
      :class:`IdempotencyConflictError` (fail-closed).
    * ``reserve`` with an existing CALL_STARTED key →
      :class:`ExecutionAlreadyInProgressError`.
    * ``reserve`` with an existing UNKNOWN key → returns the UNKNOWN
      record (caller MUST NOT auto-retry).
    * ``reserve`` with an existing FAILED key + same fingerprint →
      returns the FAILED record (caller MAY retry explicitly).
    * P0-7: ``dry_run=True`` reserves under the ``"dry-run"`` namespace
      and never collides with a ``dry_run=False`` (``"real"``) reservation
      for the same key.
    * P0-8: ``scope`` overrides the dry-run namespace when provided;
      ``NONE`` always creates a fresh record (no replay / no retry).
    * P0-9: every state transition is validated against
      :data:`_LEGAL_TRANSITIONS`.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, ...], IdempotencyRecord] = {}
        self._receipts: dict[tuple[str, ...], ActionExecutionReceipt] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Store-key computation
    # ------------------------------------------------------------------

    @staticmethod
    def _lookup_key(
        tenant_id: str,
        key: str,
        *,
        dry_run: bool,
        scope: IdempotencyScope | None,
    ) -> tuple[str, ...]:
        """Store key for the lookup methods (``reserve``/``get``/``get_receipt``).

        For ``NONE`` scope this returns the BASE key (without a
        ``reservation_id``); since every ``reserve`` stores under a
        unique suffix, ``get`` for ``NONE`` will not find a record —
        this is intentional (``NONE`` means no replay).
        """
        if scope is not None:
            return compute_scope_key(tenant_id, key, scope)
        namespace = "dry-run" if dry_run else "real"
        return (tenant_id, namespace, key)

    @staticmethod
    def _record_store_key(record: IdempotencyRecord) -> tuple[str, ...]:
        """Store key derived from a record (self-locating).

        Mirrors :meth:`_lookup_key` but resolves the namespace from the
        record's own ``dry_run`` / ``scope`` / ``reservation_id`` so that
        ``mark_started`` / ``complete_with_receipt`` / ``mark_unknown``
        can re-find the exact slot without an external index.  For
        ``NONE`` scope the unique ``reservation_id`` is appended so each
        attempt maps to its own slot.
        """
        scope = record.scope
        if scope is IdempotencyScope.NONE:
            return (
                record.tenant_id,
                record.idempotency_key,
                "none",
                record.reservation_id or "",
            )
        if scope is not None:
            return compute_scope_key(record.tenant_id, record.idempotency_key, scope)
        namespace = "dry-run" if record.dry_run else "real"
        return (record.tenant_id, namespace, record.idempotency_key)

    @staticmethod
    def _state_for_receipt(receipt: ActionExecutionReceipt) -> IdempotencyState:
        """Map an :class:`ExecutionStatus` to a terminal idempotency state."""
        status = receipt.status
        if status is ExecutionStatus.SUCCEEDED:
            return IdempotencyState.SUCCEEDED
        if status is ExecutionStatus.DRY_RUN_SUCCEEDED:
            return IdempotencyState.DRY_RUN_SUCCEEDED
        if status is ExecutionStatus.FAILED:
            return IdempotencyState.FAILED
        # UNKNOWN / CANCELLED / anything else → UNKNOWN (fail-closed).
        return IdempotencyState.UNKNOWN

    # ------------------------------------------------------------------
    # reserve
    # ------------------------------------------------------------------

    async def reserve(
        self,
        tenant_id: str,
        key: str,
        fingerprint: str,
        command_id: str,
        *,
        dry_run: bool = False,
        scope: IdempotencyScope | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        conflict_family: str | None = None,
    ) -> IdempotencyRecord:
        async with self._lock:
            # P0-8: NONE scope ALWAYS creates a fresh record — never
            # replay, never conflict-check.  A unique reservation_id
            # guarantees the slot key never collides.
            if scope is IdempotencyScope.NONE:
                record = IdempotencyRecord(
                    tenant_id=tenant_id,
                    idempotency_key=key,
                    execution_fingerprint=fingerprint,
                    state=IdempotencyState.RESERVED,
                    command_id=command_id,
                    dry_run=dry_run,
                    scope=scope,
                    reservation_id=uuid.uuid4().hex,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    conflict_family=conflict_family,
                )
                ck = self._record_store_key(record)
                self._records[ck] = record
                return record

            ck = self._lookup_key(tenant_id, key, dry_run=dry_run, scope=scope)
            existing = self._records.get(ck)
            if existing is None:
                record = IdempotencyRecord(
                    tenant_id=tenant_id,
                    idempotency_key=key,
                    execution_fingerprint=fingerprint,
                    state=IdempotencyState.RESERVED,
                    command_id=command_id,
                    dry_run=dry_run,
                    scope=scope,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    conflict_family=conflict_family,
                )
                self._records[ck] = record
                return record
            # Existing record — fingerprint MUST match (else conflict).
            if existing.execution_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} already used with a different "
                    f"execution fingerprint",
                )
            if existing.state == IdempotencyState.CALL_STARTED:
                raise ExecutionAlreadyInProgressError(
                    f"idempotency key {key!r} is already CALL_STARTED",
                )
            # RESERVED / SUCCEEDED / FAILED / UNKNOWN / DRY_RUN_SUCCEEDED —
            # return the existing record so the caller can decide what to do.
            return existing

    # ------------------------------------------------------------------
    # mark_started
    # ------------------------------------------------------------------

    async def mark_started(
        self,
        record: IdempotencyRecord,
        command_id: str,
    ) -> IdempotencyRecord:
        """Transition RESERVED (or FAILED, for safe retry) → CALL_STARTED.

        P0-9: the current stored state MUST be ``RESERVED`` or ``FAILED``;
        any other state is an illegal transition.
        """
        async with self._lock:
            ck = self._record_store_key(record)
            existing = self._records.get(ck)
            if existing is None:
                raise KeyError(
                    f"no idempotency record for ({record.tenant_id!r}, "
                    f"{record.idempotency_key!r})"
                )
            if existing.execution_fingerprint != record.execution_fingerprint:
                raise IdempotencyConflictError(
                    "fingerprint mismatch on mark_started",
                )
            _assert_transition(existing.state, IdempotencyState.CALL_STARTED)
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=IdempotencyState.CALL_STARTED,
                command_id=command_id,
                receipt_id=existing.receipt_id,
                dry_run=existing.dry_run,
                scope=existing.scope,
                reservation_id=existing.reservation_id,
                resource_type=existing.resource_type,
                resource_id=existing.resource_id,
                conflict_family=existing.conflict_family,
            )
            self._records[ck] = updated
            return updated

    # ------------------------------------------------------------------
    # complete (deprecated) — P0-9
    # ------------------------------------------------------------------

    async def _complete_deprecated(
        self,
        record: IdempotencyRecord,
        receipt_id: str,
        *,
        succeeded: bool,
    ) -> IdempotencyRecord:
        """DEPRECATED terminal commit (no receipt verification).

        Retained for backward compatibility only.  New callers MUST
        use :meth:`complete_with_receipt` which atomically commits the
        full :class:`ActionExecutionReceipt`.  P0-9: the current stored
        state MUST be ``CALL_STARTED``.
        """
        async with self._lock:
            ck = self._record_store_key(record)
            existing = self._records.get(ck)
            if existing is None:
                raise KeyError(
                    f"no idempotency record for ({record.tenant_id!r}, "
                    f"{record.idempotency_key!r})"
                )
            if existing.execution_fingerprint != record.execution_fingerprint:
                raise IdempotencyConflictError(
                    "fingerprint mismatch on complete",
                )
            new_state = (
                IdempotencyState.SUCCEEDED if succeeded else IdempotencyState.FAILED
            )
            _assert_transition(existing.state, new_state)
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=new_state,
                command_id=existing.command_id,
                receipt_id=receipt_id,
                dry_run=existing.dry_run,
                scope=existing.scope,
                reservation_id=existing.reservation_id,
                resource_type=existing.resource_type,
                resource_id=existing.resource_id,
                conflict_family=existing.conflict_family,
            )
            self._records[ck] = updated
            return updated

    # ------------------------------------------------------------------
    # mark_unknown
    # ------------------------------------------------------------------

    async def mark_unknown(
        self,
        record: IdempotencyRecord,
        *,
        receipt_id: str | None = None,
    ) -> IdempotencyRecord:
        """Mark an in-flight record as UNKNOWN (fail-closed).

        Phase 5B Section 17: UNKNOWN outcomes are NEVER auto-retried.
        P0-9: the current stored state MUST be ``CALL_STARTED``.
        """
        async with self._lock:
            ck = self._record_store_key(record)
            existing = self._records.get(ck)
            if existing is None:
                raise KeyError(
                    f"no idempotency record for ({record.tenant_id!r}, "
                    f"{record.idempotency_key!r})"
                )
            if existing.execution_fingerprint != record.execution_fingerprint:
                raise IdempotencyConflictError(
                    "fingerprint mismatch on mark_unknown",
                )
            _assert_transition(existing.state, IdempotencyState.UNKNOWN)
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=IdempotencyState.UNKNOWN,
                command_id=existing.command_id,
                receipt_id=receipt_id,
                dry_run=existing.dry_run,
                scope=existing.scope,
                reservation_id=existing.reservation_id,
                resource_type=existing.resource_type,
                resource_id=existing.resource_id,
                conflict_family=existing.conflict_family,
            )
            self._records[ck] = updated
            return updated

    # ------------------------------------------------------------------
    # complete_with_receipt (P0-6 + P0-7 + P0-9)
    # ------------------------------------------------------------------

    async def complete_with_receipt(
        self,
        record: IdempotencyRecord,
        receipt: ActionExecutionReceipt,
    ) -> IdempotencyRecord:
        """P0-6: atomically commit terminal state + full receipt.

        The receipt is stored alongside the idempotency record so
        replay returns the ORIGINAL trusted receipt, not a fabricated
        DEDUPLICATED one.  Both writes happen under the same lock — a
        crash between them is impossible.

        P0-7: a ``DRY_RUN_SUCCEEDED`` receipt transitions the record to
        ``DRY_RUN_SUCCEEDED`` (never ``SUCCEEDED``).

        P0-9: the current stored state MUST be ``CALL_STARTED`` and the
        receipt is independently verified against the record.
        """
        async with self._lock:
            ck = self._record_store_key(record)
            existing = self._records.get(ck)
            if existing is None:
                raise KeyError(
                    f"no idempotency record for ({record.tenant_id!r}, "
                    f"{record.idempotency_key!r})"
                )
            if existing.execution_fingerprint != record.execution_fingerprint:
                raise IdempotencyConflictError(
                    "fingerprint mismatch on complete_with_receipt",
                )
            # P0-9: strict CAS — current state MUST be CALL_STARTED.
            target = self._state_for_receipt(receipt)
            _assert_transition(existing.state, target)
            # P0-6 / P0-9: independently verify the trusted receipt.
            receipt.verify_integrity()
            if receipt.tenant_id != existing.tenant_id:
                raise ExecutionReceiptError(
                    f"Receipt {receipt.receipt_id!r}: tenant_id "
                    f"{receipt.tenant_id!r} != record {existing.tenant_id!r}"
                )
            if receipt.idempotency_key != existing.idempotency_key:
                raise ExecutionReceiptError(
                    f"Receipt {receipt.receipt_id!r}: idempotency_key "
                    f"{receipt.idempotency_key!r} != record "
                    f"{existing.idempotency_key!r}"
                )
            if receipt.execution_fingerprint != existing.execution_fingerprint:
                raise ExecutionReceiptError(
                    f"Receipt {receipt.receipt_id!r}: execution_fingerprint "
                    f"mismatch with record"
                )
            if receipt.command_id != existing.command_id:
                raise ExecutionReceiptError(
                    f"Receipt {receipt.receipt_id!r}: command_id "
                    f"{receipt.command_id!r} != record {existing.command_id!r}"
                )
            if not receipt.receipt_id or not receipt.receipt_id.strip():
                raise ExecutionReceiptError("Receipt.receipt_id must not be blank")
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=target,
                command_id=existing.command_id,
                receipt_id=receipt.receipt_id,
                dry_run=existing.dry_run,
                scope=existing.scope,
                reservation_id=existing.reservation_id,
                resource_type=existing.resource_type,
                resource_id=existing.resource_id,
                conflict_family=existing.conflict_family,
            )
            self._records[ck] = updated
            self._receipts[ck] = receipt
            return updated

    # ------------------------------------------------------------------
    # release_reservation (P0-9)
    # ------------------------------------------------------------------

    async def release_reservation(
        self,
        record: IdempotencyRecord,
    ) -> None:
        """Release a RESERVED slot for a pre-call cancellation (P0-9).

        Deletes the record entirely.  The current stored state MUST be
        ``RESERVED`` — releasing an in-flight or terminal slot is an
        illegal transition.
        """
        async with self._lock:
            ck = self._record_store_key(record)
            existing = self._records.get(ck)
            if existing is None:
                raise KeyError(
                    f"no idempotency record for ({record.tenant_id!r}, "
                    f"{record.idempotency_key!r})"
                )
            if existing.execution_fingerprint != record.execution_fingerprint:
                raise IdempotencyConflictError(
                    "fingerprint mismatch on release_reservation",
                )
            if existing.state != IdempotencyState.RESERVED:
                raise ValueError(
                    f"release_reservation: expected state "
                    f"{IdempotencyState.RESERVED.value!r}, found "
                    f"{existing.state.value!r}"
                )
            del self._records[ck]
            self._receipts.pop(ck, None)

    # ------------------------------------------------------------------
    # get / get_receipt
    # ------------------------------------------------------------------

    async def get(
        self,
        tenant_id: str,
        key: str,
        *,
        dry_run: bool = False,
        scope: IdempotencyScope | None = None,
    ) -> IdempotencyRecord | None:
        async with self._lock:
            ck = self._lookup_key(tenant_id, key, dry_run=dry_run, scope=scope)
            return self._records.get(ck)

    async def get_receipt(
        self,
        tenant_id: str,
        key: str,
        *,
        dry_run: bool = False,
        scope: IdempotencyScope | None = None,
    ) -> ActionExecutionReceipt | None:
        """P0-6: return the original trusted receipt for replay."""
        async with self._lock:
            ck = self._lookup_key(tenant_id, key, dry_run=dry_run, scope=scope)
            return self._receipts.get(ck)


__all__ = [
    "ExecutionStore",
    "IdempotencyRecord",
    "IdempotencyState",
    "InMemoryExecutionStore",
    "compute_resource_key",
    "compute_scope_key",
]
