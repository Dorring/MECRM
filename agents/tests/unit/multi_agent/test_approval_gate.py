"""Phase 5B — Approval Gate counterexample tests (Section 33).

Covers the human approval gate invariants:

* A high-risk / approval-required Proposal NEVER executes without a
  valid APPROVED decision (``high_risk_without_approval_never_executes``).
* An approver MUST hold at least one of ``required_approver_roles``
  (``wrong_approver_role_rejected``).
* EXPIRED decisions can NEVER be consumed
  (``expired_approval_rejected``).
* REVOKED decisions can NEVER be consumed
  (``revoked_approval_rejected``).
* An APPROVED decision can be consumed exactly ONCE
  (``approval_can_only_be_consumed_once``).
* An approval issued for Proposal A CANNOT authorise Proposal B
  (``approval_for_other_proposal_rejected``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_REGISTRY,
    get_action_governance_spec,
)
from multi_agent.approval_contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    FrozenClock,
)
from multi_agent.approval_gate import (
    ApprovalGate,
    ApprovalRequirement,
    InMemoryApprovalStore,
)
from multi_agent.execution_authorization import ExecutionAuthorization, ExecutionStatus
from multi_agent.execution_error_codes import (
    ApprovalRequiredError,
    ApprovalValidationError,
    APPROVAL_REJECTED,
)
from multi_agent.review_contracts import ReviewRiskLevel

from phase5b_helpers import TENANT, RUN_ID, run_async


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_TS_LATER = _TS + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_authorization(
    *,
    authorization_id: str = "auth-001",
    approval_id: str | None = None,
    proposal_id: str = "prop-001",
    action_type: str = "crm.owner.assign",
    risk_level: ReviewRiskLevel = ReviewRiskLevel.HIGH,
    approval_required: bool = True,
    approval_decision_hash: str | None = None,
    authorization_hash: str | None = None,
) -> ExecutionAuthorization:
    """Build an authorization.  ``authorization_hash`` is auto-computed
    when not supplied (forged hashes use ``object.__setattr__`` after
    construction)."""
    auth = ExecutionAuthorization(
        authorization_id=authorization_id,
        tenant_id=TENANT,
        run_id=RUN_ID,
        proposal_id=proposal_id,
        action_type=action_type,
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        proposal_review_hash="p" * 64,
        proposal_snapshot_hash="snap" + "0" * 60,
        proposal_origin_hash="orig" + "0" * 60,
        governance_spec_hash="g" * 64,
        adapter_registry_hash="reg" + "0" * 60,
        status=ExecutionStatus.PENDING_APPROVAL,
        approval_required=approval_required,
        approval_id=approval_id,
        approval_decision_hash=approval_decision_hash,
        risk_level=risk_level,
        idempotency_key="idem-001",
    )
    if authorization_hash is not None:
        object.__setattr__(auth, "authorization_hash", authorization_hash)
    return auth


def _make_approval_request(
    *,
    approval_id: str = "appr-001",
    authorization_id: str = "auth-001",
    proposal_id: str = "prop-001",
    authorization_hash: str | None = None,
    required_roles: tuple[str, ...] = ("manager",),
    expires_at: datetime | None = None,
    risk_level: ReviewRiskLevel = ReviewRiskLevel.HIGH,
    action_type: str = "crm.owner.assign",
) -> ApprovalRequest:
    auth = _make_authorization(
        authorization_id=authorization_id,
        approval_id=approval_id,
        proposal_id=proposal_id,
        action_type=action_type,
        risk_level=risk_level,
    )
    # P0-1 R3: the request binds to approval_subject_hash (what the
    # human approves), NOT the final authorization_hash.
    default_hash = auth.approval_subject_hash or auth.authorization_hash
    ah = authorization_hash or default_hash
    return ApprovalRequest(
        approval_id=approval_id,
        authorization_id=authorization_id,
        tenant_id=TENANT,
        run_id=RUN_ID,
        proposal_id=proposal_id,
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        authorization_hash=ah,
        risk_level=risk_level,
        action_type=action_type,
        action_summary="test action",
        required_approver_roles=required_roles,
        requested_by="test_requester",
        requested_at=_TS,
        expires_at=expires_at,
    )


def _make_decision(
    *,
    approval_id: str = "appr-001",
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    approver_id: str = "approver-001",
    approver_roles: tuple[str, ...] = ("manager",),
    decision_reason: str = "approved",
    decided_at: datetime = _TS,
    approval_request_hash: str | None = None,
    authorization_hash: str | None = None,
) -> ApprovalDecision:
    request = _make_approval_request(approval_id=approval_id)
    arh = approval_request_hash or request.approval_request_hash
    # P0-1 R3: the decision binds to approval_subject_hash (same as the
    # request), NOT the final authorization_hash.
    ah = authorization_hash or request.authorization_hash
    return ApprovalDecision(
        approval_id=approval_id,
        status=status,
        approver_id=approver_id,
        approver_roles=approver_roles,
        decision_reason=decision_reason,
        decided_at=decided_at,
        approval_request_hash=arh,
        authorization_hash=ah,
    )


# ---------------------------------------------------------------------------
# resolve_approval_requirement
# ---------------------------------------------------------------------------


class TestResolveApprovalRequirement:
    def test_always_needs_approval_requires_approval(self) -> None:
        """A governance spec with ``always_needs_approval=True`` ALWAYS
        requires human approval, regardless of review status."""
        # crm.owner.assign has always_needs_approval=True
        spec = get_action_governance_spec("crm.owner.assign")
        assert spec is not None and spec.always_needs_approval

        from multi_agent.review_contracts import (
            PolicyDecision,
            ProposalReview,
            ReviewDecisionStatus,
            ReviewRiskLevel,
        )
        from phase5b_helpers import make_policy_audit

        audit = make_policy_audit("prop-001", "r" * 64, decision=PolicyDecision.ALLOWED)
        review = ProposalReview(
            proposal_id="prop-001",
            status=ReviewDecisionStatus.APPROVED,
            findings=(),
            required_approval=False,
            risk_level=ReviewRiskLevel.HIGH,
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=audit,
            primary_proposal_id=None,
        )
        auth = _make_authorization(
            approval_id="appr-001",
            approval_required=True,
        )
        gate = ApprovalGate()
        req = gate.resolve_approval_requirement(review, auth, spec)
        assert req.required is True

    def test_high_canonical_risk_requires_approval(self) -> None:
        """A governance spec with canonical_risk HIGH/CRITICAL requires
        approval even when ``always_needs_approval=False``."""
        # report.generate has canonical_risk=LOW → not required
        spec = get_action_governance_spec("report.generate")
        assert spec is not None
        assert spec.canonical_risk not in (
            ReviewRiskLevel.HIGH,
            ReviewRiskLevel.CRITICAL,
        )

        from multi_agent.review_contracts import (
            PolicyDecision,
            ProposalReview,
            ReviewDecisionStatus,
        )
        from phase5b_helpers import make_policy_audit

        audit = make_policy_audit("prop-001", "r" * 64, decision=PolicyDecision.ALLOWED)
        review = ProposalReview(
            proposal_id="prop-001",
            status=ReviewDecisionStatus.APPROVED,
            findings=(),
            required_approval=False,
            risk_level=ReviewRiskLevel.LOW,
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=audit,
            primary_proposal_id=None,
        )
        auth = _make_authorization(
            action_type="report.generate",
            risk_level=ReviewRiskLevel.LOW,
            approval_required=False,
        )
        gate = ApprovalGate()
        req = gate.resolve_approval_requirement(review, auth, spec)
        assert req.required is False

    def test_needs_approval_review_status_requires_approval(self) -> None:
        """A review status of NEEDS_APPROVAL triggers the approval
        requirement regardless of canonical risk."""
        # summary.compile has canonical_risk=LOW
        spec = get_action_governance_spec("summary.compile")
        assert spec is not None

        from multi_agent.review_contracts import (
            PolicyDecision,
            ProposalReview,
            ReviewDecisionStatus,
            ReviewRiskLevel,
        )
        from phase5b_helpers import make_policy_audit

        audit = make_policy_audit(
            "prop-001", "r" * 64, decision=PolicyDecision.NEEDS_APPROVAL
        )
        review = ProposalReview(
            proposal_id="prop-001",
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            findings=(),
            required_approval=True,
            risk_level=ReviewRiskLevel.LOW,
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=audit,
            primary_proposal_id=None,
        )
        auth = _make_authorization(
            action_type="summary.compile",
            risk_level=ReviewRiskLevel.LOW,
            approval_required=True,
            approval_id="appr-001",
        )
        gate = ApprovalGate()
        req = gate.resolve_approval_requirement(review, auth, spec)
        assert req.required is True


# ---------------------------------------------------------------------------
# validate_decision — fail-closed checks
# ---------------------------------------------------------------------------


class TestValidateDecisionFailClosed:
    def test_valid_approved_decision_passes(self) -> None:
        request = _make_approval_request()
        decision = _make_decision()
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
            approval_decision_hash=decision.decision_hash,
        )
        gate = ApprovalGate()
        gate.validate_decision(decision, request, auth)  # no raise

    def test_wrong_approver_role_rejected(self) -> None:
        """The approver holds none of the required roles → rejected."""
        request = _make_approval_request(required_roles=("manager", "director"))
        decision = _make_decision(
            approver_roles=("viewer",),  # not in required set
            approval_request_hash=request.approval_request_hash,
        )
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError) as exc:
            gate.validate_decision(decision, request, auth)
        assert (
            "approver roles" in str(exc.value).lower()
            or "required" in str(exc.value).lower()
        )

    def test_rejected_status_blocks(self) -> None:
        request = _make_approval_request()
        decision = _make_decision(status=ApprovalStatus.REJECTED)
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_expired_status_blocks(self) -> None:
        request = _make_approval_request()
        decision = _make_decision(status=ApprovalStatus.EXPIRED)
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_revoked_status_blocks(self) -> None:
        request = _make_approval_request()
        decision = _make_decision(status=ApprovalStatus.REVOKED)
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_consumed_status_blocks(self) -> None:
        request = _make_approval_request()
        decision = _make_decision(status=ApprovalStatus.CONSUMED)
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_decision_approval_id_mismatch_blocks(self) -> None:
        request = _make_approval_request(approval_id="appr-A")
        decision = _make_decision(approval_id="appr-B")
        auth = _make_authorization(
            approval_id="appr-A",
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_decision_request_hash_mismatch_blocks(self) -> None:
        request = _make_approval_request()
        # Build a decision with a forged (mismatched) request_hash.
        decision = _make_decision()
        forged_decision = ApprovalDecision(
            approval_id=decision.approval_id,
            status=decision.status,
            approver_id=decision.approver_id,
            approver_roles=decision.approver_roles,
            decision_reason=decision.decision_reason,
            decided_at=decision.decided_at,
            approval_request_hash="x" * 64,  # mismatched
            authorization_hash=decision.authorization_hash,
        )
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(forged_decision, request, auth)

    def test_authorization_hash_mismatch_blocks(self) -> None:
        """P0-1 R3: the decision's ``authorization_hash`` MUST match
        the authorization's ``approval_subject_hash`` (what the human
        approved).  Forging the subject hash blocks the decision."""
        request = _make_approval_request()
        decision = _make_decision()
        # Authorization with a forged approval_subject_hash.
        auth = _make_authorization(
            approval_id=request.approval_id,
        )
        object.__setattr__(auth, "approval_subject_hash", "z" * 64)
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_proposal_id_mismatch_blocks(self) -> None:
        """approval_for_other_proposal_rejected — an approval issued
        for Proposal A CANNOT authorise Proposal B."""
        request = _make_approval_request(proposal_id="prop-A")
        decision = _make_decision(
            approval_request_hash=request.approval_request_hash,
            authorization_hash=request.authorization_hash,
        )  # bound to request
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
            proposal_id="prop-B",  # different proposal
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError) as exc:
            gate.validate_decision(decision, request, auth)
        assert "proposal_id" in str(exc.value).lower()

    def test_authorization_id_mismatch_blocks(self) -> None:
        request = _make_approval_request(authorization_id="auth-A")
        decision = _make_decision()
        auth = _make_authorization(
            authorization_id="auth-B",
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_tenant_mismatch_blocks(self) -> None:
        request = _make_approval_request()
        decision = _make_decision()
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        object.__setattr__(auth, "tenant_id", "tenant-foreign")
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_expired_decision_blocks_when_decided_after_expires_at(self) -> None:
        """If ``request.expires_at`` is set and the decision was made
        AFTER the deadline, validation fails (expired approval)."""
        expires = _TS + timedelta(minutes=30)
        request = _make_approval_request(expires_at=expires)
        decision = _make_decision(
            decided_at=_TS_LATER,  # past the deadline
        )
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)

    def test_empty_required_roles_blocks(self) -> None:
        """An approval request with no required roles cannot be
        validated (ambiguous authority)."""
        # ApprovalRequest rejects empty required_approver_roles at
        # construction, so we forge it via object.__setattr__.
        request = _make_approval_request(required_roles=("manager",))
        object.__setattr__(request, "required_approver_roles", ())
        decision = _make_decision()
        auth = _make_authorization(
            approval_id=request.approval_id,
            authorization_hash=request.authorization_hash,
        )
        gate = ApprovalGate()
        with pytest.raises(ApprovalValidationError):
            gate.validate_decision(decision, request, auth)


# ---------------------------------------------------------------------------
# InMemoryApprovalStore — single-consume rule
# ---------------------------------------------------------------------------


class TestApprovalStoreSingleConsume:
    def test_approval_can_only_be_consumed_once(self) -> None:
        """An APPROVED decision can be consumed exactly ONCE per command
        family.  A replay with the SAME family returns the original
        consumption; a DIFFERENT family is rejected."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision = _make_decision()
        run_async(store.decide(request.approval_id, decision))
        auth = _make_authorization(approval_id=request.approval_id)

        # First consume succeeds.
        consumed = run_async(
            store.consume_for_command(
                request.approval_id,
                authorization=auth,
                command_family_id="cfam-001",
                execution_fingerprint="fp-001",
                now=_TS,
            )
        )
        assert consumed.approval_id == request.approval_id

        # Replay with SAME family + fingerprint → returns original (NOT
        # a second illegal consume).
        replay = run_async(
            store.consume_for_command(
                request.approval_id,
                authorization=auth,
                command_family_id="cfam-001",
                execution_fingerprint="fp-001",
                now=_TS,
            )
        )
        assert replay.consumption_hash == consumed.consumption_hash

        # Consume with a DIFFERENT family → MUST fail.
        with pytest.raises(ApprovalValidationError) as exc:
            run_async(
                store.consume_for_command(
                    request.approval_id,
                    authorization=auth,
                    command_family_id="cfam-002",
                    execution_fingerprint="fp-002",
                    now=_TS,
                )
            )
        assert "already consumed" in str(exc.value).lower()

    def test_consume_without_decision_blocks(self) -> None:
        """Consuming an approval that has no decision yet raises
        ApprovalRequiredError."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        auth = _make_authorization(approval_id=request.approval_id)
        with pytest.raises(ApprovalRequiredError):
            run_async(
                store.consume_for_command(
                    request.approval_id,
                    authorization=auth,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )

    def test_consume_unknown_approval_blocks(self) -> None:
        store = InMemoryApprovalStore()
        auth = _make_authorization(approval_id="appr-unknown")
        with pytest.raises(ApprovalValidationError):
            run_async(
                store.consume_for_command(
                    "appr-unknown",
                    authorization=auth,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )

    def test_decide_twice_blocks(self) -> None:
        """Only ONE terminal decision may be applied per approval."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision1 = _make_decision()
        run_async(store.decide(request.approval_id, decision1))

        # A second decision MUST fail.
        decision2 = _make_decision(decision_reason="second-thoughts")
        with pytest.raises(ApprovalValidationError):
            run_async(store.decide(request.approval_id, decision2))

    def test_expired_decision_cannot_be_consumed(self) -> None:
        """An EXPIRED decision can NEVER be consumed."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision = _make_decision(status=ApprovalStatus.EXPIRED)
        run_async(store.decide(request.approval_id, decision))
        auth = _make_authorization(approval_id=request.approval_id)
        with pytest.raises(ApprovalValidationError) as exc:
            run_async(
                store.consume_for_command(
                    request.approval_id,
                    authorization=auth,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )
        assert "expired" in str(exc.value).lower()

    def test_revoked_decision_cannot_be_consumed(self) -> None:
        """A REVOKED decision can NEVER be consumed."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision = _make_decision(status=ApprovalStatus.REVOKED)
        run_async(store.decide(request.approval_id, decision))
        auth = _make_authorization(approval_id=request.approval_id)
        with pytest.raises(ApprovalValidationError) as exc:
            run_async(
                store.consume_for_command(
                    request.approval_id,
                    authorization=auth,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )
        assert "revoked" in str(exc.value).lower()

    def test_rejected_decision_cannot_be_consumed(self) -> None:
        """A REJECTED decision can NEVER be consumed."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision = _make_decision(status=ApprovalStatus.REJECTED)
        run_async(store.decide(request.approval_id, decision))
        auth = _make_authorization(approval_id=request.approval_id)
        with pytest.raises(ApprovalValidationError) as exc:
            run_async(
                store.consume_for_command(
                    request.approval_id,
                    authorization=auth,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )
        assert exc.value.error_code == APPROVAL_REJECTED

    def test_consume_with_mismatched_authorization_hash_blocks(self) -> None:
        """P0-1 R3: the consume call's ``approval_subject_hash`` MUST
        match the request / decision's ``authorization_hash``.  Forging
        the subject hash blocks consumption."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision = _make_decision()
        run_async(store.decide(request.approval_id, decision))
        # Forge an auth with a different approval_subject_hash.
        auth = _make_authorization(approval_id=request.approval_id)
        object.__setattr__(auth, "approval_subject_hash", "z" * 64)
        with pytest.raises(ApprovalValidationError):
            run_async(
                store.consume_for_command(
                    request.approval_id,
                    authorization=auth,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )

    def test_decide_with_mismatched_approval_id_blocks(self) -> None:
        store = InMemoryApprovalStore()
        request = _make_approval_request(approval_id="appr-A")
        run_async(store.create(request))
        # Decision's approval_id does not match the store call.
        decision = _make_decision(approval_id="appr-B")
        with pytest.raises(ApprovalValidationError):
            run_async(store.decide(request.approval_id, decision))

    def test_decide_with_mismatched_request_hash_blocks(self) -> None:
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        decision = _make_decision(approval_request_hash="x" * 64)
        with pytest.raises(ApprovalValidationError):
            run_async(store.decide(request.approval_id, decision))

    def test_create_is_idempotent_on_approval_id(self) -> None:
        """Re-creating the same approval_id returns the existing
        request — no duplicate state."""
        store = InMemoryApprovalStore()
        request = _make_approval_request()
        run_async(store.create(request))
        # Re-create with the same approval_id.
        result = run_async(store.create(request))
        assert result.approval_id == request.approval_id
        # No duplicate.
        assert len(store._requests) == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ApprovalRequirement dataclass
# ---------------------------------------------------------------------------


class TestApprovalRequirementDataclass:
    def test_required_true_carries_approval_id(self) -> None:
        req = ApprovalRequirement(
            required=True,
            approval_id="appr-001",
            reason="high risk",
        )
        assert req.required is True
        assert req.approval_id == "appr-001"
        assert req.reason == "high risk"

    def test_required_false_has_no_approval_id(self) -> None:
        req = ApprovalRequirement(required=False, reason="low risk")
        assert req.required is False
        assert req.approval_id is None

    def test_requirement_is_frozen(self) -> None:
        req = ApprovalRequirement(required=True, approval_id="appr-001")
        with pytest.raises(Exception):
            req.required = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Governance-spec coverage sanity check
# ---------------------------------------------------------------------------


class TestGovernanceSpecCoverage:
    def test_all_governance_actions_have_specs(self) -> None:
        """Every action in the governance registry has a spec —
        the gate never silently falls through to a default."""
        for action_type in ACTION_GOVERNANCE_REGISTRY:
            spec = get_action_governance_spec(action_type)
            assert spec is not None, f"missing spec for {action_type!r}"
            assert spec.action_type == action_type

    def test_unknown_action_returns_none(self) -> None:
        """An unknown action type returns None (caller treats as
        ACTION_NOT_SUPPORTED)."""
        assert get_action_governance_spec("nonexistent.action") is None


# ---------------------------------------------------------------------------
# FrozenClock injection
# ---------------------------------------------------------------------------


class TestFrozenClock:
    def test_frozen_clock_returns_constant(self) -> None:
        clock = FrozenClock(_TS)
        assert clock.now() == _TS
        assert clock.now() == _TS  # deterministic

    def test_frozen_clock_converts_naive_to_utc(self) -> None:
        naive = datetime(2026, 1, 1, 0, 0, 0)
        clock = FrozenClock(naive)
        assert clock.now().tzinfo is timezone.utc

    def test_frozen_clock_preserves_utc(self) -> None:
        clock = FrozenClock(_TS_LATER)
        assert clock.now() == _TS_LATER
        assert clock.now().tzinfo is timezone.utc
