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
* An IN_PROGRESS key blocks a second reservation
  (``reserve_in_progress_raises_already_in_progress``).
* An UNKNOWN outcome is returned as-is — NEVER auto-retried
  (``reserve_unknown_returns_record_without_retry``).
* A FAILED key + the same fingerprint returns the FAILED record so the
  caller may retry explicitly
  (``reserve_failed_same_fingerprint_returns_failed``).
* ``mark_started`` transitions RESERVED → IN_PROGRESS
  (``mark_started_transitions_to_in_progress``).
* ``complete`` with ``succeeded=True`` → SUCCEEDED, ``succeeded=False``
  → FAILED (``complete_succeeded_sets_succeeded_state``).
* ``mark_unknown`` sets the UNKNOWN terminal state
  (``mark_unknown_sets_unknown_state``).
* :class:`IdempotencyRecord` is frozen and hash-stable — a tampered
  ``record_hash`` is detected at construction and via
  :meth:`verify_integrity`
  (``tampered_record_hash_detected_at_construction``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from multi_agent.execution_error_codes import (
    ExecutionAlreadyInProgressError,
    IdempotencyConflictError,
)
from multi_agent.execution_store import (
    IdempotencyRecord,
    IdempotencyState,
    InMemoryExecutionStore,
)

from phase5b_helpers import TENANT, run_async


_FP_1 = "fp-aaa" + "0" * 59
_FP_2 = "fp-bbb" + "0" * 59
_CMD_1 = "cmd-001"
_CMD_2 = "cmd-002"
_KEY_1 = "idem-001"


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


# ---------------------------------------------------------------------------
# IdempotencyState enum
# ---------------------------------------------------------------------------


class TestIdempotencyState:
    def test_states_have_distinct_values(self) -> None:
        values = {s.value for s in IdempotencyState}
        assert values == {
            "reserved",
            "in_progress",
            "succeeded",
            "failed",
            "unknown",
        }


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
        with pytest.raises(Exception):
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
        run_async(store.complete(r1, "rcpt-001", succeeded=True))
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
        run_async(store.complete(r1, "rcpt-fail", succeeded=False))
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
    def test_mark_started_transitions_to_in_progress(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        updated = run_async(store.mark_started(r1, _CMD_1))
        assert updated.state == IdempotencyState.IN_PROGRESS
        assert updated.command_id == _CMD_1

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
# InMemoryExecutionStore — complete
# ---------------------------------------------------------------------------


class TestComplete:
    def test_complete_succeeded_sets_succeeded_state(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        completed = run_async(store.complete(r1, "rcpt-001", succeeded=True))
        assert completed.state == IdempotencyState.SUCCEEDED
        assert completed.receipt_id == "rcpt-001"

    def test_complete_failed_sets_failed_state(self) -> None:
        store = InMemoryExecutionStore()
        r1 = run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        run_async(store.mark_started(r1, _CMD_1))
        completed = run_async(store.complete(r1, "rcpt-fail", succeeded=False))
        assert completed.state == IdempotencyState.FAILED
        assert completed.receipt_id == "rcpt-fail"

    def test_complete_fingerprint_mismatch_raises_conflict(self) -> None:
        store = InMemoryExecutionStore()
        run_async(store.reserve(TENANT, _KEY_1, _FP_1, _CMD_1))
        tampered = _make_record(fingerprint=_FP_2)
        with pytest.raises(IdempotencyConflictError):
            run_async(store.complete(tampered, "rcpt-001", succeeded=True))

    def test_complete_unknown_key_raises_keyerror(self) -> None:
        store = InMemoryExecutionStore()
        phantom = _make_record()
        with pytest.raises(KeyError):
            run_async(store.complete(phantom, "rcpt-001", succeeded=True))


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
