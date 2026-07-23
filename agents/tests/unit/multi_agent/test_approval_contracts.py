"""Phase 5B — Approval contracts integrity tests.

Covers (Phase 5B Section 33):

* ApprovalRequest / ApprovalDecision hash consistency — the same
  semantic content always yields the same SHA-256, stable across
  processes and ``PYTHONHASHSEED`` values.
* Frozen immutability — neither contract can be mutated after
  construction (``frozen=True``).
* Round-trip — ``model_dump`` -> ``model_validate`` preserves every
  field including the recomputed hash.
* Hash tamper detection — a stored hash that does not match the
  recomputed content is rejected at construction.
* Timezone enforcement — naive ``datetime`` fields raise ``ValueError``.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from multi_agent.approval_contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    FrozenClock,
    SystemClock,
    is_terminal_approval_status,
)
from multi_agent.review_contracts import ReviewRiskLevel

from phase5b_helpers import TS, TENANT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    approval_id: str = "appr-001",
    authorization_id: str = "auth-001",
    proposal_id: str = "prop-001",
    run_id: str = "run-001",
    expires_at: datetime | None = None,
    required_roles: tuple[str, ...] = ("approver",),
) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        authorization_id=authorization_id,
        tenant_id=TENANT,
        run_id=run_id,
        proposal_id=proposal_id,
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        authorization_hash="a" * 64,
        risk_level=ReviewRiskLevel.HIGH,
        action_type="crm.owner.assign",
        action_summary="Assign owner",
        required_approver_roles=required_roles,
        requested_by="system",
        requested_at=TS,
        expires_at=expires_at,
    )


def _make_decision(
    *,
    approval_id: str = "appr-001",
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    authorization_hash: str = "a" * 64,
    request: ApprovalRequest | None = None,
    approver_roles: tuple[str, ...] = ("approver",),
) -> ApprovalDecision:
    req = request or _make_request()
    return ApprovalDecision(
        approval_id=approval_id,
        status=status,
        approver_id="user-001",
        approver_roles=approver_roles,
        decision_reason="approved",
        decided_at=TS,
        approval_request_hash=req.approval_request_hash,
        authorization_hash=authorization_hash,
    )


# ---------------------------------------------------------------------------
# ApprovalRequest tests
# ---------------------------------------------------------------------------


class TestApprovalRequestHashConsistency:
    """ApprovalRequest.compute_hash is stable across instances."""

    def test_two_identical_requests_yield_same_hash(self) -> None:
        r1 = _make_request()
        r2 = _make_request()
        assert r1.approval_request_hash == r2.approval_request_hash
        assert r1.compute_hash() == r2.compute_hash()

    def test_hash_is_64_char_hex(self) -> None:
        r = _make_request()
        assert len(r.approval_request_hash) == 64
        int(r.approval_request_hash, 16)  # valid hex

    def test_hash_excludes_self_referential_field(self) -> None:
        """The stored hash MUST NOT be part of the canonical hash input."""
        r = _make_request()
        # If hash included itself, computing again would change.
        assert r.compute_hash() == r.approval_request_hash

    def test_different_proposal_id_changes_hash(self) -> None:
        r1 = _make_request(proposal_id="prop-A")
        r2 = _make_request(proposal_id="prop-B")
        assert r1.approval_request_hash != r2.approval_request_hash

    def test_different_authorization_hash_changes_request_hash(self) -> None:
        r1 = _make_request()
        # Build a different authorization_hash and rebuild — clear the
        # stored hash so the validator recomputes it from new content.
        r2_data = r1.model_dump(mode="python")
        r2_data["authorization_hash"] = "b" * 64
        r2_data["approval_request_hash"] = ""
        r2 = ApprovalRequest.model_validate(r2_data)
        assert r1.approval_request_hash != r2.approval_request_hash


class TestApprovalRequestFrozen:
    """ApprovalRequest is immutable after construction."""

    def test_field_assignment_raises(self) -> None:
        r = _make_request()
        with pytest.raises((ValidationError, TypeError)):
            r.proposal_id = "mutated"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalRequest(
                approval_id="appr-x",
                authorization_id="auth-x",
                tenant_id=TENANT,
                run_id="run-x",
                proposal_id="prop-x",
                review_request_hash="r" * 64,
                review_result_hash="s" * 64,
                authorization_hash="a" * 64,
                risk_level=ReviewRiskLevel.LOW,
                action_type="report.generate",
                action_summary="x",
                required_approver_roles=("approver",),
                requested_by="system",
                requested_at=TS,
                unknown_field="x",  # type: ignore[call-arg]
            )

    def test_required_roles_sorted_and_frozen(self) -> None:
        r = _make_request(required_roles=("zeta", "alpha", "beta"))
        assert r.required_approver_roles == ("alpha", "beta", "zeta")


class TestApprovalRequestRoundTrip:
    """model_dump -> model_validate preserves every field."""

    def test_round_trip_preserves_hash(self) -> None:
        r1 = _make_request()
        dumped = r1.model_dump(mode="python")
        r2 = ApprovalRequest.model_validate(dumped)
        assert r1.approval_request_hash == r2.approval_request_hash
        assert r1 == r2

    def test_round_trip_with_expires_at(self) -> None:
        r1 = _make_request(expires_at=TS + timedelta(hours=24))
        dumped = r1.model_dump(mode="python")
        r2 = ApprovalRequest.model_validate(dumped)
        assert r1.expires_at == r2.expires_at
        assert r1.approval_request_hash == r2.approval_request_hash


class TestApprovalRequestTamperDetection:
    """A stored hash that does not match recomputed content is rejected."""

    def test_tampered_hash_rejected(self) -> None:
        r = _make_request()
        dumped = r.model_dump(mode="python")
        dumped["approval_request_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ApprovalRequest.model_validate(dumped)

    def test_verify_integrity_passes_for_valid(self) -> None:
        r = _make_request()
        r.verify_integrity()  # no raise

    def test_verify_integrity_detects_tamper(self) -> None:
        r = _make_request()
        # Forge the stored hash via object.__setattr__ (bypass frozen).
        object.__setattr__(r, "approval_request_hash", "0" * 64)
        with pytest.raises(ValueError):
            r.verify_integrity()


class TestApprovalRequestTimezone:
    """Naive datetime fields raise ValueError."""

    def test_naive_requested_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalRequest(
                approval_id="appr-naive",
                authorization_id="auth-naive",
                tenant_id=TENANT,
                run_id="run-naive",
                proposal_id="prop-naive",
                review_request_hash="r" * 64,
                review_result_hash="s" * 64,
                authorization_hash="a" * 64,
                risk_level=ReviewRiskLevel.LOW,
                action_type="report.generate",
                action_summary="x",
                required_approver_roles=("approver",),
                requested_by="system",
                requested_at=datetime(2026, 1, 1),  # naive
            )

    def test_naive_expires_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalRequest(
                approval_id="appr-naive2",
                authorization_id="auth-naive2",
                tenant_id=TENANT,
                run_id="run-naive2",
                proposal_id="prop-naive2",
                review_request_hash="r" * 64,
                review_result_hash="s" * 64,
                authorization_hash="a" * 64,
                risk_level=ReviewRiskLevel.LOW,
                action_type="report.generate",
                action_summary="x",
                required_approver_roles=("approver",),
                requested_by="system",
                requested_at=TS,
                expires_at=datetime(2026, 1, 2),  # naive
            )

    def test_non_utc_timezone_converted_to_utc(self) -> None:
        # +08:00 should be converted to UTC.
        tz_plus_8 = timezone(timedelta(hours=8))
        r = ApprovalRequest(
            approval_id="appr-tz",
            authorization_id="auth-tz",
            tenant_id=TENANT,
            run_id="run-tz",
            proposal_id="prop-tz",
            review_request_hash="r" * 64,
            review_result_hash="s" * 64,
            authorization_hash="a" * 64,
            risk_level=ReviewRiskLevel.LOW,
            action_type="report.generate",
            action_summary="x",
            required_approver_roles=("approver",),
            requested_by="system",
            requested_at=datetime(2026, 1, 1, 8, 0, 0, tzinfo=tz_plus_8),
        )
        assert r.requested_at.tzinfo == timezone.utc
        assert r.requested_at.hour == 0  # 08:00 +08:00 == 00:00 UTC


class TestApprovalRequestIsExpired:
    """is_expired respects the injected clock / passed time."""

    def test_no_expires_at_never_expires(self) -> None:
        r = _make_request(expires_at=None)
        assert not r.is_expired(datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_expired_when_now_past_expires_at(self) -> None:
        r = _make_request(expires_at=TS + timedelta(hours=1))
        assert r.is_expired(TS + timedelta(hours=2))

    def test_not_expired_when_now_before_expires_at(self) -> None:
        r = _make_request(expires_at=TS + timedelta(hours=1))
        assert not r.is_expired(TS + timedelta(minutes=30))


# ---------------------------------------------------------------------------
# ApprovalDecision tests
# ---------------------------------------------------------------------------


class TestApprovalDecisionHashConsistency:
    def test_identical_decisions_yield_same_hash(self) -> None:
        d1 = _make_decision()
        d2 = _make_decision()
        assert d1.decision_hash == d2.decision_hash

    def test_hash_is_64_char_hex(self) -> None:
        d = _make_decision()
        assert len(d.decision_hash) == 64
        int(d.decision_hash, 16)

    def test_different_status_changes_hash(self) -> None:
        d1 = _make_decision(status=ApprovalStatus.APPROVED)
        d2 = _make_decision(status=ApprovalStatus.REJECTED)
        assert d1.decision_hash != d2.decision_hash

    def test_different_authorization_hash_changes_decision_hash(self) -> None:
        d1 = _make_decision(authorization_hash="a" * 64)
        d2 = _make_decision(authorization_hash="b" * 64)
        assert d1.decision_hash != d2.decision_hash


class TestApprovalDecisionFrozen:
    def test_field_assignment_raises(self) -> None:
        d = _make_decision()
        with pytest.raises((ValidationError, TypeError)):
            d.status = ApprovalStatus.REJECTED  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalDecision(
                approval_id="x",
                status=ApprovalStatus.APPROVED,
                approver_id="u",
                approver_roles=("approver",),
                decision_reason="ok",
                decided_at=TS,
                approval_request_hash="r" * 64,
                authorization_hash="a" * 64,
                extra="x",  # type: ignore[call-arg]
            )


class TestApprovalDecisionRoundTrip:
    def test_round_trip_preserves_hash(self) -> None:
        d1 = _make_decision()
        dumped = d1.model_dump(mode="python")
        d2 = ApprovalDecision.model_validate(dumped)
        assert d1.decision_hash == d2.decision_hash
        assert d1 == d2


class TestApprovalDecisionTamperDetection:
    def test_tampered_hash_rejected(self) -> None:
        d = _make_decision()
        dumped = d.model_dump(mode="python")
        dumped["decision_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ApprovalDecision.model_validate(dumped)

    def test_verify_integrity_detects_tamper(self) -> None:
        d = _make_decision()
        object.__setattr__(d, "decision_hash", "0" * 64)
        with pytest.raises(ValueError):
            d.verify_integrity()


# ---------------------------------------------------------------------------
# Clock + status helpers
# ---------------------------------------------------------------------------


class TestClockImplementations:
    def test_system_clock_returns_utc_now(self) -> None:
        c = SystemClock()
        now = c.now()
        assert now.tzinfo is not None
        # Should be very close to actual now.
        delta = abs((datetime.now(timezone.utc) - now).total_seconds())
        assert delta < 5.0

    def test_frozen_clock_returns_fixed_instant(self) -> None:
        c = FrozenClock(TS)
        assert c.now() == TS

    def test_frozen_clock_coerces_naive_to_utc(self) -> None:
        c = FrozenClock(datetime(2026, 1, 1))
        assert c.now().tzinfo == timezone.utc


class TestIsTerminalApprovalStatus:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (ApprovalStatus.NOT_REQUIRED, False),
            (ApprovalStatus.PENDING, False),
            (ApprovalStatus.APPROVED, True),
            (ApprovalStatus.REJECTED, True),
            (ApprovalStatus.EXPIRED, True),
            (ApprovalStatus.REVOKED, True),
            (ApprovalStatus.CONSUMED, True),
        ],
    )
    def test_terminal_detection(self, status: ApprovalStatus, expected: bool) -> None:
        assert is_terminal_approval_status(status) is expected


class TestApprovalRequestBlankValidation:
    """Identity fields must not be blank (Section 7)."""

    @pytest.mark.parametrize(
        "field",
        [
            "approval_id",
            "authorization_id",
            "tenant_id",
            "run_id",
            "proposal_id",
            "review_request_hash",
            "review_result_hash",
            "authorization_hash",
            "action_type",
            "action_summary",
            "requested_by",
        ],
    )
    def test_blank_identity_field_rejected(self, field: str) -> None:
        kwargs = dict(
            approval_id="appr-b",
            authorization_id="auth-b",
            tenant_id=TENANT,
            run_id="run-b",
            proposal_id="prop-b",
            review_request_hash="r" * 64,
            review_result_hash="s" * 64,
            authorization_hash="a" * 64,
            risk_level=ReviewRiskLevel.LOW,
            action_type="report.generate",
            action_summary="x",
            required_approver_roles=("approver",),
            requested_by="system",
            requested_at=TS,
        )
        kwargs[field] = "   "
        with pytest.raises(ValidationError):
            ApprovalRequest(**kwargs)

    def test_empty_required_roles_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalRequest(
                approval_id="appr-roles",
                authorization_id="auth-roles",
                tenant_id=TENANT,
                run_id="run-roles",
                proposal_id="prop-roles",
                review_request_hash="r" * 64,
                review_result_hash="s" * 64,
                authorization_hash="a" * 64,
                risk_level=ReviewRiskLevel.LOW,
                action_type="report.generate",
                action_summary="x",
                required_approver_roles=(),
                requested_by="system",
                requested_at=TS,
            )
