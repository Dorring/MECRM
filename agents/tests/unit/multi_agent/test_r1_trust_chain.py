"""Phase 5A R1 Trust-Chain Regression Tests.

24 tests (23 counter-examples + 1 public-API round-trip) covering the
8 P0 trust-chain defects closed by the R1 revision:

1. Fail-closed identity uniqueness (proposal_id, evidence_id, task_id,
   agent_id in capability_bindings, trace sequence, origin_hash).
2. Envelope ↔ Proposal bijection.
3. Canonical, order-invariant hashing.
4. Tamper detection (request_hash, result_hash, review_hash).
5. Aggregate policy priority (denied > needs_input > needs_approval).
6. DEDUPLICATED status for exact duplicates (not CONFLICT).
7. Semantic validation (status ↔ findings consistency).
8. Public API round-trip (serialize → deserialize → verify).

All async tests use ``asyncio.run()`` inside the test body — NOT
``@pytest.mark.asyncio`` — per the R1 spec.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
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
from multi_agent.execution import ExecutionCapabilitySnapshot
from multi_agent.review_contracts import (
    CODE_EVIDENCE_DANGLING,
    PolicyContext,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewEvidenceSnapshot,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewProposalEnvelope,
    ReviewRequest,
    REVIEW_SCHEMA_VERSION,
    TaskRecordSummary,
    TraceSummary,
    REVIEWER_VERSION,
    batch_status_priority,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    ReviewIntegrityError,
)
from multi_agent.policy import DeterministicPolicyEvaluator
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev-001",
    *,
    tenant_id: str = "tenant-tc",
    source_agent: str = "agent_tc",
    content_hash: str = "a" * 64,
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
    proposal_id: str = "prop-001",
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict[str, object] | None = None,
    evidence_ids: list[str] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    idempotency_key: str = "tc-idem-0001",
    tenant_id: str = "tenant-tc",
    created_by_agent: str = "agent_tc",
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
    agent_id: str = "agent_tc",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="tc",
        domains=frozenset({"d"}),
        supported_tasks=frozenset({"t"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_customers"}),
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
    )


def _make_capability_binding(
    capability: AgentCapability,
    *,
    task_id: str = "task-tc",
) -> ExecutionCapabilitySnapshot:
    return ExecutionCapabilitySnapshot(
        task_id=task_id,
        agent_id=capability.agent_id,
        agent_version=capability.version,
        capability=capability,
        binding_hash=stable_hash(
            {
                "task_id": task_id,
                "agent_id": capability.agent_id,
                "agent_version": capability.version,
                "capability": capability.model_dump(mode="python"),
            }
        ),
    )


def _make_envelope(
    proposal: ActionProposal,
    *,
    run_id: str = "run-tc",
    result_id: str = "r-tc",
    task_id: str = "task-tc",
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
    proposals: list[ActionProposal],
    *,
    evidence: list[Evidence] | None = None,
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
    proposal_envelopes: list[ReviewProposalEnvelope] | None = None,
    task_records: list[TaskRecordSummary] | None = None,
    trace: list[TraceSummary] | None = None,
    policy_context: PolicyContext | None = None,
    review_id: str = "review-tc-001",
) -> ReviewRequest:
    # R2 P0-3: wrap raw Evidence in ReviewEvidenceSnapshot before sending
    # to ReviewRequest so the snapshot_hash is verified by the contract.
    raw_evidence = evidence or []
    evidence_snapshots = [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in raw_evidence
    ]
    return ReviewRequest(
        review_id=review_id,
        run_id="run-tc",
        tenant_id="tenant-tc",
        plan_hash="plan-tc-hash",
        registry_version="registry-tc-v1",
        proposals=proposals,
        evidence=evidence_snapshots,
        task_records=task_records
        or [
            TaskRecordSummary(
                task_id="task-tc", agent_id="agent_tc", status="completed"
            )
        ],
        trace=trace or [TraceSummary(sequence=0, event_type="run_started")],
        capability_bindings=capability_bindings or [],
        proposal_envelopes=(
            proposal_envelopes
            if proposal_envelopes is not None
            else [_make_envelope(p) for p in proposals]
        ),
        policy_context=policy_context
        or PolicyContext(policy_version="tc-v1", rules=[]),
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


def _valid_request() -> ReviewRequest:
    """Build a minimal valid ReviewRequest for positive-path tests."""
    prop = _make_proposal("prop-ok-001", evidence_ids=["ev-ok-001"])
    ev = _make_evidence("ev-ok-001")
    cap = _make_capability_binding(_make_capability())
    return _make_request([prop], evidence=[ev], capability_bindings=[cap])


# ===========================================================================
# 1-7: Identity uniqueness — fail-closed
# ===========================================================================


class TestIdentityUniqueness:
    """R1 P0-1: any duplicate-with-different-content raises."""

    def test_duplicate_proposal_id_different_content_raises(self):
        p1 = _make_proposal("prop-dup-001", idempotency_key="key-a")
        p2 = _make_proposal("prop-dup-001", idempotency_key="key-b")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([p1, p2])

    def test_duplicate_proposal_id_same_content_raises(self):
        p1 = _make_proposal("prop-dup-002", idempotency_key="key-same")
        p2 = _make_proposal("prop-dup-002", idempotency_key="key-same")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([p1, p2])

    def test_duplicate_evidence_id_different_content_raises(self):
        # compute_review_evidence_hash excludes the self-referential
        # content_hash field, so differ in source_agent to produce
        # genuinely distinct review hashes.
        ev1 = _make_evidence("ev-dup", source_agent="agent_a")
        ev2 = _make_evidence("ev-dup", source_agent="agent_b")
        prop = _make_proposal("prop-ev-dup-001")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], evidence=[ev1, ev2])

    def test_duplicate_task_id_raises(self):
        prop = _make_proposal("prop-task-dup-001")
        tr1 = TaskRecordSummary(task_id="task-dup", agent_id="a", status="ok")
        tr2 = TaskRecordSummary(task_id="task-dup", agent_id="b", status="ok")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], task_records=[tr1, tr2])

    def test_duplicate_task_id_in_bindings_raises(self):
        # R2 P0-2: capability_bindings are unique by task_id (NOT
        # agent_id) — the same Agent may legitimately execute multiple
        # Tasks in one Run.  Two bindings for the SAME task_id must
        # still raise.
        cap = _make_capability()
        b1 = _make_capability_binding(cap, task_id="task-dup")
        b2 = _make_capability_binding(cap, task_id="task-dup")
        prop = _make_proposal("prop-bind-dup-001")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], capability_bindings=[b1, b2])

    def test_same_agent_id_different_task_id_allowed(self):
        # R2 P0-2: the same Agent may execute multiple Tasks — two
        # bindings with the same agent_id but different task_id MUST
        # NOT raise.
        cap = _make_capability()
        b1 = _make_capability_binding(cap, task_id="task-a")
        b2 = _make_capability_binding(cap, task_id="task-b")
        prop = _make_proposal("prop-bind-dup-001")
        # Should not raise.
        _make_request([prop], capability_bindings=[b1, b2])

    def test_duplicate_trace_sequence_raises(self):
        prop = _make_proposal("prop-trace-dup-001")
        t1 = TraceSummary(sequence=0, event_type="a")
        t2 = TraceSummary(sequence=0, event_type="b")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], trace=[t1, t2])

    def test_duplicate_origin_hash_raises(self):
        p1 = _make_proposal("prop-oh-001", idempotency_key="key-oh-1")
        p2 = _make_proposal("prop-oh-002", idempotency_key="key-oh-2")
        env1 = _make_envelope(p1)
        # Create env2 with the correct origin_hash for p2, then tamper
        # to give it the same origin_hash as env1 (bypassing the
        # frozen-model validator via object.__setattr__).  Pydantic
        # re-validates nested envelope instances inside ReviewRequest
        # and rejects the mismatched origin_hash.
        env2 = _make_envelope(p2)
        object.__setattr__(env2, "origin_hash", env1.origin_hash)
        with pytest.raises((InvalidReviewRequestError, ValidationError)):
            _make_request([p1, p2], proposal_envelopes=[env1, env2])


# ===========================================================================
# 8-9: Envelope ↔ Proposal bijection
# ===========================================================================


class TestEnvelopeBijection:
    """R1 P0-2: every proposal must have exactly one envelope."""

    def test_proposal_without_envelope_raises(self):
        prop = _make_proposal("prop-no-env-001")
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], proposal_envelopes=[])

    def test_envelope_without_proposal_raises(self):
        prop = _make_proposal("prop-env-orphan-001")
        other = _make_proposal("prop-other-002")
        env_for_other = _make_envelope(other)
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], proposal_envelopes=[env_for_other])


# ===========================================================================
# 10-12: Hash tamper detection
# ===========================================================================


class TestHashTamperDetection:
    """R1 P0-3: hash mismatch is always detected."""

    def test_request_hash_tamper_detected(self):
        req = _valid_request()
        object.__setattr__(req, "request_hash", "tampered" * 8)
        with pytest.raises(ReviewIntegrityError):
            req.verify_integrity()

    def test_result_hash_tamper_detected(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        object.__setattr__(result, "result_hash", "tampered" * 8)
        with pytest.raises(ReviewIntegrityError):
            result.verify_integrity()

    def test_review_hash_tamper_detected(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        review = result.proposal_reviews[0]
        object.__setattr__(review, "review_hash", "tampered" * 8)
        with pytest.raises(ReviewIntegrityError):
            review.verify_integrity()


# ===========================================================================
# 13-14: Order-invariant hashing
# ===========================================================================


class TestOrderInvariantHashing:
    """R1 P0-4: reordering inputs does not change the hash."""

    def test_request_hash_order_invariant(self):
        p1 = _make_proposal("prop-ord-001", idempotency_key="ord-key-1")
        p2 = _make_proposal("prop-ord-002", idempotency_key="ord-key-2")
        ev1 = _make_evidence("ev-ord-001")
        ev2 = _make_evidence("ev-ord-002")
        cap = _make_capability_binding(_make_capability())

        req_a = _make_request(
            [p1, p2],
            evidence=[ev1, ev2],
            capability_bindings=[cap],
            review_id="review-ord-001",
        )
        req_b = _make_request(
            [p2, p1],
            evidence=[ev2, ev1],
            capability_bindings=[cap],
            review_id="review-ord-001",
        )
        assert req_a.request_hash == req_b.request_hash

    def test_result_hash_order_invariant(self):
        p1 = _make_proposal("prop-res-ord-001", idempotency_key="res-key-1")
        p2 = _make_proposal("prop-res-ord-002", idempotency_key="res-key-2")
        ev = _make_evidence("ev-res-ord")
        cap = _make_capability_binding(_make_capability())

        req_a = _make_request(
            [p1, p2],
            evidence=[ev],
            capability_bindings=[cap],
            review_id="review-res-ord",
        )
        req_b = _make_request(
            [p2, p1],
            evidence=[ev],
            capability_bindings=[cap],
            review_id="review-res-ord",
        )
        r1 = asyncio.run(
            ProposalReviewer().review(
                req_a, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        r2 = asyncio.run(
            ProposalReviewer().review(
                req_b, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        assert r1.result_hash == r2.result_hash


# ===========================================================================
# 15-16: DEDUPLICATED vs CONFLICT
# ===========================================================================


class TestDeduplicatedVsConflict:
    """R1 P0-5: exact duplicates are DEDUPLICATED, real conflicts are CONFLICT."""

    def test_exact_duplicate_is_deduplicated_not_conflict(self):
        p1 = _make_proposal("prop-dedup-001", idempotency_key="dedup-key")
        p2 = _make_proposal("prop-dedup-002", idempotency_key="dedup-key")
        ev = _make_evidence("ev-dedup")
        cap = _make_capability_binding(_make_capability())
        req = _make_request([p1, p2], evidence=[ev], capability_bindings=[cap])
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        statuses = {r.proposal_id: r.status for r in result.proposal_reviews}
        assert statuses["prop-dedup-002"] == ReviewDecisionStatus.DEDUPLICATED
        assert statuses["prop-dedup-002"] != ReviewDecisionStatus.CONFLICT

    def test_real_conflict_is_conflict_not_deduplicated(self):
        p1 = _make_proposal(
            "prop-conf-001",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="conf-key-1",
        )
        p2 = _make_proposal(
            "prop-conf-002",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            idempotency_key="conf-key-2",
        )
        ev = _make_evidence("ev-conf")
        cap = _make_capability_binding(
            _make_capability(
                authority=AgentAuthority.PROPOSE,
                allowed_tools=frozenset(
                    {"crm_reader.get_customers", "crm_writer.propose"}
                ),
            )
        )
        req = _make_request(
            [p1, p2],
            evidence=[ev],
            capability_bindings=[cap],
        )
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        statuses = {r.proposal_id: r.status for r in result.proposal_reviews}
        assert statuses["prop-conf-001"] == ReviewDecisionStatus.CONFLICT
        assert statuses["prop-conf-002"] == ReviewDecisionStatus.CONFLICT


# ===========================================================================
# 17-19: Aggregate policy priority
# ===========================================================================


class TestAggregatePolicyPriority:
    """R1 P0-6: denied > needs_input > needs_approval > allowed."""

    def test_denied_overrides_needs_approval(self):
        assert batch_status_priority(
            ReviewBatchStatus.REJECTED
        ) > batch_status_priority(ReviewBatchStatus.NEEDS_APPROVAL)

    def test_denied_overrides_needs_input(self):
        assert batch_status_priority(
            ReviewBatchStatus.REJECTED
        ) > batch_status_priority(ReviewBatchStatus.NEEDS_INPUT)

    def test_needs_input_overrides_needs_approval(self):
        assert batch_status_priority(
            ReviewBatchStatus.NEEDS_INPUT
        ) > batch_status_priority(ReviewBatchStatus.NEEDS_APPROVAL)


# ===========================================================================
# 20-23: Semantic validation
# ===========================================================================


def _make_finding(
    *,
    code: str = CODE_EVIDENCE_DANGLING,
    severity: ReviewFindingSeverity = ReviewFindingSeverity.WARNING,
    proposal_id: str = "prop-sem-001",
) -> ReviewFinding:
    return ReviewFinding(
        finding_code=code,
        severity=severity,
        message="semantic test finding",
        proposal_id=proposal_id,
    )


class TestSemanticValidation:
    """R1 P0-7: status ↔ findings consistency is enforced."""

    def test_approved_with_error_finding_raises(self):
        review = ProposalReview(
            proposal_id="prop-sem-001",
            status=ReviewDecisionStatus.APPROVED,
            findings=[_make_finding(severity=ReviewFindingSeverity.ERROR)],
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()

    def test_rejected_without_rejection_code_raises(self):
        review = ProposalReview(
            proposal_id="prop-sem-002",
            status=ReviewDecisionStatus.REJECTED,
            findings=[_make_finding(code=CODE_EVIDENCE_DANGLING)],
            authority_valid=False,
            policy_valid=True,
            idempotency_valid=True,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()

    def test_needs_input_without_missing_finding_raises(self):
        review = ProposalReview(
            proposal_id="prop-sem-003",
            status=ReviewDecisionStatus.NEEDS_INPUT,
            findings=[_make_finding(code=CODE_EVIDENCE_DANGLING)],
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()

    def test_deduplicated_without_dedup_code_raises(self):
        review = ProposalReview(
            proposal_id="prop-sem-004",
            status=ReviewDecisionStatus.DEDUPLICATED,
            findings=[_make_finding(code=CODE_EVIDENCE_DANGLING)],
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()


# ===========================================================================
# 24: Public API round-trip
# ===========================================================================


class TestPublicApiRoundTrip:
    """R1 P0-8: serialize → deserialize → verify_integrity + verify_semantics."""

    def test_public_api_round_trip(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )

        # Serialize to JSON and back.
        json_str = result.model_dump_json()
        restored = ReviewBatchResult.model_validate_json(json_str)

        # Hashes must match.
        assert restored.result_hash == result.result_hash
        assert restored.request_hash == result.request_hash

        # Integrity + semantics must pass on the restored copy.
        restored.verify_integrity()
        restored.verify_semantics()

        # Per-proposal reviews also survive round-trip.
        for orig, rt in zip(result.proposal_reviews, restored.proposal_reviews):
            assert orig.review_hash == rt.review_hash
            rt.verify_integrity()
            rt.verify_semantics()
