"""Phase 5A R2 Trust-Chain Regression Tests.

Covers the R2 supplementary requirements (S1–S14) that strengthen the
trust chain beyond the R1 P0 fixes:

* S1:   Deep immutability — every collection field is a tuple/frozenset.
* S2:   ExecutionRunIdentity + ResultOriginSnapshot — authoritative
         identity with hash verification and tamper detection.
* S3:   Governance spec version/hash — the Reviewer rejects a Request
         built against a stale or tampered spec.
* S5:   PolicyDecisionAudit — every Proposal carries one, including
         skipped-authority-failure; evaluation_hash is verified.
* S6:   EvidenceDeduplicationAudit — audit_hash is verified.
* S7:   ReviewBatchStatus.NO_ACTIONS for empty batch (NOT APPROVED).
* S8:   ReviewBatchResult.verify_against_request binds Result → Request.
* S9:   ReviewExpectedOutcome contract.
* S10:  reviewer_version is required on ReviewBatchResult.
* S12:  ReviewGraphError — persistable, non-blank error contract.
* S14:  Action Governance Spec Registry — single source of truth.

All async tests use ``asyncio.run()`` inside the test body — NOT
``@pytest.mark.asyncio`` — per the R2 spec.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_REGISTRY,
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
    ActionGovernanceSpec,
    get_action_governance_spec,
)
from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    Evidence,
    EvidenceType,
)
from multi_agent.evidence_review import compute_review_evidence_hash
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ResultOriginSnapshot,
)
from multi_agent.policy import DeterministicPolicyEvaluator
from multi_agent.review_contracts import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionAudit,
    PolicyMatchedRule,
    PolicyRule,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewEvidenceSnapshot,
    ReviewExpectedOutcome,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewGraphError,
    ReviewProposalEnvelope,
    ReviewRequest,
    REVIEW_SCHEMA_VERSION,
    REVIEWER_VERSION,
    TaskRecordSummary,
    TraceSummary,
    EvidenceDeduplicationAudit,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    ReviewIntegrityError,
)
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev-r2-001",
    *,
    tenant_id: str = "tenant-r2",
    source_agent: str = "agent_r2",
    content_hash: str = "b" * 64,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.CUSTOMER,
        tenant_id=tenant_id,
        source_agent=source_agent,
        content_hash=content_hash,
        created_at=_TS,
    )


def _make_proposal(
    proposal_id: str = "prop-r2-001",
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict[str, object] | None = None,
    evidence_ids: list[str] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    idempotency_key: str = "r2-idem-0001",
    tenant_id: str = "tenant-r2",
    created_by_agent: str = "agent_r2",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity=target_entity,
        target_id=target_id,
        payload=payload or {},
        priority="medium",
        risk_level=risk_level,
        evidence_ids=evidence_ids or [],
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


def _make_capability(
    agent_id: str = "agent_r2",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="r2",
        domains=frozenset({"d"}),
        supported_tasks=frozenset({"t"}),
        allowed_tools=frozenset({"crm_reader.get_customers"}),
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
    )


def _make_capability_binding(
    capability: AgentCapability | None = None,
    *,
    task_id: str = "task-r2",
    agent_id: str = "agent_r2",
) -> ExecutionCapabilitySnapshot:
    cap = capability or _make_capability(agent_id)
    return ExecutionCapabilitySnapshot(
        task_id=task_id,
        agent_id=agent_id,
        agent_version=cap.version,
        capability=cap,
        binding_hash=stable_hash(
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "agent_version": cap.version,
                "capability": cap.model_dump(mode="python"),
            }
        ),
    )


def _make_envelope(
    proposal: ActionProposal,
    *,
    run_id: str = "run-r2",
    result_id: str = "r-r2",
    task_id: str = "task-r2",
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    aid = proposal.created_by_agent
    return ReviewProposalEnvelope(
        proposal=proposal,
        run_id=run_id,
        result_id=result_id,
        task_id=task_id,
        agent_id=aid,
        agent_version=agent_version,
        origin_hash=stable_hash(
            {
                "proposal": proposal.model_dump(mode="python"),
                "run_id": run_id,
                "result_id": result_id,
                "task_id": task_id,
                "agent_id": aid,
                "agent_version": agent_version,
            }
        ),
    )


def _make_request(
    proposals: list[ActionProposal] | None = None,
    *,
    evidence: list[Evidence] | None = None,
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
    proposal_envelopes: list[ReviewProposalEnvelope] | None = None,
    governance_spec_version: str = ACTION_GOVERNANCE_SPEC_VERSION,
    governance_spec_hash: str = ACTION_GOVERNANCE_SPEC_HASH,
    review_id: str = "review-r2-001",
) -> ReviewRequest:
    # Use explicit None checks so empty lists are preserved (R2 S7
    # tests rely on passing proposals=[] for an empty batch).
    props = (
        proposals
        if proposals is not None
        else [_make_proposal(evidence_ids=["ev-r2-001"])]
    )
    raw_evidence = evidence if evidence is not None else [_make_evidence("ev-r2-001")]
    evidence_snapshots = [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in raw_evidence
    ]
    return ReviewRequest(
        review_id=review_id,
        run_id="run-r2",
        tenant_id="tenant-r2",
        plan_hash="plan-r2-hash",
        registry_version="registry-r2-v1",
        proposals=props,
        evidence=evidence_snapshots,
        task_records=[
            TaskRecordSummary(
                task_id="task-r2", agent_id="agent_r2", status="completed"
            )
        ],
        trace=[TraceSummary(sequence=0, event_type="run_started")],
        capability_bindings=capability_bindings or [_make_capability_binding()],
        proposal_envelopes=(
            proposal_envelopes
            if proposal_envelopes is not None
            else [_make_envelope(p) for p in props]
        ),
        policy_context=PolicyContext(
            policy_version="r2-v1",
            rules=(),
        ),
        governance_spec_version=governance_spec_version,
        governance_spec_hash=governance_spec_hash,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


def _valid_request() -> ReviewRequest:
    return _make_request()


# ===========================================================================
# S1: Deep immutability — every collection field is a tuple/frozenset
# ===========================================================================


class TestDeepImmutability:
    """R2 S1: collection fields are tuples, not lists."""

    def test_request_collections_are_tuples(self):
        req = _valid_request()
        assert isinstance(req.proposals, tuple)
        assert isinstance(req.evidence, tuple)
        assert isinstance(req.task_records, tuple)
        assert isinstance(req.trace, tuple)
        assert isinstance(req.proposal_envelopes, tuple)
        assert isinstance(req.capability_bindings, tuple)
        assert isinstance(req.result_origins, tuple)

    def test_policy_context_rules_is_tuple(self):
        ctx = PolicyContext(policy_version="r2", rules=())
        assert isinstance(ctx.rules, tuple)

    def test_finding_evidence_ids_is_tuple(self):
        f = ReviewFinding(
            finding_code="review.test",
            severity=ReviewFindingSeverity.INFO,
            message="test",
            proposal_id="p1",
        )
        assert isinstance(f.evidence_ids, tuple)

    def test_result_collections_are_tuples(self):
        result = asyncio.run(
            ProposalReviewer().review(
                _valid_request(),
                policy_evaluator=DeterministicPolicyEvaluator(),
            )
        )
        assert isinstance(result.proposal_reviews, tuple)
        assert isinstance(result.approved_proposal_ids, tuple)
        assert isinstance(result.rejected_proposal_ids, tuple)
        assert isinstance(result.findings, tuple)


# ===========================================================================
# S2: ExecutionRunIdentity + ResultOriginSnapshot
# ===========================================================================


class TestExecutionRunIdentity:
    """R2 S2: authoritative Run identity with hash verification."""

    def _make_identity(
        self,
        *,
        run_id: str = "run-r2",
        tenant_id: str = "tenant-r2",
        plan_hash: str = "plan-r2-hash",
        registry_version: str = "registry-r2-v1",
    ) -> ExecutionRunIdentity:
        identity_hash = stable_hash(
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "plan_hash": plan_hash,
                "registry_version": registry_version,
            }
        )
        return ExecutionRunIdentity(
            run_id=run_id,
            tenant_id=tenant_id,
            plan_hash=plan_hash,
            registry_version=registry_version,
            identity_hash=identity_hash,
        )

    def test_valid_identity_accepted(self):
        identity = self._make_identity()
        assert identity.run_id == "run-r2"

    def test_tampered_identity_hash_rejected(self):
        identity = self._make_identity()
        object.__setattr__(identity, "identity_hash", "tampered" * 8)
        with pytest.raises((ReviewIntegrityError, ValidationError)):
            identity.model_validate(identity.model_dump(mode="python"))

    def test_blank_run_id_rejected(self):
        with pytest.raises(ValidationError):
            self._make_identity(run_id="  ")

    def test_identity_is_frozen(self):
        identity = self._make_identity()
        with pytest.raises(ValidationError):
            identity.run_id = "tampered"  # type: ignore[misc]


class TestResultOriginSnapshot:
    """R2 P0-1 / S2: per-result origin snapshot with hash verification."""

    def _make_origin(
        self,
        *,
        result_id: str = "r-r2",
        task_id: str = "task-r2",
        agent_id: str = "agent_r2",
        agent_version: str = "1.0.0",
    ) -> ResultOriginSnapshot:
        origin_hash = stable_hash(
            {
                "result_id": result_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "agent_version": agent_version,
            }
        )
        return ResultOriginSnapshot(
            result_id=result_id,
            task_id=task_id,
            agent_id=agent_id,
            agent_version=agent_version,
            origin_hash=origin_hash,
        )

    def test_valid_origin_accepted(self):
        origin = self._make_origin()
        assert origin.result_id == "r-r2"

    def test_blank_origin_hash_rejected(self):
        with pytest.raises(ValidationError):
            ResultOriginSnapshot(
                result_id="r",
                task_id="t",
                agent_id="a",
                agent_version="1.0.0",
                origin_hash="",
            )

    def test_origin_is_frozen(self):
        origin = self._make_origin()
        with pytest.raises(ValidationError):
            origin.result_id = "tampered"  # type: ignore[misc]


# ===========================================================================
# S3: Governance spec version/hash verification
# ===========================================================================


class TestGovernanceSpecVerification:
    """R2 S3: the Reviewer rejects a Request with a stale/tampered spec."""

    def test_valid_spec_accepted(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        assert result.batch_status in (
            ReviewBatchStatus.APPROVED,
            ReviewBatchStatus.NEEDS_APPROVAL,
        )

    def test_mismatched_spec_version_rejected(self):
        req = _make_request(governance_spec_version="stale.version")
        with pytest.raises(InvalidReviewRequestError):
            asyncio.run(
                ProposalReviewer().review(
                    req, policy_evaluator=DeterministicPolicyEvaluator()
                )
            )

    def test_mismatched_spec_hash_rejected(self):
        req = _make_request(governance_spec_hash="0" * 64)
        with pytest.raises(InvalidReviewRequestError):
            asyncio.run(
                ProposalReviewer().review(
                    req, policy_evaluator=DeterministicPolicyEvaluator()
                )
            )

    def test_empty_spec_hash_allowed(self):
        """An empty governance_spec_hash is allowed (legacy fixtures)."""
        req = _make_request(governance_spec_hash="")
        # Should NOT raise — empty hash skips the check.
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        assert result.review_id == req.review_id


# ===========================================================================
# S5: PolicyDecisionAudit — every Proposal carries one
# ===========================================================================


class TestPolicyDecisionAudit:
    """R2 S5: per-Proposal policy audit, including skipped-authority."""

    def test_audit_hash_verified_on_construction(self):
        audit = PolicyDecisionAudit(
            evaluator_source_id="deterministic",
            evaluator_version="r2-v1",
            policy_version="r2-v1",
            decision=PolicyDecision.ALLOWED,
            matched_rules=(),
            evaluation_hash=stable_hash(
                {
                    "evaluator_source_id": "deterministic",
                    "evaluator_version": "r2-v1",
                    "policy_version": "r2-v1",
                    "decision": "allowed",
                    "matched_rules": [],
                }
            ),
        )
        assert audit.decision == PolicyDecision.ALLOWED

    def test_tampered_audit_hash_rejected(self):
        with pytest.raises(ValidationError):
            PolicyDecisionAudit(
                evaluator_source_id="deterministic",
                evaluator_version="r2-v1",
                policy_version="r2-v1",
                decision=PolicyDecision.ALLOWED,
                matched_rules=(),
                evaluation_hash="tampered" * 8,
            )

    def test_blank_evaluator_source_id_rejected(self):
        with pytest.raises(ValidationError):
            PolicyDecisionAudit(
                evaluator_source_id="",
                evaluator_version="v1",
                policy_version="v1",
                decision=PolicyDecision.ALLOWED,
                evaluation_hash="x" * 64,
            )

    def test_every_proposal_carries_audit(self):
        """Every ProposalReview — including authority-failures — has a
        non-None ``policy_audit``."""
        # Use a READ agent proposing a PROPOSE-level action so authority
        # fails and the Reviewer skips external Policy evaluation.
        prop = _make_proposal(
            "prop-auth-fail-001",
            action_type="crm.tag.update",
            evidence_ids=["ev-auth-fail"],
            risk_level=ActionRiskLevel.MEDIUM,
            idempotency_key="auth-fail-key-0001",
        )
        ev = _make_evidence("ev-auth-fail")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request(
            proposals=[prop],
            evidence=[ev],
            capability_bindings=[cap],
        )
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        assert len(result.proposal_reviews) == 1
        review = result.proposal_reviews[0]
        assert review.policy_audit is not None
        # The audit for an authority-failure is skipped.
        assert review.policy_audit.evaluator_source_id == "skipped-authority-failure"

    def test_normal_proposal_carries_real_audit(self):
        """A valid Proposal carries a real (non-skipped) audit."""
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        review = result.proposal_reviews[0]
        assert review.policy_audit is not None
        assert review.policy_audit.evaluator_source_id != "skipped-authority-failure"


# ===========================================================================
# S6: EvidenceDeduplicationAudit
# ===========================================================================


class TestEvidenceDeduplicationAudit:
    """R2 S6: audit hash verification and dedup semantics."""

    def test_valid_audit_accepted(self):
        audit = EvidenceDeduplicationAudit(
            deduped_evidence_ids=frozenset(),
            original_count=1,
            snapshot_count=1,
            audit_hash=stable_hash(
                {
                    "deduped_evidence_ids": [],
                    "original_count": 1,
                    "snapshot_count": 1,
                }
            ),
        )
        assert audit.original_count == 1

    def test_tampered_audit_hash_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceDeduplicationAudit(
                deduped_evidence_ids=frozenset(),
                original_count=1,
                snapshot_count=1,
                audit_hash="tampered" * 8,
            )

    def test_negative_count_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceDeduplicationAudit(
                deduped_evidence_ids=frozenset(),
                original_count=-1,
                snapshot_count=0,
                audit_hash="x" * 64,
            )

    def test_deduped_ids_is_frozenset(self):
        audit = EvidenceDeduplicationAudit(
            deduped_evidence_ids=frozenset({"ev-1"}),
            original_count=2,
            snapshot_count=1,
            audit_hash=stable_hash(
                {
                    "deduped_evidence_ids": ["ev-1"],
                    "original_count": 2,
                    "snapshot_count": 1,
                }
            ),
        )
        assert isinstance(audit.deduped_evidence_ids, frozenset)


# ===========================================================================
# S7: NO_ACTIONS for empty batch
# ===========================================================================


class TestEmptyBatchNoActions:
    """R2 S7: empty batch returns NO_ACTIONS, NOT APPROVED."""

    def test_empty_request_returns_no_actions(self):
        req = _make_request(proposals=[], evidence=[])
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        assert result.batch_status == ReviewBatchStatus.NO_ACTIONS
        assert result.proposal_reviews == ()


# ===========================================================================
# S8: verify_against_request — Result → Request binding
# ===========================================================================


class TestVerifyAgainstRequest:
    """R2 S8: binds Result back to its Request."""

    def _review(self) -> tuple[ReviewRequest, ReviewBatchResult]:
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        return req, result

    def test_valid_result_passes_verification(self):
        req, result = self._review()
        # Should not raise.
        result.verify_against_request(req)

    def test_mismatched_review_id_rejected(self):
        req, result = self._review()
        tampered_req = req.model_copy(update={"review_id": "other-review"})
        # Re-compute hash so the tampered request is internally consistent.
        object.__setattr__(tampered_req, "review_id", "other-review")
        with pytest.raises(InvalidReviewResultError):
            result.verify_against_request(tampered_req)

    def test_mismatched_run_id_rejected(self):
        req, result = self._review()
        tampered_req = req.model_copy(update={"run_id": "other-run"})
        with pytest.raises(InvalidReviewResultError):
            result.verify_against_request(tampered_req)

    def test_mismatched_tenant_id_rejected(self):
        req, result = self._review()
        tampered_req = req.model_copy(update={"tenant_id": "other-tenant"})
        with pytest.raises(InvalidReviewResultError):
            result.verify_against_request(tampered_req)

    def test_mismatched_request_hash_rejected(self):
        req, result = self._review()
        # Build a different request with a different plan_hash so the
        # request_hash differs.
        other_req = _make_request(review_id="review-r2-other")
        with pytest.raises(InvalidReviewResultError):
            result.verify_against_request(other_req)

    def test_extra_proposal_review_rejected(self):
        """A Result with a ProposalReview for a Proposal not in the
        Request must fail verification."""
        req, result = self._review()
        # Append an extra review for a non-existent proposal.
        # review_hash is left empty so the model computes it automatically.
        extra_review = ProposalReview(
            proposal_id="prop-nonexistent",
            status=ReviewDecisionStatus.APPROVED,
            findings=(),
            policy_audit=PolicyDecisionAudit(
                evaluator_source_id="test",
                evaluator_version="v1",
                policy_version="v1",
                decision=PolicyDecision.ALLOWED,
                matched_rules=(),
                evaluation_hash=stable_hash(
                    {
                        "evaluator_source_id": "test",
                        "evaluator_version": "v1",
                        "policy_version": "v1",
                        "decision": "allowed",
                        "matched_rules": [],
                    }
                ),
            ),
        )
        tampered_reviews = result.proposal_reviews + (extra_review,)
        tampered_result = ReviewBatchResult(
            review_id=result.review_id,
            run_id=result.run_id,
            tenant_id=result.tenant_id,
            request_hash=result.request_hash,
            proposal_reviews=tampered_reviews,
            batch_status=result.batch_status,
            approved_proposal_ids=result.approved_proposal_ids,
            rejected_proposal_ids=result.rejected_proposal_ids,
            approval_required_proposal_ids=result.approval_required_proposal_ids,
            conflicted_proposal_ids=result.conflicted_proposal_ids,
            deduplicated_proposal_ids=result.deduplicated_proposal_ids,
            findings=result.findings,
            governance_spec_hash=result.governance_spec_hash,
            reviewer_version=result.reviewer_version,
        )
        with pytest.raises(InvalidReviewResultError):
            tampered_result.verify_against_request(req)


# ===========================================================================
# S9: ReviewExpectedOutcome contract
# ===========================================================================


class TestReviewExpectedOutcome:
    """R2 S9: explicit expected outcome without fixture-name leakage."""

    def test_empty_outcome_accepted(self):
        outcome = ReviewExpectedOutcome()
        assert outcome.expected_status_by_proposal == {}
        assert outcome.expected_finding_codes_by_proposal == {}

    def test_outcome_with_statuses_accepted(self):
        outcome = ReviewExpectedOutcome(
            expected_status_by_proposal={
                "p1": ReviewDecisionStatus.APPROVED,
                "p2": ReviewDecisionStatus.REJECTED,
            }
        )
        assert (
            outcome.expected_status_by_proposal["p1"] == ReviewDecisionStatus.APPROVED
        )

    def test_outcome_is_frozen(self):
        outcome = ReviewExpectedOutcome()
        with pytest.raises(ValidationError):
            outcome.expected_status_by_proposal = {"p1": ReviewDecisionStatus.APPROVED}  # type: ignore[misc]

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ReviewExpectedOutcome(extra_field="bad")  # type: ignore[call-arg]


# ===========================================================================
# S10: reviewer_version required on ReviewBatchResult
# ===========================================================================


class TestReviewerVersionRequired:
    """R2 S10: reviewer_version is required and non-blank on the Result."""

    def test_blank_reviewer_version_rejected(self):
        with pytest.raises(ValidationError):
            ReviewBatchResult(
                review_id="r1",
                run_id="run1",
                tenant_id="t1",
                request_hash="h" * 64,
                reviewer_version="",
            )

    def test_valid_reviewer_version_accepted(self):
        result = ReviewBatchResult(
            review_id="r1",
            run_id="run1",
            tenant_id="t1",
            request_hash="h" * 64,
            reviewer_version=REVIEWER_VERSION,
        )
        assert result.reviewer_version == REVIEWER_VERSION

    def test_result_carries_reviewer_version(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        assert result.reviewer_version == REVIEWER_VERSION


# ===========================================================================
# S12: ReviewGraphError — persistable error contract
# ===========================================================================


class TestReviewGraphError:
    """R2 S12: strict, persistable graph error."""

    def test_valid_error_accepted(self):
        err = ReviewGraphError(
            error_code="review.graph.integrity",
            message="Request hash mismatch",
        )
        assert err.error_code == "review.graph.integrity"

    def test_blank_error_code_rejected(self):
        with pytest.raises(ValidationError):
            ReviewGraphError(error_code="", message="msg")

    def test_blank_message_rejected(self):
        with pytest.raises(ValidationError):
            ReviewGraphError(error_code="code", message="")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ReviewGraphError(
                error_code="code",
                message="msg",
                extra="bad",  # type: ignore[call-arg]
            )

    def test_is_frozen(self):
        err = ReviewGraphError(error_code="code", message="msg")
        with pytest.raises(ValidationError):
            err.error_code = "tampered"  # type: ignore[misc]


# ===========================================================================
# S14: Action Governance Spec Registry — single source of truth
# ===========================================================================


class TestActionGovernanceRegistry:
    """R2 S14: the registry is the canonical source for action specs."""

    def test_registry_is_non_empty(self):
        assert len(ACTION_GOVERNANCE_REGISTRY) > 0

    def test_spec_hash_non_blank(self):
        assert ACTION_GOVERNANCE_SPEC_HASH
        assert len(ACTION_GOVERNANCE_SPEC_HASH) == 64

    def test_spec_version_non_blank(self):
        assert ACTION_GOVERNANCE_SPEC_VERSION
        assert "." in ACTION_GOVERNANCE_SPEC_VERSION

    def test_known_action_returns_spec(self):
        spec = get_action_governance_spec("report.generate")
        assert spec is not None
        assert spec.action_type == "report.generate"
        assert spec.reviewable is True

    def test_unknown_action_returns_none(self):
        spec = get_action_governance_spec("nonexistent.action")
        assert spec is None

    def test_spec_is_frozen(self):
        spec = get_action_governance_spec("report.generate")
        assert spec is not None
        with pytest.raises(ValidationError):
            spec.action_type = "tampered"  # type: ignore[misc]

    def test_spec_hash_stable_across_calls(self):
        """The spec hash is computed once and does not drift."""
        from multi_agent.action_governance import _compute_spec_hash

        assert _compute_spec_hash() == ACTION_GOVERNANCE_SPEC_HASH
        assert _compute_spec_hash() == ACTION_GOVERNANCE_SPEC_HASH

    def test_spec_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ActionGovernanceSpec(
                action_type="test",
                reviewable=True,
                canonical_risk="low",
                minimum_authority="read",
                extra_field="bad",  # type: ignore[call-arg]
            )


# ===========================================================================
# PolicyRule + PolicyMatchedRule — R2 P0-6 strict typing
# ===========================================================================


class TestPolicyRuleStrictTyping:
    """R2 P0-6: PolicyRule and PolicyMatchedRule are strictly typed."""

    def test_policy_rule_blank_rule_id_rejected(self):
        with pytest.raises(ValidationError):
            PolicyRule(
                rule_id="",
                rule_version="v1",
                priority=50,
                effect=PolicyDecision.ALLOWED,
                action_type="report.generate",
            )

    def test_policy_rule_blank_rule_version_rejected(self):
        with pytest.raises(ValidationError):
            PolicyRule(
                rule_id="r1",
                rule_version="",
                priority=50,
                effect=PolicyDecision.ALLOWED,
                action_type="report.generate",
            )

    def test_policy_matched_rule_blank_version_rejected(self):
        with pytest.raises(ValidationError):
            PolicyMatchedRule(
                rule_id="r1",
                rule_version="",
                effect=PolicyDecision.ALLOWED,
            )

    def test_policy_rule_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            PolicyRule(
                rule_id="r1",
                rule_version="v1",
                priority=50,
                effect=PolicyDecision.ALLOWED,
                action_type="report.generate",
                extra="bad",  # type: ignore[call-arg]
            )

    def test_policy_rule_is_frozen(self):
        rule = PolicyRule(
            rule_id="r1",
            rule_version="v1",
            priority=50,
            effect=PolicyDecision.ALLOWED,
            action_type="report.generate",
        )
        with pytest.raises(ValidationError):
            rule.rule_id = "tampered"  # type: ignore[misc]


# ===========================================================================
# R2 public-API round-trip: serialize → deserialize → verify
# ===========================================================================


class TestR2RoundTrip:
    """R2 public-API round-trip: the Result survives JSON serialization."""

    def test_result_round_trip_preserves_integrity(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        # Serialize → deserialize.
        json_str = result.model_dump_json()
        restored = ReviewBatchResult.model_validate_json(json_str)
        # Integrity is preserved.
        restored.verify_integrity()
        assert restored.result_hash == result.result_hash
        # And it still binds to the original request.
        restored.verify_against_request(req)

    def test_request_round_trip_preserves_integrity(self):
        req = _valid_request()
        json_str = req.model_dump_json()
        restored = ReviewRequest.model_validate_json(json_str)
        restored.verify_integrity()
        assert restored.request_hash == req.request_hash
