"""Phase 5B — Execution Receipt integrity tests.

Covers (Phase 5B Section 33):

* ``receipt_hash`` covers EVERY field (a tampered receipt is detected).
* ``verify_against_command`` binds the receipt to the producing command.
* ``verify_against_authorization`` binds the receipt to its authorisation.
* status ↔ executed invariants are enforced.
* ``started_at <= completed_at`` is enforced.
* Naive timestamps are rejected.
* Approval-required receipts MUST carry ``approval_decision_hash``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from multi_agent.action_adapter import ExecutionCommand
from multi_agent.execution_authorization import ExecutionAuthorization, ExecutionStatus
from multi_agent.execution_receipts import ActionExecutionReceipt

from phase5b_helpers import TENANT, RUN_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_TS_LATER = _TS + timedelta(seconds=5)


def _make_authorization(
    *,
    approval_required: bool = False,
    approval_decision_hash: str | None = None,
    idempotency_key: str = "idem-001",
    proposal_id: str = "prop-001",
    tenant_id: str = TENANT,
    run_id: str = RUN_ID,
) -> ExecutionAuthorization:
    return ExecutionAuthorization(
        authorization_id="auth-001",
        tenant_id=tenant_id,
        run_id=run_id,
        proposal_id=proposal_id,
        action_type="report.generate",
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        proposal_review_hash="p" * 64,
        proposal_snapshot_hash="snap" + "0" * 60,
        proposal_origin_hash="orig" + "0" * 60,
        governance_spec_hash="g" * 64,
        adapter_registry_hash="reg" + "0" * 60,
        status=ExecutionStatus.READY,
        approval_required=approval_required,
        approval_decision_hash=approval_decision_hash,
        idempotency_key=idempotency_key,
    )


def _make_command(
    *,
    command_id: str = "cmd-001",
    auth: ExecutionAuthorization | None = None,
    adapter_id: str = "noop-adapter",
    adapter_version: str = "1.0.0",
    fingerprint: str = "fp" + "0" * 62,
    attempt: int = 1,
) -> ExecutionCommand:
    a = auth or _make_authorization()
    return ExecutionCommand(
        command_id=command_id,
        authorization=a,
        proposal_snapshot_hash=a.proposal_snapshot_hash,
        proposal_origin_hash=a.proposal_origin_hash,
        action_type=a.action_type,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        execution_fingerprint=fingerprint,
        attempt=attempt,
    )


def _make_receipt(
    *,
    receipt_id: str = "rcpt-001",
    command_id: str = "cmd-001",
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    executed: bool | None = True,
    authorization: ExecutionAuthorization | None = None,
    command: ExecutionCommand | None = None,
    started_at: datetime = _TS,
    completed_at: datetime = _TS_LATER,
    approval_decision_hash: str | None = None,
    external_reference: str | None = "ext-001",
    adapter_registry_hash: str | None = None,
    idempotency_key: str | None = None,
    execution_fingerprint: str | None = None,
    attempt: int = 1,
) -> ActionExecutionReceipt:
    auth = authorization or _make_authorization()
    cmd = command or _make_command(command_id=command_id, auth=auth)
    return ActionExecutionReceipt(
        receipt_id=receipt_id,
        command_id=cmd.command_id,
        tenant_id=auth.tenant_id,
        run_id=auth.run_id,
        proposal_id=auth.proposal_id,
        authorization_hash=auth.authorization_hash,
        approval_decision_hash=approval_decision_hash,
        adapter_id=cmd.adapter_id,
        adapter_version=cmd.adapter_version,
        adapter_registry_hash=adapter_registry_hash or auth.adapter_registry_hash,
        idempotency_key=idempotency_key or auth.idempotency_key,
        execution_fingerprint=execution_fingerprint or cmd.execution_fingerprint,
        status=status,
        executed=executed,
        external_reference=external_reference,
        started_at=started_at,
        completed_at=completed_at,
        attempt=attempt,
    )


# ---------------------------------------------------------------------------
# Hash + integrity tests
# ---------------------------------------------------------------------------


class TestReceiptHashCoversEveryField:
    def test_identical_receipts_yield_same_hash(self) -> None:
        r1 = _make_receipt()
        r2 = _make_receipt()
        assert r1.receipt_hash == r2.receipt_hash

    def test_hash_is_64_char_hex(self) -> None:
        r = _make_receipt()
        assert len(r.receipt_hash) == 64
        int(r.receipt_hash, 16)

    def test_different_external_reference_changes_hash(self) -> None:
        r1 = _make_receipt(external_reference="ext-A")
        r2 = _make_receipt(external_reference="ext-B")
        assert r1.receipt_hash != r2.receipt_hash

    def test_different_status_changes_hash(self) -> None:
        r1 = _make_receipt(status=ExecutionStatus.SUCCEEDED, executed=True)
        r2 = _make_receipt(status=ExecutionStatus.FAILED, executed=False)
        assert r1.receipt_hash != r2.receipt_hash

    def test_different_executed_flag_changes_hash(self) -> None:
        # Status must match the executed invariant.
        r1 = _make_receipt(status=ExecutionStatus.SUCCEEDED, executed=True)
        # Same hash-able inputs but different executed — need a status
        # that allows both (impossible), so we just verify SUCCEEDED
        # receipt's hash differs from a FAILED receipt's hash.
        r2 = _make_receipt(status=ExecutionStatus.FAILED, executed=False)
        assert r1.receipt_hash != r2.receipt_hash

    def test_different_proposal_id_changes_hash(self) -> None:
        r1 = _make_receipt()
        auth2 = _make_authorization()
        # Build a different proposal_id authorization.
        a2_data = auth2.model_dump(mode="python")
        a2_data["proposal_id"] = "prop-002"
        a2_data["base_authorization_hash"] = ""  # recompute (R3 three-tier)
        a2_data["approval_subject_hash"] = None  # recompute
        a2_data["pre_approval_authorization_hash"] = None  # recompute
        a2_data["authorization_hash"] = ""  # recompute
        auth2 = ExecutionAuthorization.model_validate(a2_data)
        r2 = _make_receipt(authorization=auth2)
        assert r1.receipt_hash != r2.receipt_hash


class TestReceiptTamperDetection:
    def test_tampered_hash_rejected(self) -> None:
        r = _make_receipt()
        dumped = r.model_dump(mode="python")
        dumped["receipt_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ActionExecutionReceipt.model_validate(dumped)

    def test_verify_integrity_detects_tamper(self) -> None:
        r = _make_receipt()
        object.__setattr__(r, "receipt_hash", "0" * 64)
        with pytest.raises(Exception):  # ExecutionReceiptError
            r.verify_integrity()

    def test_verify_integrity_passes_for_valid(self) -> None:
        r = _make_receipt()
        r.verify_integrity()


# ---------------------------------------------------------------------------
# status ↔ executed invariants
# ---------------------------------------------------------------------------


class TestReceiptStatusExecutedInvariant:
    def test_succeeded_requires_executed_true(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(status=ExecutionStatus.SUCCEEDED, executed=False)

    def test_failed_requires_executed_false(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(status=ExecutionStatus.FAILED, executed=True)

    def test_unknown_requires_executed_none(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(status=ExecutionStatus.UNKNOWN, executed=True)

    def test_cancelled_requires_executed_none(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(status=ExecutionStatus.CANCELLED, executed=False)


# ---------------------------------------------------------------------------
# started_at <= completed_at
# ---------------------------------------------------------------------------


class TestReceiptTimeOrdering:
    def test_started_after_completed_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(
                started_at=_TS_LATER,
                completed_at=_TS,
            )

    def test_started_equals_completed_allowed(self) -> None:
        # Equal timestamps are allowed (instantaneous execution).
        r = _make_receipt(started_at=_TS, completed_at=_TS)
        assert r.started_at == r.completed_at


# ---------------------------------------------------------------------------
# Naive timestamp rejection
# ---------------------------------------------------------------------------


class TestReceiptTimezoneEnforcement:
    def test_naive_started_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(started_at=datetime(2026, 1, 1))

    def test_naive_completed_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(completed_at=datetime(2026, 1, 2))


# ---------------------------------------------------------------------------
# verify_against_command
# ---------------------------------------------------------------------------


class TestVerifyAgainstCommand:
    def _build_pair(self) -> tuple[ActionExecutionReceipt, ExecutionCommand]:
        auth = _make_authorization()
        cmd = _make_command(
            command_id="cmd-pair",
            auth=auth,
            adapter_id="adapter-A",
            adapter_version="1.0.0",
            fingerprint="fp" + "1" * 62,
        )
        receipt = _make_receipt(
            command_id="cmd-pair",
            authorization=auth,
            command=cmd,
            execution_fingerprint=cmd.execution_fingerprint,
            adapter_registry_hash=auth.adapter_registry_hash,
            idempotency_key=auth.idempotency_key,
            attempt=cmd.attempt,
        )
        return receipt, cmd

    def test_valid_pair_passes(self) -> None:
        receipt, cmd = self._build_pair()
        receipt.verify_against_command(cmd)  # no raise

    def test_command_id_mismatch_blocks(self) -> None:
        receipt, cmd = self._build_pair()
        # Forge a different command_id on the receipt.
        cmd2 = _make_command(
            command_id="cmd-different",
            auth=cmd.authorization,
            adapter_id=cmd.adapter_id,
            adapter_version=cmd.adapter_version,
            fingerprint=cmd.execution_fingerprint,
        )
        with pytest.raises(Exception, match="command_id"):
            receipt.verify_against_command(cmd2)

    def test_adapter_id_mismatch_blocks(self) -> None:
        receipt, cmd = self._build_pair()
        cmd2 = _make_command(
            command_id=cmd.command_id,
            auth=cmd.authorization,
            adapter_id="different-adapter",
            adapter_version=cmd.adapter_version,
            fingerprint=cmd.execution_fingerprint,
        )
        with pytest.raises(Exception, match="adapter_id"):
            receipt.verify_against_command(cmd2)

    def test_execution_fingerprint_mismatch_blocks(self) -> None:
        receipt, cmd = self._build_pair()
        cmd2 = _make_command(
            command_id=cmd.command_id,
            auth=cmd.authorization,
            adapter_id=cmd.adapter_id,
            adapter_version=cmd.adapter_version,
            fingerprint="fp" + "2" * 62,
        )
        with pytest.raises(Exception, match="execution_fingerprint"):
            receipt.verify_against_command(cmd2)

    def test_idempotency_key_mismatch_blocks(self) -> None:
        receipt, cmd = self._build_pair()
        # Build a different idempotency_key authorization.
        auth2 = _make_authorization(idempotency_key="idem-different")
        cmd2 = _make_command(
            command_id=cmd.command_id,
            auth=auth2,
            adapter_id=cmd.adapter_id,
            adapter_version=cmd.adapter_version,
            fingerprint=cmd.execution_fingerprint,
        )
        with pytest.raises(Exception, match="idempotency_key"):
            receipt.verify_against_command(cmd2)

    def test_attempt_mismatch_blocks(self) -> None:
        receipt, cmd = self._build_pair()
        cmd2 = _make_command(
            command_id=cmd.command_id,
            auth=cmd.authorization,
            adapter_id=cmd.adapter_id,
            adapter_version=cmd.adapter_version,
            fingerprint=cmd.execution_fingerprint,
            attempt=2,
        )
        with pytest.raises(Exception, match="attempt"):
            receipt.verify_against_command(cmd2)

    def test_authorization_hash_mismatch_blocks(self) -> None:
        receipt, cmd = self._build_pair()
        # Build a different auth (different proposal_id → naturally
        # different authorization_hash).  cmd2 carries matching
        # command_id / adapter_id / adapter_version / fingerprint so
        # every preceding check passes and the authorization_hash check
        # is the first to fail.
        auth2 = _make_authorization(proposal_id="prop-different")
        cmd2 = _make_command(
            command_id=cmd.command_id,
            auth=auth2,
            adapter_id=cmd.adapter_id,
            adapter_version=cmd.adapter_version,
            fingerprint=cmd.execution_fingerprint,
        )
        with pytest.raises(Exception, match="authorization_hash"):
            receipt.verify_against_command(cmd2)


# ---------------------------------------------------------------------------
# verify_against_authorization
# ---------------------------------------------------------------------------


class TestVerifyAgainstAuthorization:
    def test_valid_pair_passes(self) -> None:
        auth = _make_authorization()
        receipt = _make_receipt(authorization=auth)
        receipt.verify_against_authorization(auth)  # no raise

    def test_authorization_hash_mismatch_blocks(self) -> None:
        auth = _make_authorization()
        receipt = _make_receipt(authorization=auth)
        # Forge a different authorization_hash on the auth (bypass frozen).
        auth2 = _make_authorization()
        object.__setattr__(auth2, "authorization_hash", "b" * 64)
        with pytest.raises(Exception, match="authorization_hash"):
            receipt.verify_against_authorization(auth2)

    def test_tenant_mismatch_blocks(self) -> None:
        auth = _make_authorization()
        receipt = _make_receipt(authorization=auth)
        # Forge only tenant_id (keep authorization_hash intact so the
        # authorization_hash check passes and we reach the tenant_id
        # check).
        auth2 = _make_authorization()
        object.__setattr__(auth2, "tenant_id", "tenant-foreign")
        with pytest.raises(Exception, match="tenant_id"):
            receipt.verify_against_authorization(auth2)

    def test_run_id_mismatch_blocks(self) -> None:
        auth = _make_authorization()
        receipt = _make_receipt(authorization=auth)
        auth2 = _make_authorization()
        object.__setattr__(auth2, "run_id", "run-foreign")
        with pytest.raises(Exception, match="run_id"):
            receipt.verify_against_authorization(auth2)

    def test_proposal_id_mismatch_blocks(self) -> None:
        auth = _make_authorization()
        receipt = _make_receipt(authorization=auth)
        auth2 = _make_authorization()
        object.__setattr__(auth2, "proposal_id", "prop-foreign")
        with pytest.raises(Exception, match="proposal_id"):
            receipt.verify_against_authorization(auth2)

    def test_adapter_registry_hash_mismatch_blocks(self) -> None:
        auth = _make_authorization()
        receipt = _make_receipt(
            authorization=auth,
            adapter_registry_hash="reg" + "1" * 60,
        )
        with pytest.raises(Exception, match="adapter_registry_hash"):
            receipt.verify_against_authorization(auth)

    def test_idempotency_key_mismatch_blocks(self) -> None:
        auth = _make_authorization(idempotency_key="idem-A")
        receipt = _make_receipt(
            authorization=auth,
            idempotency_key="idem-B",
        )
        with pytest.raises(Exception, match="idempotency_key"):
            receipt.verify_against_authorization(auth)

    def test_approval_required_without_receipt_hash_blocks(self) -> None:
        """An approval-required authorization MUST have a matching
        ``approval_decision_hash`` on the receipt."""
        auth = _make_authorization(
            approval_required=True,
            approval_decision_hash="d" * 64,
        )
        receipt = _make_receipt(
            authorization=auth,
            approval_decision_hash=None,
        )
        with pytest.raises(Exception, match="approval_decision_hash"):
            receipt.verify_against_authorization(auth)

    def test_approval_required_with_mismatched_hash_blocks(self) -> None:
        auth = _make_authorization(
            approval_required=True,
            approval_decision_hash="d" * 64,
        )
        receipt = _make_receipt(
            authorization=auth,
            approval_decision_hash="e" * 64,
        )
        with pytest.raises(Exception, match="approval_decision_hash"):
            receipt.verify_against_authorization(auth)

    def test_approval_required_with_matching_hash_passes(self) -> None:
        auth = _make_authorization(
            approval_required=True,
            approval_decision_hash="d" * 64,
        )
        receipt = _make_receipt(
            authorization=auth,
            approval_decision_hash="d" * 64,
        )
        receipt.verify_against_authorization(auth)  # no raise


# ---------------------------------------------------------------------------
# Round-trip + frozen
# ---------------------------------------------------------------------------


class TestReceiptRoundTrip:
    def test_round_trip_preserves_hash(self) -> None:
        r1 = _make_receipt()
        dumped = r1.model_dump(mode="python")
        r2 = ActionExecutionReceipt.model_validate(dumped)
        assert r1.receipt_hash == r2.receipt_hash
        assert r1 == r2


class TestReceiptFrozen:
    def test_field_assignment_raises(self) -> None:
        r = _make_receipt()
        with pytest.raises((ValidationError, TypeError)):
            r.external_reference = "mutated"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        auth = _make_authorization()
        cmd = _make_command(auth=auth)
        with pytest.raises(ValidationError):
            ActionExecutionReceipt(
                receipt_id="r",
                command_id=cmd.command_id,
                tenant_id=auth.tenant_id,
                run_id=auth.run_id,
                proposal_id=auth.proposal_id,
                authorization_hash=auth.authorization_hash,
                adapter_id=cmd.adapter_id,
                adapter_version=cmd.adapter_version,
                adapter_registry_hash=auth.adapter_registry_hash,
                idempotency_key=auth.idempotency_key,
                execution_fingerprint=cmd.execution_fingerprint,
                status=ExecutionStatus.SUCCEEDED,
                executed=True,
                started_at=_TS,
                completed_at=_TS_LATER,
                extra="x",  # type: ignore[call-arg]
            )


class TestReceiptBlankValidation:
    @pytest.mark.parametrize(
        "field",
        [
            "receipt_id",
            "command_id",
            "tenant_id",
            "run_id",
            "proposal_id",
            "authorization_hash",
            "adapter_id",
            "adapter_version",
            "adapter_registry_hash",
            "execution_fingerprint",
        ],
    )
    def test_blank_identity_field_rejected(self, field: str) -> None:
        auth = _make_authorization()
        cmd = _make_command(auth=auth)
        kwargs = dict(
            receipt_id="r",
            command_id=cmd.command_id,
            tenant_id=auth.tenant_id,
            run_id=auth.run_id,
            proposal_id=auth.proposal_id,
            authorization_hash=auth.authorization_hash,
            adapter_id=cmd.adapter_id,
            adapter_version=cmd.adapter_version,
            adapter_registry_hash=auth.adapter_registry_hash,
            idempotency_key=auth.idempotency_key,
            execution_fingerprint=cmd.execution_fingerprint,
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            started_at=_TS,
            completed_at=_TS_LATER,
        )
        kwargs[field] = "   "
        with pytest.raises(ValidationError):
            ActionExecutionReceipt(**kwargs)

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_receipt(attempt=0)
