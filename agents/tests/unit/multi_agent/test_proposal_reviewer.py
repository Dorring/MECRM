"""Phase 5A Proposal Reviewer tests.

Covers (Phase 5A Section 17 — Authority, Policy, Conflict):

* READ-only agent proposing Write → rejected
* authority violation → rejected
* high-risk → needs_approval
* policy deny → rejected
* policy needs_input → needs_input
* deterministic policy replay consistent
* duplicate proposal → conflict (deduped)
* same-resource different-value → conflict
* input order does not affect result
* batch status priority
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ResultOriginSnapshot,
)
from multi_agent.policy import (
    DeterministicPolicyEvaluator,
    FakePolicyEvaluator,
    PolicyDecision,
    PolicyEvaluationResult,
)
from multi_agent.review_contracts import (
    CODE_POLICY_DENIED,
    CODE_POLICY_NEEDS_INPUT,
    PolicyContext,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewEvidenceSnapshot,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewProposalEnvelope,
    ReviewProposalSnapshot,
    ReviewRequest,
    REVIEW_SCHEMA_VERSION,
    TaskRecordSummary,
    TraceSummary,
    REVIEWER_VERSION,
)
from multi_agent.review_errors import InvalidReviewRequestError
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_evidence(
    evidence_id: str = "ev-001",
    *,
    tenant_id: str = "tenant-test",
    source_agent: str = "agent_test",
    evidence_type: EvidenceType = EvidenceType.CUSTOMER,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=tenant_id,
        source_agent=source_agent,
        content_hash="a" * 64,
        created_at=_TS,
    )


def _make_capability(
    agent_id: str = "agent_test",
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="test",
        domains=frozenset({"test"}),
        supported_tasks=frozenset({"test_task"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_customers"}),
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
    )


def _make_proposal(
    proposal_id: str = "prop-001",
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict | None = None,
    evidence_ids: list[str] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    requires_approval: bool = True,
    idempotency_key: str = "idem-key-0001",
    tenant_id: str = "tenant-test",
    created_by_agent: str = "agent_test",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity=target_entity,
        target_id=target_id,
        payload=payload or {},
        risk_level=risk_level,
        evidence_ids=evidence_ids or [],
        requires_approval=requires_approval,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


def _make_capability_binding(
    capability: AgentCapability,
    *,
    task_id: str = "task-test",
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
    run_id: str = "run-test-001",
    result_id: str = "r-test-001",
    task_id: str = "task-test",
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    aid = proposal.created_by_agent
    # R2.1 P0-1: Envelope carries a deep-frozen ReviewProposalSnapshot.
    snapshot = ReviewProposalSnapshot.from_proposal(proposal)
    # R2.1 P0-1: origin_hash MUST match the envelope's validator which
    # uses to_action_proposal().model_dump(mode="python").
    return ReviewProposalEnvelope(
        proposal=snapshot,
        run_id=run_id,
        result_id=result_id,
        task_id=task_id,
        agent_id=aid,
        agent_version=agent_version,
        origin_hash=stable_hash(
            {
                "proposal": snapshot.to_action_proposal().model_dump(mode="python"),
                "run_id": run_id,
                "result_id": result_id,
                "task_id": task_id,
                "agent_id": aid,
                "agent_version": agent_version,
            }
        ),
    )


def _make_run_identity(
    *,
    run_id: str = "run-test-001",
    tenant_id: str = "tenant-test",
    plan_hash: str = "plan-test-hash",
    registry_version: str = "registry-test-v1",
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


def _make_result_origin(
    proposal: ActionProposal,
    *,
    run_id: str = "run-test-001",
    tenant_id: str = "tenant-test",
    result_id: str = "r-test-001",
    task_id: str = "task-test",
    agent_id: str = "agent_test",
    agent_version: str = "1.0.0",
    evidence: list[Evidence] | None = None,
) -> ResultOriginSnapshot:
    """R2.1 P0-4: build a ResultOriginSnapshot whose origin_hash is
    verified on construction."""
    proposal_hashes: tuple[tuple[str, str], ...] = (
        (proposal.proposal_id, proposal.proposal_hash),
    )
    evidence_hashes_list: list[tuple[str, str]] = []
    for ev in evidence or []:
        if proposal.evidence_ids and ev.evidence_id in proposal.evidence_ids:
            evidence_hashes_list.append(
                (ev.evidence_id, compute_review_evidence_hash(ev))
            )
    evidence_hashes = tuple(evidence_hashes_list)
    origin_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "result_id": result_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_version": agent_version,
            "proposal_hashes": sorted(proposal_hashes),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
    return ResultOriginSnapshot(
        run_id=run_id,
        tenant_id=tenant_id,
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version=agent_version,
        proposal_hashes=proposal_hashes,
        evidence_hashes=evidence_hashes,
        origin_hash=origin_hash,
    )


def _make_request(
    proposals: list[ActionProposal],
    *,
    evidence: list[Evidence] | None = None,
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
    policy_context: PolicyContext | None = None,
    review_id: str = "review-test-001",
) -> ReviewRequest:
    # R2 P0-3: wrap evidence in ReviewEvidenceSnapshot
    raw_evidence = evidence or []
    evidence_snapshots = [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in raw_evidence
    ]
    # R2.1 P0-1: convert ActionProposal → ReviewProposalSnapshot.
    proposal_snapshots: tuple[ReviewProposalSnapshot, ...] = tuple(
        ReviewProposalSnapshot.from_proposal(p) for p in proposals
    )
    # R2.1 P0-3: default capability_binding for task-test so the
    # Reviewer's exact-task authority check passes.  Tests that need
    # a different authority pass their own capability_bindings.
    if capability_bindings is None:
        default_cap = _make_capability()
        capability_bindings = [_make_capability_binding(default_cap)]
    # R2.1 P0-4: result_origins are REQUIRED.  Each proposal gets a
    # unique result_id to avoid the duplicate-result_id rejection.
    # The envelope's result_id MUST match the corresponding
    # result_origin's result_id so the identity-uniqueness validator
    # can bind Envelope → ResultOrigin.
    result_ids = [f"r-test-{i:03d}" for i in range(len(proposals))]
    origins = [
        _make_result_origin(p, result_id=rid, evidence=raw_evidence)
        for p, rid in zip(proposals, result_ids)
    ]
    envelopes = [
        _make_envelope(p, result_id=rid) for p, rid in zip(proposals, result_ids)
    ]
    return ReviewRequest(
        review_id=review_id,
        run_id="run-test-001",
        tenant_id="tenant-test",
        plan_hash="plan-test-hash",
        registry_version="registry-test-v1",
        proposals=proposal_snapshots,
        evidence=evidence_snapshots,
        task_records=[
            TaskRecordSummary(
                task_id="task-test",
                agent_id="agent_test",
                status="completed",
            )
        ],
        trace=[TraceSummary(sequence=0, event_type="run_started")],
        capability_bindings=capability_bindings,
        proposal_envelopes=envelopes,
        result_origins=origins,
        policy_context=policy_context
        or PolicyContext(
            policy_version="test-v1",
            rules=[],
        ),
        run_identity=_make_run_identity(),
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


# ---------------------------------------------------------------------------
# Authority validation
# ---------------------------------------------------------------------------


class TestAuthorityValidation:
    @pytest.mark.asyncio
    async def test_read_agent_proposing_write_rejected(self):
        prop = _make_proposal(
            action_type="crm.tag.update",
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.MEDIUM,
            idempotency_key="idem-key-0001",
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(
                authority=AgentAuthority.READ,
                allowed_tools=frozenset({"crm_reader.get_customers"}),
            ),
        )
        req = _make_request(
            [prop],
            evidence=[ev],
            capability_bindings=[cap],
        )
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        review = result.proposal_reviews[0]
        assert review.status == ReviewDecisionStatus.REJECTED
        assert not review.authority_valid

    @pytest.mark.asyncio
    async def test_propose_agent_proposing_execute_rejected(self):
        prop = _make_proposal(
            action_type="account.delete",  # execute-only → denied by policy
            evidence_ids=["ev-001"],
            idempotency_key="idem-key-0001",
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(
                authority=AgentAuthority.PROPOSE,
                allowed_tools=frozenset(
                    {
                        "crm_reader.get_customers",
                        "crm_writer.propose",
                    }
                ),
            ),
        )
        req = _make_request(
            [prop],
            evidence=[ev],
            capability_bindings=[cap],
        )
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        review = result.proposal_reviews[0]
        assert review.status == ReviewDecisionStatus.REJECTED

    @pytest.mark.asyncio
    async def test_no_capability_snapshot_rejected(self):
        """R2.1 P0-3: a Request whose envelope task_id has no matching
        capability_binding is rejected at the Request boundary (not at
        review time) — the exact-task capability check is a structural
        invariant, not a per-Proposal decision."""
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        with pytest.raises(InvalidReviewRequestError):
            _make_request(
                [prop],
                evidence=[ev],
                capability_bindings=[],  # no snapshot
            )


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


class TestRiskClassification:
    @pytest.mark.asyncio
    async def test_high_risk_needs_approval(self):
        prop = _make_proposal(
            action_type="crm.owner.assign",
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.HIGH,
            idempotency_key="high-risk-key-0001",
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(
                authority=AgentAuthority.PROPOSE,
                allowed_tools=frozenset(
                    {
                        "crm_reader.get_customers",
                        "crm_writer.propose",
                    }
                ),
            ),
        )
        req = _make_request(
            [prop],
            evidence=[ev],
            capability_bindings=[cap],
        )
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        review = result.proposal_reviews[0]
        assert review.status == ReviewDecisionStatus.NEEDS_APPROVAL
        assert review.required_approval is True

    @pytest.mark.asyncio
    async def test_low_risk_approved(self):
        prop = _make_proposal(
            action_type="report.generate",
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.LOW,
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request(
            [prop],
            evidence=[ev],
            capability_bindings=[cap],
        )
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        review = result.proposal_reviews[0]
        assert review.status == ReviewDecisionStatus.APPROVED
        assert not review.required_approval


# ---------------------------------------------------------------------------
# Policy integration
# ---------------------------------------------------------------------------


class TestPolicyIntegration:
    @pytest.mark.asyncio
    async def test_policy_deny_rejected(self):
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        # Fake evaluator returns DENIED with the required finding.
        fake = FakePolicyEvaluator()
        fake.set(
            "prop-001",
            PolicyEvaluationResult(
                proposal_id="prop-001",
                decision=PolicyDecision.DENIED,
                policy_version="test-v1",
                findings=(
                    ReviewFinding(
                        finding_code=CODE_POLICY_DENIED,
                        severity=ReviewFindingSeverity.ERROR,
                        message="policy denied",
                        proposal_id="prop-001",
                    ),
                ),
            ),
        )
        req = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(req, policy_evaluator=fake)
        review = result.proposal_reviews[0]
        assert review.status == ReviewDecisionStatus.REJECTED
        assert not review.policy_valid

    @pytest.mark.asyncio
    async def test_policy_needs_input(self):
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        fake = FakePolicyEvaluator()
        fake.set(
            "prop-001",
            PolicyEvaluationResult(
                proposal_id="prop-001",
                decision=PolicyDecision.NEEDS_INPUT,
                policy_version="test-v1",
                findings=(
                    ReviewFinding(
                        finding_code=CODE_POLICY_NEEDS_INPUT,
                        severity=ReviewFindingSeverity.WARNING,
                        message="policy needs input",
                        proposal_id="prop-001",
                    ),
                ),
            ),
        )
        req = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(req, policy_evaluator=fake)
        review = result.proposal_reviews[0]
        assert review.status == ReviewDecisionStatus.NEEDS_INPUT

    @pytest.mark.asyncio
    async def test_deterministic_policy_replay(self):
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        r1 = await reviewer.review(req, policy_evaluator=DeterministicPolicyEvaluator())
        r2 = await reviewer.review(req, policy_evaluator=DeterministicPolicyEvaluator())
        assert r1.result_hash == r2.result_hash
        assert r1.model_dump_json() == r2.model_dump_json()


# ---------------------------------------------------------------------------
# Conflict / Duplicate handling
# ---------------------------------------------------------------------------


class TestConflictAndDuplicate:
    @pytest.mark.asyncio
    async def test_duplicate_proposal_marked_deduplicated(self):
        p1 = _make_proposal("prop-001", idempotency_key="shared-key-0001")
        p2 = _make_proposal("prop-002", idempotency_key="shared-key-0001")
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request([p1, p2], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        # R1: exact duplicates (same idempotency_key) are DEDUPLICATED,
        # not CONFLICT.  The deduped proposal (prop-002) is DEDUPLICATED.
        statuses = {r.proposal_id: r.status for r in result.proposal_reviews}
        assert statuses["prop-002"] == ReviewDecisionStatus.DEDUPLICATED
        assert result.batch_status == ReviewBatchStatus.DEDUPLICATED

    @pytest.mark.asyncio
    async def test_same_resource_conflict(self):
        p1 = _make_proposal(
            "prop-001",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.MEDIUM,
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "prop-002",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.MEDIUM,
            idempotency_key="key-0002",
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(
                authority=AgentAuthority.PROPOSE,
                allowed_tools=frozenset(
                    {
                        "crm_reader.get_customers",
                        "crm_writer.propose",
                    }
                ),
            ),
        )
        req = _make_request([p1, p2], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        statuses = {r.proposal_id: r.status for r in result.proposal_reviews}
        assert statuses["prop-001"] == ReviewDecisionStatus.CONFLICT
        assert statuses["prop-002"] == ReviewDecisionStatus.CONFLICT

    @pytest.mark.asyncio
    async def test_input_order_does_not_affect_result(self):
        p1 = _make_proposal(
            "prop-001",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.MEDIUM,
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "prop-002",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.MEDIUM,
            idempotency_key="key-0002",
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(
                authority=AgentAuthority.PROPOSE,
                allowed_tools=frozenset(
                    {
                        "crm_reader.get_customers",
                        "crm_writer.propose",
                    }
                ),
            ),
        )
        req1 = _make_request(
            [p1, p2], evidence=[ev], capability_bindings=[cap], review_id="review-001"
        )
        req2 = _make_request(
            [p2, p1], evidence=[ev], capability_bindings=[cap], review_id="review-002"
        )
        # review_id differs, so request_hash will differ — but the
        # batch_status and proposal_statuses must be identical.
        reviewer = ProposalReviewer()
        r1 = await reviewer.review(
            req1, policy_evaluator=DeterministicPolicyEvaluator()
        )
        r2 = await reviewer.review(
            req2, policy_evaluator=DeterministicPolicyEvaluator()
        )
        assert r1.batch_status == r2.batch_status
        s1 = {r.proposal_id: r.status for r in r1.proposal_reviews}
        s2 = {r.proposal_id: r.status for r in r2.proposal_reviews}
        assert s1 == s2


# ---------------------------------------------------------------------------
# Batch status
# ---------------------------------------------------------------------------


class TestBatchStatus:
    @pytest.mark.asyncio
    async def test_all_approved_batch_approved(self):
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        assert result.batch_status == ReviewBatchStatus.APPROVED

    @pytest.mark.asyncio
    async def test_one_rejected_batch_rejected(self):
        p_ok = _make_proposal(
            "prop-001",
            evidence_ids=["ev-001"],
            idempotency_key="ok-key-0001",
        )
        p_bad = _make_proposal(
            "prop-002",
            action_type="nonexistent.action",  # rejected
            evidence_ids=["ev-001"],
            idempotency_key="bad-key-0001",
        )
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request([p_ok, p_bad], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        assert result.batch_status == ReviewBatchStatus.REJECTED

    @pytest.mark.asyncio
    async def test_empty_request_batch_no_actions(self):
        # R2 S7: empty batch uses NO_ACTIONS (NOT APPROVED).
        req = _make_request([])
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        assert result.batch_status == ReviewBatchStatus.NO_ACTIONS
        # R2 S1: proposal_reviews is a tuple (deep immutability)
        assert result.proposal_reviews == ()


# ---------------------------------------------------------------------------
# Result integrity
# ---------------------------------------------------------------------------


class TestResultIntegrity:
    @pytest.mark.asyncio
    async def test_result_hash_stable(self):
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        r1 = await reviewer.review(req, policy_evaluator=DeterministicPolicyEvaluator())
        r2 = await reviewer.review(req, policy_evaluator=DeterministicPolicyEvaluator())
        assert r1.result_hash == r2.result_hash

    @pytest.mark.asyncio
    async def test_result_verify_integrity(self):
        prop = _make_proposal(evidence_ids=["ev-001"])
        ev = _make_evidence("ev-001")
        cap = _make_capability_binding(
            _make_capability(authority=AgentAuthority.READ),
        )
        req = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        reviewer = ProposalReviewer()
        result = await reviewer.review(
            req, policy_evaluator=DeterministicPolicyEvaluator()
        )
        # Should not raise
        result.verify_integrity()
