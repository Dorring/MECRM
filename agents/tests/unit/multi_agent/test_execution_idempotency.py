"""Phase 5B — Idempotency Store counterexample tests (Section 33).

Covers the idempotency invariants enforced by
:class:`InMemoryExecutionStore` and :class:`IdempotencyRecord`:

* A brand-new key reserves a ``RESERVED`` slot
  (``reserve_new_key_creates_reserved_record``).
* The same key + the same fingerprint that previously SUCCEEDED returns
  the cached record (replay / DEDUPLICATED) without re-invoking the
  adapter (``reserve_succeeded_same_fingerprint_returns_cached``).
* The same key + a DIFFERENT fingerprint is a fail-closed
  :class:`IdempotencyConflictError`
  (``reserve_different_fingerprint_raises_conflict``).
* A CALL_STARTED key blocks a second reservation
  (``reserve_in_progress_raises_already_in_progress``).
* An UNKNOWN outcome is returned as-is — NEVER auto-retried
  (``reserve_unknown_returns_record_without_retry``).
* A FAILED key + the same fingerprint returns the FAILED record so the
  caller may retry explicitly
  (``reserve_failed_same_fingerprint_returns_failed``).
* ``mark_started`` transitions RESERVED → CALL_STARTED
  (``mark_started_transitions_to_call_started``).

Phase 5B R2 (P0-7 / P0-8 / P0-9) additions:

* ``IN_PROGRESS`` is an alias for ``CALL_STARTED`` (P0-9).
* ``complete_with_receipt`` atomically commits terminal state + the
  full :class:`ActionExecutionReceipt` (P0-6); the receipt is
  independently verified against the record (tenant / idempotency_key /
  fingerprint / command_id).
* A ``DRY_RUN_SUCCEEDED`` receipt transitions to
  ``DRY_RUN_SUCCEEDED`` (never ``SUCCEEDED``) — P0-7.
* Strict CAS transitions are enforced — RESERVED → SUCCEEDED is illegal
  (P0-9).
* ``mark_unknown`` sets the UNKNOWN terminal state
  (``mark_unknown_sets_unknown_state``).
* :class:`IdempotencyRecord` is frozen and hash-stable — a tampered
  ``record_hash`` is detected at construction and via
  :meth:`verify_integrity`
  (``tampered_record_hash_detected_at_construction``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from phase5b_helpers import TENANT, run_async
from pydantic import ValidationError

from multi_agent.execution_authorization import ExecutionStatus
from multi_agent.execution_error_codes import (
    ExecutionAlreadyInProgressError,
    ExecutionReceiptError,
    IdempotencyConflictError,
)
from multi_agent.execution_receipts import ActionExecutionReceipt
from multi_agent.execution_store import (
    IdempotencyRecord,
    IdempotencyState,
    InMemoryExecutionStore,
)

_FP_1 = "fp-aaa" + "0" * 59
_FP_2 = "fp-bbb" + "0" * 59
_CMD_1 = "cmd-001"
_CMD_2 = "cmd-002"
_KEY_1 = "idem-001"

_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_TS_LATER = _TS + timedelta(seconds=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    tenant_id: str = TENANT,
    key: str = _KEY_1,
    fingerprint: str = _FP_1,
    state: IdempotencyState = IdempotencyState.RESERVED,
    command_id: str = _CMD_1,
    receipt_id: str | None = None,
) -> IdempotencyRecord:
    return IdempotencyRecord(
        tenant_id=tenant_id,
        idempotency_key=key,
        execution_fingerprint=fingerprint,
        state=state,
        command_id=command_id,
        receipt_id=receipt_id,
    )


def _make_receipt_for_record(
    record: IdempotencyRecord,
    *,
    receipt_id: str = "rcpt-001",
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    run_id: str = "run-test",
    proposal_id: str = "prop-test",
    authorization_hash: str = "auth" + "0" * 60,
    adapter_id: str = "noop-adapter",
    adapter_version: str = "1.0.0",
    adapter_registry_hash: str = "reg" + "0" * 60,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> ActionExecutionReceipt:
    """Build a valid :class:`ActionExecutionReceipt` matching a record.

    The receipt's ``tenant_id``, ``idempotency_key``,
    ``execution_fingerprint`` and ``command_id`` are copied from the
    record so :meth:`complete_with_receipt` (P0-9) accepts it.  The
    ``executed`` flag is derived from ``status`` to satisfy the
    receipt's own status ↔ executed invariant.
    """
    started = started_at or _TS
    completed = completed_at or _TS_LATER
    if status is ExecutionStatus.SUCCEEDED:
        executed: bool | None = True
    elif status in (
        ExecutionStatus.FAILED,
        ExecutionStatus.DRY_RUN_SUCCEEDED,
    ):
        executed = False
    else:
        executed = None
    return ActionExecutionReceipt(
        receipt_id=receipt_id,
        command_id=record.command_id,
        tenant_id=record.tenant_id,
        run_id=run_id,
        proposal_id=proposal_id,
        authorization_hash=authorization_hash,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        adapter_registry_hash=adapter_registry_hash,
        idempotency_key=record.idempotency_key,
        execution_fingerprint=record.execution_fingerprint,
        status=status,
        executed=executed,
        started_at=started,
        completed_at=completed,
    )


# ---------------------------------------------------------------------------
# IdempotencyState enum
# ---------------------------------------------------------------------------


class TestIdempotencyState:
    def test_states_have_distinct_values(self) -> None:
        # P0-9: IN_PROGRESS renamed to CALL_STARTED; DRY_RUN_SUCCEEDED
        # added (P0-7).  Aliases are NOT yielded by iteration.
        values = {s.value for s in IdempotencyState}
        assert values == {
            "reserved",
            "call_started",
            "succeeded",
            "dry_run_succeeded",
            "failed",
            "unknown",
        }

    def test_in_progress_is_backwards_compatible_alias(self) -> None:
        # P0-9: IN_PROGRESS is retained as an alias for CALL_STARTED so
        # legacy callers keep resolving.
        assert IdempotencyState.IN_PROGRESS is IdempotencyState.CALL_STARTED
        assert IdempotencyState.IN_PROGRESS.value == "call_started"


# ---------------------------------------------------------------------------
# IdempotencyRecord
# ---------------------------------------------------------------------------


class TestIdempotencyRecord:
    def test_record_auto_computes_hash(self) -> None:
        record = _make_record()
        assert record.record_hash != ""
        assert len(record.record_hash) == 64

    def test_record_is_frozen(self) -> None:
        record = _make_record()
        with pytest.raises(ValidationError):
            record.state = IdempotencyState.SUCCEEDED  # type: ignore[misc]

    def test_record_verify_integrity_passes(self) -> None:
        record = _make_record()
        record.verify_integrity()  # no raise

    def test_tampered_record_hash_detected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            IdempotencyRecord(
                tenant_id=TENANT,
                idempotency_key=_KEY_1,
                execution_fingerprint=_FP_1,
                state=IdempotencyState.RESERVED,
                command_id=_CMD_1,
                record_hash="tampered-hash",
            )

    def test_verify_integrity_detects_tamper(self) -> None:
        record = _make_record()
        object.__setattr__(record, "receipt_id", "swapped-receipt")
        with pytest.raises(ValueError):
            record.verify_integrity()

    def test_blank_identity_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IdempotencyRecord(
                tenant_id="",
                idempotency_key=_KEY_1,
                execution_fingerprint=_FP_1,
                state=IdempotencyState.RESERVED,
                command_id=_CMD_1,
            )

    def test_record_hash_is_deterministic(self) -> None:
        r1 = _make_record()
        r2 = _make_record()
        assert r1.record_hash == r2.record_hash

    def test_record_hash_differs_on_content_change(self) -> None:
        r1 = _make_record(state=IdempotencyState.RESERVED)
        r2 = _make_record(state=IdempotencyState.SUCCEEDED)
        assert r1.record_hash != r2.record_hash


# ---------------------------------------------------------------------------
# InMemoryExecutionStore — reserve
# ---------------------------------------------------------------------------


class TestReserve:
    def test_reserve_new_key_creates_reserved_record(self) -> None:
        store = InMemoryExecutionStore()
        record = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        assert record.state == IdempotencyState.RESERVED
        assert record.tenant_id == TENANT
        assert record.idempotency_key == _KEY_1
        assert record.execution_fingerprint == _FP_1
        assert record.command_id == _CMD_1

    def test_reserve_succeeded_same_fingerprint_returns_cached(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(
            r1, receipt_id="rcpt-001", status=ExecutionStatus.SUCCEEDED
        )
        run_async(store.complete_with_receipt(r1, receipt))
        # Replay with same fingerprint → returns SUCCEEDED record.
        replay = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_2))
        assert replay.state == IdempotencyState.SUCCEEDED
        assert replay.receipt_id == "rcpt-001"

    def test_reserve_different_fingerprint_raises_conflict(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        with pytest.raises(IdempotencyConflictError):
            run_async(store.reserve(TENANT, _KEY_1, _FP_2, _CMD_2))

    def test_reserve_in_progress_raises_already_in_progress(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        with pytest.raises(ExecutionAlreadyInProgressError):
            run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_2))

    def test_reserve_unknown_returns_record_without_retry(self) -> None:
        """An UNKNOWN outcome is returned as-is — the caller MUST NOT
        auto-retry (Phase 5B Section 17)."""
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        run_async(store.mark_unknown(r1))
        replay = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_2))
        assert replay.state == IdempotencyState.UNKNOWN

    def test_reserve_failed_same_fingerprint_returns_failed(self) -> None:
        """A FAILED key + the same fingerprint returns the FAILED
        record so the caller MAY retry explicitly."""
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(
            r1, receipt_id="rcpt-fail", status=ExecutionStatus.FAILED
        )
        run_async(store.complete_with_receipt(r1, receipt))
        replay = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_2))
        assert replay.state == IdempotencyState.FAILED

    def test_reserve_reserved_same_fingerprint_returns_reserved(self) -> None:
        """Re-reserving a RESERVED key with the same fingerprint
        returns the existing RESERVED record."""
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        replay = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_2))
        assert replay.state == IdempotencyState.RESERVED

    def test_reserve_is_tenant_scoped(self) -> None:
        """The same key in two different tenants does NOT conflict."""
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        # Different tenant — no conflict.
        record = run_async(store.reserve("tenant-other", _KEY_1, _FP_1, _CMD_2))
        assert record.tenant_id == "tenant-other"
        assert record.state == IdempotencyState.RESERVED


# ---------------------------------------------------------------------------
# InMemoryExecutionStore — mark_started
# ---------------------------------------------------------------------------


class TestMarkStarted:
    def test_mark_started_transitions_to_call_started(self) -> None:
        # P0-9: RESERVED → CALL_STARTED (renamed from IN_PROGRESS).
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        updated = run_async(store.mark_started(r1, _CMD_1))
        assert updated.state == IdempotencyState.CALL_STARTED
        assert updated.command_id == _CMD_1

    def test_mark_started_on_call_started_is_illegal_transition(self) -> None:
        # P0-9: strict CAS — CALL_STARTED → CALL_STARTED is illegal.
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        started = run_async(store.mark_started(r1, _CMD_1))
        with pytest.raises(ValueError, match="illegal idempotency state transition"):
            run_async(store.mark_started(started, _CMD_1))

    def test_mark_started_fingerprint_mismatch_raises_conflict(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        tampered = _make_record(fingerprint=_FP_2)
        with pytest.raises(IdempotencyConflictError):
            run_async(store.mark_started(tampered, _CMD_1))

    def test_mark_started_unknown_key_raises_keyerror(self) -> None:
        store = InMemoryExecutionStore()
        phantom = _make_record()
        with pytest.raises(KeyError):
            run_async(store.mark_started(phantom, _CMD_1))


# ---------------------------------------------------------------------------
# InMemoryExecutionStore — complete_with_receipt (P0-6 / P0-7 / P0-9)
# ---------------------------------------------------------------------------


class TestCompleteWithReceipt:
    def test_complete_succeeded_sets_succeeded_state(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(
            r1, receipt_id="rcpt-001", status=ExecutionStatus.SUCCEEDED
        )
        completed = run_async(store.complete_with_receipt(r1, receipt))
        assert completed.state == IdempotencyState.SUCCEEDED
        assert completed.receipt_id == "rcpt-001"

    def test_complete_failed_sets_failed_state(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(
            r1, receipt_id="rcpt-fail", status=ExecutionStatus.FAILED
        )
        completed = run_async(store.complete_with_receipt(r1, receipt))
        assert completed.state == IdempotencyState.FAILED
        assert completed.receipt_id == "rcpt-fail"

    def test_complete_dry_run_sets_dry_run_succeeded_state(self) -> None:
        # P0-7: a DRY_RUN_SUCCEEDED receipt transitions to
        # DRY_RUN_SUCCEEDED (never SUCCEEDED).
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1, dry_run=True))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(
            r1,
            receipt_id="rcpt-dry",
            status=ExecutionStatus.DRY_RUN_SUCCEEDED,
        )
        completed = run_async(store.complete_with_receipt(r1, receipt))
        assert completed.state == IdempotencyState.DRY_RUN_SUCCEEDED
        assert completed.receipt_id == "rcpt-dry"

    def test_complete_stores_receipt_for_replay(self) -> None:
        # P0-6: the original trusted receipt is retrievable via
        # get_receipt for deterministic replay.
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(
            r1, receipt_id="rcpt-replay", status=ExecutionStatus.SUCCEEDED
        )
        run_async(store.complete_with_receipt(r1, receipt))
        fetched = run_async(store.get_receipt(TENANT, _KEY_1))
        assert fetched is not None
        assert fetched.receipt_id == "rcpt-replay"

    def test_complete_fingerprint_mismatch_raises_conflict(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        tampered = _make_record(fingerprint=_FP_2)
        receipt = _make_receipt_for_record(tampered)
        with pytest.raises(IdempotencyConflictError):
            run_async(store.complete_with_receipt(tampered, receipt))

    def test_complete_unknown_key_raises_keyerror(self) -> None:
        store = InMemoryExecutionStore()
        phantom = _make_record()
        receipt = _make_receipt_for_record(phantom)
        with pytest.raises(KeyError):
            run_async(store.complete_with_receipt(phantom, receipt))

    def test_complete_from_reserved_is_illegal_transition(self) -> None:
        # P0-9: strict CAS — RESERVED → SUCCEEDED is illegal (must go
        # through CALL_STARTED first).
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        receipt = _make_receipt_for_record(r1)
        with pytest.raises(ValueError, match="illegal idempotency state transition"):
            run_async(store.complete_with_receipt(r1, receipt))

    def test_complete_receipt_tenant_mismatch_raises(self) -> None:
        # P0-9: receipt is independently verified against the record.
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(r1)
        object.__setattr__(receipt, "tenant_id", "tenant-other")
        object.__setattr__(receipt, "receipt_hash", receipt.compute_hash())
        with pytest.raises(ExecutionReceiptError, match="tenant_id"):
            run_async(store.complete_with_receipt(r1, receipt))

    def test_complete_receipt_idempotency_key_mismatch_raises(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(r1)
        object.__setattr__(receipt, "idempotency_key", "wrong-key")
        object.__setattr__(receipt, "receipt_hash", receipt.compute_hash())
        with pytest.raises(ExecutionReceiptError, match="idempotency_key"):
            run_async(store.complete_with_receipt(r1, receipt))

    def test_complete_receipt_command_id_mismatch_raises(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        receipt = _make_receipt_for_record(r1)
        object.__setattr__(receipt, "command_id", "wrong-cmd")
        object.__setattr__(receipt, "receipt_hash", receipt.compute_hash())
        with pytest.raises(ExecutionReceiptError, match="command_id"):
            run_async(store.complete_with_receipt(r1, receipt))


# ---------------------------------------------------------------------------
# InMemoryExecutionStore — mark_unknown
# ---------------------------------------------------------------------------


class TestMarkUnknown:
    def test_mark_unknown_sets_unknown_state(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        unknown = run_async(store.mark_unknown(r1, receipt_id="rcpt-unk"))
        assert unknown.state == IdempotencyState.UNKNOWN
        assert unknown.receipt_id == "rcpt-unk"

    def test_mark_unknown_without_receipt_id(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        unknown = run_async(store.mark_unknown(r1))
        assert unknown.state == IdempotencyState.UNKNOWN
        assert unknown.receipt_id is None

    def test_mark_unknown_fingerprint_mismatch_raises_conflict(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        tampered = _make_record(fingerprint=_FP_2)
        with pytest.raises(IdempotencyConflictError):
            run_async(store.mark_unknown(tampered))


# ---------------------------------------------------------------------------
# InMemoryExecutionStore — get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_returns_record_for_existing_key(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        record = run_async(store.get(TENANT, _KEY_1))
        assert record is not None
        assert record.idempotency_key == _KEY_1

    def test_get_returns_none_for_unknown_key(self) -> None:
        store = InMemoryExecutionStore()
        assert run_async(store.get(TENANT, "nonexistent")) is None

    def test_get_is_tenant_scoped(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        assert run_async(store.get("tenant-other", _KEY_1)) is None
