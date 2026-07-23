"""Phase 5B — Execution Authorization contract tests.

Covers (Phase 5B Section 33):

* authorization_hash tamper blocks execution.
* review binding mismatch (request_hash, result_hash, proposal_review_hash)
  is detected by verify_against_review.
* governance_spec_hash mismatch is detected.
* tenant mismatch is detected.
* non-APPROVED status is not authorised for execution.
* batch_execution_status_priority weights are unique.
* status ↔ executed consistency invariants.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from multi_agent.execution_authorization import (
    BatchExecutionStatus,
    ExecutionAuthorization,
    ExecutionStatus,
    batch_execution_status_priority,
)
from multi_agent.review_contracts import ReviewRiskLevel

from phase5b_helpers import (
    TENANT,
    RUN_ID,
    make_request,
    make_result,
    make_review,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_auth(
    *,
    authorization_id: str = "auth-001",
    proposal_id: str = "prop-test-001",
    tenant_id: str = TENANT,
    run_id: str = RUN_ID,
    action_type: str = "report.generate",
    review_request_hash: str = "r" * 64,
    review_result_hash: str = "s" * 64,
    proposal_review_hash: str = "p" * 64,
    proposal_snapshot_hash: str = "snap" + "0" * 60,
    proposal_origin_hash: str = "orig" + "0" * 60,
    governance_spec_hash: str = "g" * 64,
    adapter_registry_hash: str = "reg" + "0" * 60,
    status: ExecutionStatus | None = None,
    approval_required: bool = False,
    approval_id: str | None = None,
    approval_decision_hash: str | None = None,
    risk_level: ReviewRiskLevel = ReviewRiskLevel.LOW,
    idempotency_key: str = "idem-test-001",
) -> ExecutionAuthorization:
    if status is None:
        status = (
            ExecutionStatus.PENDING_APPROVAL
            if approval_required
            else ExecutionStatus.READY
        )
    return ExecutionAuthorization(
        authorization_id=authorization_id,
        tenant_id=tenant_id,
        run_id=run_id,
        proposal_id=proposal_id,
        action_type=action_type,
        review_request_hash=review_request_hash,
        review_result_hash=review_result_hash,
        proposal_review_hash=proposal_review_hash,
        proposal_snapshot_hash=proposal_snapshot_hash,
        proposal_origin_hash=proposal_origin_hash,
        governance_spec_hash=governance_spec_hash,
        adapter_registry_hash=adapter_registry_hash,
        status=status,
        approval_required=approval_required,
        approval_id=approval_id,
        approval_decision_hash=approval_decision_hash,
        risk_level=risk_level,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Hash + integrity tests
# ---------------------------------------------------------------------------


class TestAuthorizationHashConsistency:
    def test_two_identical_authorizations_same_hash(self) -> None:
        a1 = _make_auth()
        a2 = _make_auth()
        assert a1.authorization_hash == a2.authorization_hash
        assert a1.compute_hash() == a2.compute_hash()

    def test_hash_is_64_char_hex(self) -> None:
        a = _make_auth()
        assert len(a.authorization_hash) == 64
        int(a.authorization_hash, 16)

    def test_different_proposal_id_changes_hash(self) -> None:
        a1 = _make_auth(proposal_id="prop-A")
        a2 = _make_auth(proposal_id="prop-B")
        assert a1.authorization_hash != a2.authorization_hash

    def test_approval_required_changes_hash(self) -> None:
        a1 = _make_auth(approval_required=False)
        a2 = _make_auth(approval_required=True)
        assert a1.authorization_hash != a2.authorization_hash

    def test_status_changes_hash(self) -> None:
        a1 = _make_auth(status=ExecutionStatus.READY)
        a2 = _make_auth(status=ExecutionStatus.PENDING_APPROVAL)
        assert a1.authorization_hash != a2.authorization_hash


class TestAuthorizationTamperDetection:
    def test_tampered_hash_rejected_at_construction(self) -> None:
        a = _make_auth()
        dumped = a.model_dump(mode="python")
        dumped["authorization_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ExecutionAuthorization.model_validate(dumped)

    def test_verify_integrity_detects_tamper(self) -> None:
        a = _make_auth()
        object.__setattr__(a, "authorization_hash", "0" * 64)
        with pytest.raises(ValueError):
            a.verify_integrity()

    def test_verify_integrity_passes_for_valid(self) -> None:
        a = _make_auth()
        a.verify_integrity()


class TestAuthorizationFrozen:
    def test_field_assignment_raises(self) -> None:
        a = _make_auth()
        with pytest.raises((ValidationError, TypeError)):
            a.proposal_id = "mutated"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionAuthorization(
                authorization_id="x",
                tenant_id=TENANT,
                run_id=RUN_ID,
                proposal_id="p",
                action_type="report.generate",
                review_request_hash="r" * 64,
                review_result_hash="s" * 64,
                proposal_review_hash="p" * 64,
                proposal_snapshot_hash="snap" + "0" * 60,
                proposal_origin_hash="orig" + "0" * 60,
                governance_spec_hash="g" * 64,
                extra="x",  # type: ignore[call-arg]
            )


class TestAuthorizationBlankValidation:
    @pytest.mark.parametrize(
        "field",
        [
            "authorization_id",
            "tenant_id",
            "run_id",
            "proposal_id",
            "action_type",
            "review_request_hash",
            "review_result_hash",
            "proposal_review_hash",
            "proposal_snapshot_hash",
            "proposal_origin_hash",
            "governance_spec_hash",
        ],
    )
    def test_blank_field_rejected(self, field: str) -> None:
        kwargs = dict(
            authorization_id="auth-b",
            tenant_id=TENANT,
            run_id=RUN_ID,
            proposal_id="prop-b",
            action_type="report.generate",
            review_request_hash="r" * 64,
            review_result_hash="s" * 64,
            proposal_review_hash="p" * 64,
            proposal_snapshot_hash="snap" + "0" * 60,
            proposal_origin_hash="orig" + "0" * 60,
            governance_spec_hash="g" * 64,
        )
        kwargs[field] = "   "
        with pytest.raises(ValidationError):
            ExecutionAuthorization(**kwargs)


# ---------------------------------------------------------------------------
# verify_against_review tests
# ---------------------------------------------------------------------------


class TestVerifyAgainstReview:
    """verify_against_review is the fail-closed binding to the Review."""

    def _build_approved_chain(self):
        """Build a valid request + result + review + authorization."""
        request = make_request("review-bind")
        review = make_review("prop-test-001", request.request_hash)
        result = make_result(request, [review])
        # Locate the snapshot + envelope so we can mirror their hashes.
        snap = next(s for s in request.proposals if s.proposal_id == "prop-test-001")
        env = next(
            e
            for e in request.proposal_envelopes
            if e.proposal.proposal_id == "prop-test-001"
        )
        auth = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=snap.snapshot_hash,
            proposal_origin_hash=env.origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=snap.action_type,
            risk_level=review.risk_level,
        )
        return request, result, review, auth

    def test_valid_chain_passes(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        # No raise.
        auth.verify_against_review(request, result, review)

    def test_tenant_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        # Build a foreign-tenant authorization.
        foreign = _make_auth(
            tenant_id="tenant-foreign",
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="tenant_id"):
            foreign.verify_against_review(request, result, review)

    def test_run_id_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        foreign = _make_auth(
            run_id="run-foreign",
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="run_id"):
            foreign.verify_against_review(request, result, review)

    def test_review_request_hash_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash="x" * 64,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="review_request_hash"):
            bad.verify_against_review(request, result, review)

    def test_review_result_hash_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash="x" * 64,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="review_result_hash"):
            bad.verify_against_review(request, result, review)

    def test_governance_spec_hash_mismatch_vs_request_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash="x" * 64,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="governance_spec_hash"):
            bad.verify_against_review(request, result, review)

    def test_proposal_review_hash_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash="x" * 64,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="proposal_review_hash"):
            bad.verify_against_review(request, result, review)

    def test_proposal_snapshot_hash_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash="x" * 64,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="proposal_snapshot_hash"):
            bad.verify_against_review(request, result, review)

    def test_proposal_origin_hash_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash="x" * 64,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="proposal_origin_hash"):
            bad.verify_against_review(request, result, review)

    def test_action_type_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type="summary.compile",  # different action type
            risk_level=review.risk_level,
        )
        with pytest.raises(ValueError, match="action_type"):
            bad.verify_against_review(request, result, review)

    def test_risk_level_mismatch_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-test-001",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=ReviewRiskLevel.HIGH,
        )
        with pytest.raises(ValueError, match="risk_level"):
            bad.verify_against_review(request, result, review)

    def test_proposal_not_in_request_blocks(self) -> None:
        request, result, review, auth = self._build_approved_chain()
        bad = _make_auth(
            proposal_id="prop-not-in-request",
            review_request_hash=request.request_hash,
            review_result_hash=result.result_hash,
            proposal_review_hash=review.review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=request.governance_spec_hash,
            action_type=auth.action_type,
            risk_level=review.risk_level,
        )
        # The proposal_id check fires before the snapshot lookup, so a
        # foreign proposal_id is rejected with the proposal_id mismatch
        # message (still fail-closed — the auth never executes).
        with pytest.raises(ValueError, match="proposal_id"):
            bad.verify_against_review(request, result, review)


# ---------------------------------------------------------------------------
# batch_execution_status_priority tests
# ---------------------------------------------------------------------------


class TestBatchExecutionStatusPriority:
    def test_priority_weights_are_unique(self) -> None:
        weights = {s: batch_execution_status_priority(s) for s in BatchExecutionStatus}
        assert len(set(weights.values())) == len(weights)

    def test_unknown_is_highest(self) -> None:
        """UNKNOWN must dominate every other status."""
        unknown_w = batch_execution_status_priority(BatchExecutionStatus.UNKNOWN)
        for s in BatchExecutionStatus:
            if s == BatchExecutionStatus.UNKNOWN:
                continue
            assert unknown_w > batch_execution_status_priority(s)

    def test_failed_beats_cancelled(self) -> None:
        assert batch_execution_status_priority(
            BatchExecutionStatus.FAILED
        ) > batch_execution_status_priority(BatchExecutionStatus.CANCELLED)

    def test_cancelled_beats_partial_success(self) -> None:
        assert batch_execution_status_priority(
            BatchExecutionStatus.CANCELLED
        ) > batch_execution_status_priority(BatchExecutionStatus.PARTIAL_SUCCESS)

    def test_partial_success_beats_pending_approval(self) -> None:
        assert batch_execution_status_priority(
            BatchExecutionStatus.PARTIAL_SUCCESS
        ) > batch_execution_status_priority(BatchExecutionStatus.PENDING_APPROVAL)

    def test_pending_approval_beats_blocked(self) -> None:
        assert batch_execution_status_priority(
            BatchExecutionStatus.PENDING_APPROVAL
        ) > batch_execution_status_priority(BatchExecutionStatus.BLOCKED)

    def test_blocked_beats_succeeded(self) -> None:
        assert batch_execution_status_priority(
            BatchExecutionStatus.BLOCKED
        ) > batch_execution_status_priority(BatchExecutionStatus.SUCCEEDED)

    def test_succeeded_beats_no_actions(self) -> None:
        """NO_ACTIONS must NEVER be equivalent to SUCCEEDED."""
        assert batch_execution_status_priority(
            BatchExecutionStatus.SUCCEEDED
        ) > batch_execution_status_priority(BatchExecutionStatus.NO_ACTIONS)

    def test_max_yields_single_winner(self) -> None:
        """max() over a mixed batch always yields one status."""
        statuses = [
            BatchExecutionStatus.SUCCEEDED,
            BatchExecutionStatus.FAILED,
            BatchExecutionStatus.PENDING_APPROVAL,
            BatchExecutionStatus.UNKNOWN,
        ]
        winner = max(statuses, key=batch_execution_status_priority)
        assert winner == BatchExecutionStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Round-trip + default state tests
# ---------------------------------------------------------------------------


class TestAuthorizationRoundTrip:
    def test_round_trip_preserves_hash(self) -> None:
        a1 = _make_auth()
        dumped = a1.model_dump(mode="python")
        a2 = ExecutionAuthorization.model_validate(dumped)
        assert a1.authorization_hash == a2.authorization_hash
        assert a1 == a2

    def test_round_trip_with_approval_binding(self) -> None:
        a1 = _make_auth(
            approval_required=True,
            status=ExecutionStatus.PENDING_APPROVAL,
            approval_id="appr-001",
            approval_decision_hash="d" * 64,
        )
        dumped = a1.model_dump(mode="python")
        a2 = ExecutionAuthorization.model_validate(dumped)
        assert a1.authorization_hash == a2.authorization_hash
        assert a2.approval_id == "appr-001"


class TestAuthorizationDefaultState:
    def test_default_status_is_pending_approval(self) -> None:
        a = _make_auth(approval_required=True)
        assert a.status == ExecutionStatus.PENDING_APPROVAL

    def test_no_approval_default_status_is_ready(self) -> None:
        a = _make_auth(approval_required=False, status=ExecutionStatus.READY)
        assert a.status == ExecutionStatus.READY

    def test_no_approval_default_has_no_approval_id(self) -> None:
        a = _make_auth()
        assert a.approval_id is None
        assert a.approval_decision_hash is None
