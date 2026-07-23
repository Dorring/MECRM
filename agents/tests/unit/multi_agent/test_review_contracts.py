"""Phase 5A Review Contracts tests.

Covers (Phase 5A Section 17 — Contract):

* extra field rejected
* illegal Enum rejected
* JSON round-trip
* Hash tamper detection
* Defensive deep copy
* Cross-process hash stability
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
)
from multi_agent.contracts import ActionProposal
from multi_agent.execution import ExecutionRunIdentity
from multi_agent.review_contracts import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionAudit,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewRequest,
    ReviewRiskLevel,
    TaskRecordSummary,
    TraceSummary,
    REVIEWER_VERSION,
)
from multi_agent.review_errors import ReviewIntegrityError
from multi_agent.serialization import stable_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_finding(
    *,
    proposal_id: str = "prop-test-001",
    code: str = "review.test",
    severity: ReviewFindingSeverity = ReviewFindingSeverity.WARNING,
) -> ReviewFinding:
    return ReviewFinding(
        finding_code=code,
        severity=severity,
        message="test finding",
        proposal_id=proposal_id,
        details={"k": "v"},
    )


def _make_policy_audit(
    *,
    proposal_id: str = "prop-test-001",
    request_hash: str = "a" * 64,
    policy_request_hash: str = "b" * 64,
    decision: PolicyDecision = PolicyDecision.ALLOWED,
) -> PolicyDecisionAudit:
    """R2.1 P0-7: build a PolicyDecisionAudit with a valid evaluation_hash."""
    payload = {
        "evaluator_source_id": "deterministic",
        "evaluator_version": "test-v1",
        "policy_version": "test-v1",
        "decision": decision.value,
        "matched_rules": [],
        "proposal_id": proposal_id,
        "request_hash": request_hash,
        "policy_request_hash": policy_request_hash,
    }
    return PolicyDecisionAudit(
        evaluator_source_id="deterministic",
        evaluator_version="test-v1",
        policy_version="test-v1",
        decision=decision,
        matched_rules=(),
        proposal_id=proposal_id,
        request_hash=request_hash,
        policy_request_hash=policy_request_hash,
        evaluation_hash=stable_hash(payload),
    )


def _make_proposal_review(
    *,
    proposal_id: str = "prop-test-001",
    status: ReviewDecisionStatus = ReviewDecisionStatus.APPROVED,
) -> ProposalReview:
    return ProposalReview(
        proposal_id=proposal_id,
        status=status,
        findings=[_make_finding(proposal_id=proposal_id)],
        matched_evidence_ids=["ev-001", "ev-002"],
        required_approval=False,
        risk_level=ReviewRiskLevel.LOW,
        authority_valid=True,
        policy_valid=True,
        idempotency_valid=True,
        policy_audit=_make_policy_audit(proposal_id=proposal_id),
    )


def _make_request(
    *,
    review_id: str = "review-test-001",
    proposals: list[ActionProposal] | None = None,
) -> ReviewRequest:
    # R2.1 P0-4: run_identity is REQUIRED.
    identity_hash = stable_hash(
        {
            "run_id": "run-test-001",
            "tenant_id": "tenant-test",
            "plan_hash": "plan-test-hash",
            "registry_version": "registry-test-v1",
        }
    )
    run_identity = ExecutionRunIdentity(
        run_id="run-test-001",
        tenant_id="tenant-test",
        plan_hash="plan-test-hash",
        registry_version="registry-test-v1",
        identity_hash=identity_hash,
    )
    return ReviewRequest(
        review_id=review_id,
        run_id="run-test-001",
        tenant_id="tenant-test",
        plan_hash="plan-test-hash",
        registry_version="registry-test-v1",
        proposals=proposals or [],
        evidence=[],
        task_records=[
            TaskRecordSummary(
                task_id="task-test",
                agent_id="agent_test",
                status="completed",
            )
        ],
        trace=[
            TraceSummary(
                sequence=0,
                event_type="run_started",
            )
        ],
        capability_bindings=[],
        policy_context=PolicyContext(
            policy_version="test-v1",
            rules=[],
        ),
        run_identity=run_identity,
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        reviewer_version=REVIEWER_VERSION,
    )


# ---------------------------------------------------------------------------
# Enum / extra-field rejection
# ---------------------------------------------------------------------------


class TestReviewFindingValidation:
    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ReviewFinding(
                finding_code="x",
                severity=ReviewFindingSeverity.INFO,
                message="m",
                proposal_id="p1",
                extra_field="should fail",  # type: ignore[call-arg]
            )

    def test_illegal_severity_rejected(self):
        with pytest.raises(ValidationError):
            ReviewFinding(
                finding_code="x",
                severity="not-a-severity",  # type: ignore[arg-type]
                message="m",
                proposal_id="p1",
            )

    def test_blank_finding_code_rejected(self):
        with pytest.raises(ValidationError):
            ReviewFinding(
                finding_code="  ",
                severity=ReviewFindingSeverity.INFO,
                message="m",
                proposal_id="p1",
            )

    def test_blank_proposal_id_rejected(self):
        with pytest.raises(ValidationError):
            ReviewFinding(
                finding_code="x",
                severity=ReviewFindingSeverity.INFO,
                message="m",
                proposal_id="  ",
            )

    def test_sensitive_details_rejected(self):
        with pytest.raises(ValidationError):
            ReviewFinding(
                finding_code="x",
                severity=ReviewFindingSeverity.INFO,
                message="m",
                proposal_id="p1",
                details={"api_key": "secret"},
            )


class TestProposalReviewValidation:
    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ProposalReview(
                proposal_id="p1",
                status=ReviewDecisionStatus.APPROVED,
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_illegal_status_rejected(self):
        with pytest.raises(ValidationError):
            ProposalReview(
                proposal_id="p1",
                status="not-a-status",  # type: ignore[arg-type]
            )

    def test_blank_proposal_id_rejected(self):
        with pytest.raises(ValidationError):
            ProposalReview(
                proposal_id="",
                status=ReviewDecisionStatus.APPROVED,
            )


class TestReviewRequestValidation:
    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ReviewRequest(
                review_id="r1",
                run_id="run1",
                tenant_id="t1",
                plan_hash="ph",
                registry_version="rv",
                policy_context=PolicyContext(policy_version="v1"),
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_blank_identity_rejected(self):
        with pytest.raises(ValidationError):
            ReviewRequest(
                review_id="",
                run_id="run1",
                tenant_id="t1",
                plan_hash="ph",
                registry_version="rv",
                policy_context=PolicyContext(policy_version="v1"),
            )


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_finding_round_trip(self):
        f = _make_finding()
        rt = ReviewFinding.model_validate_json(f.model_dump_json())
        assert rt == f
        assert rt.review_hash if hasattr(rt, "review_hash") else True

    def test_proposal_review_round_trip_preserves_hash(self):
        r = _make_proposal_review()
        rt = ProposalReview.model_validate_json(r.model_dump_json())
        assert rt.review_hash == r.review_hash
        assert rt.compute_hash() == r.review_hash

    def test_review_request_round_trip_preserves_hash(self):
        req = _make_request()
        rt = ReviewRequest.model_validate_json(req.model_dump_json())
        assert rt.request_hash == req.request_hash
        rt.verify_integrity()

    def test_review_batch_result_round_trip_preserves_hash(self):
        review = _make_proposal_review()
        result = ReviewBatchResult(
            review_id="r1",
            run_id="run1",
            tenant_id="t1",
            request_hash="rh",
            proposal_reviews=[review],
            batch_status=ReviewBatchStatus.APPROVED,
            approved_proposal_ids=["prop-test-001"],
            governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
            reviewer_version=REVIEWER_VERSION,
        )
        rt = ReviewBatchResult.model_validate_json(result.model_dump_json())
        assert rt.result_hash == result.result_hash
        rt.verify_integrity()


# ---------------------------------------------------------------------------
# Hash tamper detection
# ---------------------------------------------------------------------------


class TestHashTamperDetection:
    def test_proposal_review_tamper_detected(self):
        r = _make_proposal_review()
        tampered = r.model_copy(update={"status": ReviewDecisionStatus.REJECTED})
        # The tampered copy has the ORIGINAL review_hash (model_copy
        # preserves all fields).  Recomputing the hash yields a
        # different value, so verify_integrity() must raise.
        with pytest.raises(ReviewIntegrityError):
            tampered.verify_integrity()

    def test_review_request_tamper_detected(self):
        req = _make_request()
        tampered = req.model_copy(update={"run_id": "different-run"})
        with pytest.raises(ReviewIntegrityError):
            tampered.verify_integrity()

    def test_review_batch_result_tamper_detected(self):
        review = _make_proposal_review()
        result = ReviewBatchResult(
            review_id="r1",
            run_id="run1",
            tenant_id="t1",
            request_hash="rh",
            proposal_reviews=[review],
            batch_status=ReviewBatchStatus.APPROVED,
            governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
            reviewer_version=REVIEWER_VERSION,
        )
        tampered = result.model_copy(
            update={"batch_status": ReviewBatchStatus.REJECTED}
        )
        with pytest.raises(ReviewIntegrityError):
            tampered.verify_integrity()

    def test_explicit_wrong_hash_rejected(self):
        with pytest.raises(ReviewIntegrityError):
            ProposalReview(
                proposal_id="p1",
                status=ReviewDecisionStatus.APPROVED,
                policy_audit=_make_policy_audit(proposal_id="p1"),
                review_hash="deadbeef" * 8,  # wrong hash
            )


# ---------------------------------------------------------------------------
# Defensive deep copy
# ---------------------------------------------------------------------------


class TestDefensiveDeepCopy:
    def test_proposal_review_copy_is_independent(self):
        r = _make_proposal_review()
        copy = r.model_copy()
        # Frozen models are immutable, but model_copy returns a new
        # instance — verify the hashes match but identities differ.
        assert copy.review_hash == r.review_hash
        assert copy is not r

    def test_review_request_copy_preserves_hash(self):
        req = _make_request()
        copy = req.model_copy()
        assert copy.request_hash == req.request_hash
        copy.verify_integrity()


# ---------------------------------------------------------------------------
# Cross-process hash stability
# ---------------------------------------------------------------------------


class TestCrossProcessHashStability:
    """Verify the same ProposalReview produces the same hash in a
    different Python process (different PYTHONHASHSEED)."""

    def test_cross_process_hash_stable(self):
        # Build the same ProposalReview in a subprocess and compare
        # the hash.  Uses PYTHONPATH so the subprocess can import
        # multi_agent.
        agents_dir = str(Path(__file__).resolve().parents[3])
        src_path = str(Path(agents_dir) / "src")
        env = os.environ.copy()
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONHASHSEED"] = "12345"

        code = (
            "from multi_agent.review_contracts import ("
            "ProposalReview, ReviewDecisionStatus, ReviewFinding, "
            "ReviewFindingSeverity, ReviewRiskLevel, PolicyDecision, "
            "PolicyDecisionAudit)\n"
            "from multi_agent.serialization import stable_hash\n"
            "f = ReviewFinding(finding_code='x.y', severity="
            "ReviewFindingSeverity.WARNING, message='m', proposal_id='p1')\n"
            "pa = PolicyDecisionAudit(evaluator_source_id='deterministic', "
            "evaluator_version='test-v1', policy_version='test-v1', "
            "decision=PolicyDecision.ALLOWED, matched_rules=(), "
            "proposal_id='p1', request_hash='a'*64, "
            "policy_request_hash='b'*64, evaluation_hash=stable_hash({"
            "'evaluator_source_id':'deterministic',"
            "'evaluator_version':'test-v1',"
            "'policy_version':'test-v1','decision':'allowed',"
            "'matched_rules':[],'proposal_id':'p1',"
            "'request_hash':'a'*64,"
            "'policy_request_hash':'b'*64}))\n"
            "r = ProposalReview(proposal_id='p1', status="
            "ReviewDecisionStatus.APPROVED, findings=[f], "
            "matched_evidence_ids=['e1','e2'], risk_level="
            "ReviewRiskLevel.LOW, authority_valid=True, policy_valid=True, "
            "idempotency_valid=True, policy_audit=pa)\n"
            "print(r.review_hash)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=agents_dir,
            env=env,
        )
        assert result.returncode == 0, f"subprocess failed: {result.stderr}"
        child_hash = result.stdout.strip()

        # Now compute the same hash in this process
        f = ReviewFinding(
            finding_code="x.y",
            severity=ReviewFindingSeverity.WARNING,
            message="m",
            proposal_id="p1",
        )
        pa = _make_policy_audit(proposal_id="p1")
        r = ProposalReview(
            proposal_id="p1",
            status=ReviewDecisionStatus.APPROVED,
            findings=[f],
            matched_evidence_ids=["e1", "e2"],
            risk_level=ReviewRiskLevel.LOW,
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=pa,
        )
        assert r.review_hash == child_hash, (
            f"hash mismatch: parent={r.review_hash!r} child={child_hash!r}"
        )


# Need to import os at module level for the subprocess test
import os  # noqa: E402  - intentionally after pytest imports for clarity


# ---------------------------------------------------------------------------
# Batch status priority
# ---------------------------------------------------------------------------


class TestBatchStatusPriority:
    def test_conflict_highest_priority(self):
        from multi_agent.review_contracts import batch_status_priority

        assert batch_status_priority(
            ReviewBatchStatus.CONFLICT
        ) > batch_status_priority(ReviewBatchStatus.REJECTED)
        assert batch_status_priority(
            ReviewBatchStatus.CONFLICT
        ) > batch_status_priority(ReviewBatchStatus.APPROVED)

    def test_approved_lowest_priority(self):
        from multi_agent.review_contracts import batch_status_priority

        assert batch_status_priority(ReviewBatchStatus.APPROVED) == 0

    def test_proposal_to_batch_mapping(self):
        from multi_agent.review_contracts import proposal_status_to_batch

        assert (
            proposal_status_to_batch(ReviewDecisionStatus.APPROVED)
            == ReviewBatchStatus.APPROVED
        )
        assert (
            proposal_status_to_batch(ReviewDecisionStatus.CONFLICT)
            == ReviewBatchStatus.CONFLICT
        )
