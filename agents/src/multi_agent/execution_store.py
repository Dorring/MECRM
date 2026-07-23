"""Phase 5B — Idempotency Store.

The idempotency store is the durable record of every execution
attempt keyed by ``(tenant_id, idempotency_key)``.  It guarantees:

* At most ONE in-flight execution per key (``IN_PROGRESS`` blocks a
  second reservation → :class:`ExecutionAlreadyInProgressError`).
* Replay safety: the same key + the same ``execution_fingerprint``
  that previously SUCCEEDED returns the cached ``receipt_id``
  without re-invoking the adapter (``DEDUPLICATED``).
* Conflict detection: the same key + a DIFFERENT fingerprint is a
  fail-closed :class:`IdempotencyConflictError` (Phase 5B Section 11).
* UNKNOWN outcomes are NOT auto-retried — they require human
  intervention (Phase 5B Section 17).
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from hmac import compare_digest
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.contracts import StrictContract
from multi_agent.execution_error_codes import (
    ExecutionAlreadyInProgressError,
    IdempotencyConflictError,
)
from multi_agent.serialization import stable_hash


# ---------------------------------------------------------------------------
# Idempotency state
# ---------------------------------------------------------------------------


class IdempotencyState(StrEnum):
    """Lifecycle of one idempotency record.

    ``RESERVED`` — a reservation was created but execution has not
    started yet.
    ``IN_PROGRESS`` — the adapter call is in flight.
    ``SUCCEEDED`` — the adapter returned a definitive success; the
    cached ``receipt_id`` is returned on replay.
    ``FAILED`` — the adapter returned a definitive failure; the key
    MAY be retried (with a new fingerprint if the command changed).
    ``UNKNOWN`` — the outcome could not be confirmed (timeout,
    cancellation, connection loss).  NEVER auto-retried.
    """

    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# IdempotencyRecord
# ---------------------------------------------------------------------------


class IdempotencyRecord(StrictContract):
    """Frozen, hash-stable record of one idempotency slot.

    ``record_hash`` covers every field so a tampered record (e.g. a
    swapped ``receipt_id``) is detected at the boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    idempotency_key: str
    execution_fingerprint: str
    state: IdempotencyState
    command_id: str
    receipt_id: str | None = None
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
# ExecutionStore Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutionStore(Protocol):
    """Async idempotency store boundary.

    All methods are async so a future Redis-backed adapter can poll
    without changing call sites.  Implementations MUST be safe under
    concurrent ``reserve`` calls for the same key (compare-and-set).
    """

    async def reserve(
        self,
        tenant_id: str,
        key: str,
        fingerprint: str,
        command_id: str,
    ) -> IdempotencyRecord: ...

    async def mark_started(
        self,
        record: IdempotencyRecord,
        command_id: str,
    ) -> IdempotencyRecord: ...

    async def complete(
        self,
        record: IdempotencyRecord,
        receipt_id: str,
        *,
        succeeded: bool,
    ) -> IdempotencyRecord: ...

    async def mark_unknown(
        self,
        record: IdempotencyRecord,
    ) -> IdempotencyRecord: ...

    async def get(
        self,
        tenant_id: str,
        key: str,
    ) -> IdempotencyRecord | None: ...


# ---------------------------------------------------------------------------
# InMemoryExecutionStore
# ---------------------------------------------------------------------------


class InMemoryExecutionStore:
    """Async, compare-and-set, in-memory idempotency store.

    Concurrency-safe via a single :class:`asyncio.Lock`.  The lock is
    coarse (one lock for the whole store) — acceptable for tests and
    single-process executors; a Redis-backed store would use key-
    scoped locks.

    Semantics (Phase 5B Section 11):

    * ``reserve`` with a brand-new key → ``RESERVED`` record.
    * ``reserve`` with an existing SUCCEEDED key + same fingerprint →
      returns the cached record (caller treats as DEDUPLICATED).
    * ``reserve`` with an existing key + different fingerprint →
      :class:`IdempotencyConflictError` (fail-closed).
    * ``reserve`` with an existing IN_PROGRESS key →
      :class:`ExecutionAlreadyInProgressError`.
    * ``reserve`` with an existing UNKNOWN key → returns the UNKNOWN
      record (caller MUST NOT auto-retry).
    * ``reserve`` with an existing FAILED key + same fingerprint →
      returns the FAILED record (caller MAY retry explicitly).
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        tenant_id: str,
        key: str,
        fingerprint: str,
        command_id: str,
    ) -> IdempotencyRecord:
        async with self._lock:
            ck = (tenant_id, key)
            existing = self._records.get(ck)
            if existing is None:
                record = IdempotencyRecord(
                    tenant_id=tenant_id,
                    idempotency_key=key,
                    execution_fingerprint=fingerprint,
                    state=IdempotencyState.RESERVED,
                    command_id=command_id,
                )
                self._records[ck] = record
                return record
            # Existing record — fingerprint MUST match (else conflict).
            if existing.execution_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} already used with a different "
                    f"execution fingerprint",
                )
            if existing.state == IdempotencyState.IN_PROGRESS:
                raise ExecutionAlreadyInProgressError(
                    f"idempotency key {key!r} is already IN_PROGRESS",
                )
            # RESERVED / SUCCEEDED / FAILED / UNKNOWN — return the
            # existing record so the caller can decide what to do.
            return existing

    async def mark_started(
        self,
        record: IdempotencyRecord,
        command_id: str,
    ) -> IdempotencyRecord:
        async with self._lock:
            ck = (record.tenant_id, record.idempotency_key)
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
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=IdempotencyState.IN_PROGRESS,
                command_id=command_id,
                receipt_id=existing.receipt_id,
            )
            self._records[ck] = updated
            return updated

    async def complete(
        self,
        record: IdempotencyRecord,
        receipt_id: str,
        *,
        succeeded: bool,
    ) -> IdempotencyRecord:
        async with self._lock:
            ck = (record.tenant_id, record.idempotency_key)
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
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=new_state,
                command_id=existing.command_id,
                receipt_id=receipt_id,
            )
            self._records[ck] = updated
            return updated

    async def mark_unknown(
        self,
        record: IdempotencyRecord,
        *,
        receipt_id: str | None = None,
    ) -> IdempotencyRecord:
        """Mark an in-flight record as UNKNOWN (fail-closed).

        Phase 5B Section 17: UNKNOWN outcomes are NEVER auto-retried.
        """
        async with self._lock:
            ck = (record.tenant_id, record.idempotency_key)
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
            updated = IdempotencyRecord(
                tenant_id=existing.tenant_id,
                idempotency_key=existing.idempotency_key,
                execution_fingerprint=existing.execution_fingerprint,
                state=IdempotencyState.UNKNOWN,
                command_id=existing.command_id,
                receipt_id=receipt_id,
            )
            self._records[ck] = updated
            return updated

    async def get(
        self,
        tenant_id: str,
        key: str,
    ) -> IdempotencyRecord | None:
        async with self._lock:
            return self._records.get((tenant_id, key))


__all__ = [
    "ExecutionStore",
    "IdempotencyRecord",
    "IdempotencyState",
    "InMemoryExecutionStore",
]
