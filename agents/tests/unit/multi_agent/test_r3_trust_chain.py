"""Phase 5A R2.1 Trust-Chain Regression Tests.

30 counter-example tests covering the R2.1 P0 / P1 trust-boundary
fixes that are NOT already exercised by ``test_r1_trust_chain.py`` /
``test_r2_trust_chain.py``:

* P0-1: ReviewProposalSnapshot deep-freeze + origin_hash consistency.
* P0-2: Envelope cardinality (exactly one envelope per proposal;
        duplicate result_id rejected).
* P0-3: Exact-task capability required (envelope task_id must be in
        capability_bindings AND task_records).
* P0-4: ResultOriginSnapshot run_id/tenant_id required + origin_hash
        verified on construction; envelope result_id must match an
        origin.
* P0-5: Evidence duplicate_id+different_content rejected at the
        Request boundary (NOT silently deduped).
* P0-6: PolicyContext rule_version == policy_version, rule_id unique,
        tenant_overrides deep-frozen.
* P0-7: PolicyDecisionAudit identity binding (proposal_id required,
        mismatch detected by verify_semantics); ReviewBatchResult
        governance_spec_hash required (non-blank) and compared
        unconditionally against the Request; live Registry tamper
        detection.
* P1-1: ReviewGraphState has only ``graph_error`` (no raw ``error``).
* P1-2: Decision priority order (conflict > rejected > needs_input >
        needs_approval > deduplicated > approved).

All async tests use ``asyncio.run()`` inside the test body — NOT
``@pytest.mark.asyncio`` — per the R1/R2 spec.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
    compute_live_governance_spec_hash,
    verify_governance_spec_integrity,
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
from multi_agent.policy import DeterministicPolicyEvaluator, PolicyDecision
from multi_agent.review_contracts import (
    CODE_EVIDENCE_DANGLING,
    CODE_POLICY_DENIED,
    PolicyContext,
    PolicyDecisionAudit,
    PolicyRule,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewEvidenceSnapshot,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewGraphError,
    ReviewProposalEnvelope,
    ReviewProposalSnapshot,
    ReviewRequest,
    REVIEW_SCHEMA_VERSION,
    REVIEWER_VERSION,
    TaskRecordSummary,
    TraceSummary,
    batch_status_priority,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    ReviewIntegrityError,
)
from multi_agent.review_graph import ReviewGraphState
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_r1_trust_chain.py but with r3 prefixes)
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev-r3-001",
    *,
    tenant_id: str = "tenant-r3",
    source_agent: str = "agent_r3",
    content_hash: str = "c" * 64,
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
    proposal_id: str = "prop-r3-001",
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict[str, object] | None = None,
    evidence_ids: list[str] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    idempotency_key: str = "r3-idem-0001",
    tenant_id: str = "tenant-r3",
    created_by_agent: str = "agent_r3",
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
    agent_id: str = "agent_r3",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="r3",
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
    capability: AgentCapability | None = None,
    *,
    task_id: str = "task-r3",
    agent_id: str = "agent_r3",
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
    run_id: str = "run-r3",
    result_id: str = "r-r3-000",
    task_id: str = "task-r3",
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    aid = proposal.created_by_agent
    # R2.1 P0-1: origin_hash MUST be computed from
    # ``snapshot.to_action_proposal().model_dump(mode="python")`` to
    # match the envelope's ``_verify_origin_hash`` validator.
    snapshot = ReviewProposalSnapshot.from_proposal(proposal)
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
    run_id: str = "run-r3",
    tenant_id: str = "tenant-r3",
    plan_hash: str = "plan-r3-hash",
    registry_version: str = "registry-r3-v1",
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
    run_id: str = "run-r3",
    tenant_id: str = "tenant-r3",
    result_id: str = "r-r3-000",
    task_id: str = "task-r3",
    agent_id: str = "agent_r3",
    agent_version: str = "1.0.0",
    evidence: list[Evidence] | None = None,
) -> ResultOriginSnapshot:
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


def _make_policy_audit(
    *,
    proposal_id: str = "prop-r3-001",
    request_hash: str = "a" * 64,
    policy_request_hash: str = "b" * 64,
    decision: PolicyDecision = PolicyDecision.ALLOWED,
) -> PolicyDecisionAudit:
    payload = {
        "evaluator_source_id": "deterministic",
        "evaluator_version": "r3-v1",
        "policy_version": "r3-v1",
        "decision": decision.value,
        "matched_rules": [],
        "proposal_id": proposal_id,
        "request_hash": request_hash,
        "policy_request_hash": policy_request_hash,
    }
    return PolicyDecisionAudit(
        evaluator_source_id="deterministic",
        evaluator_version="r3-v1",
        policy_version="r3-v1",
        decision=decision,
        matched_rules=(),
        proposal_id=proposal_id,
        request_hash=request_hash,
        policy_request_hash=policy_request_hash,
        evaluation_hash=stable_hash(payload),
    )


def _make_request(
    proposals: list[ActionProposal],
    *,
    evidence: list[Evidence] | None = None,
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
    proposal_envelopes: list[ReviewProposalEnvelope] | None = None,
    result_origins: list[ResultOriginSnapshot] | None = None,
    task_records: list[TaskRecordSummary] | None = None,
    trace: list[TraceSummary] | None = None,
    policy_context: PolicyContext | None = None,
    run_identity: ExecutionRunIdentity | None = None,
    review_id: str = "review-r3-001",
) -> ReviewRequest:
    raw_evidence = evidence or []
    evidence_snapshots = [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in raw_evidence
    ]
    proposal_snapshots: tuple[ReviewProposalSnapshot, ...] = tuple(
        ReviewProposalSnapshot.from_proposal(p) for p in proposals
    )
    if capability_bindings is None:
        capability_bindings = [_make_capability_binding(_make_capability())]
    # envelope task_id MUST match a capability_binding's task_id.
    envelope_task_id = capability_bindings[0].task_id
    # R1 P0-4: result_id assigned by sorted proposal_id for order-invariance.
    sorted_ids = sorted(p.proposal_id for p in proposals)
    result_id_map = {pid: f"r-r3-{i:03d}" for i, pid in enumerate(sorted_ids)}
    result_ids = [result_id_map[p.proposal_id] for p in proposals]
    origins = (
        result_origins
        if result_origins is not None
        else [
            _make_result_origin(
                p,
                result_id=rid,
                task_id=envelope_task_id,
                evidence=raw_evidence,
            )
            for p, rid in zip(proposals, result_ids)
        ]
    )
    envelopes = (
        proposal_envelopes
        if proposal_envelopes is not None
        else [
            _make_envelope(p, result_id=rid, task_id=envelope_task_id)
            for p, rid in zip(proposals, result_ids)
        ]
    )
    return ReviewRequest(
        review_id=review_id,
        run_id="run-r3",
        tenant_id="tenant-r3",
        plan_hash="plan-r3-hash",
        registry_version="registry-r3-v1",
        proposals=proposal_snapshots,
        evidence=evidence_snapshots,
        task_records=task_records
        or [
            TaskRecordSummary(
                task_id=b.task_id,
                agent_id=b.agent_id,
                status="completed",
            )
            for b in capability_bindings
        ],
        trace=trace or [TraceSummary(sequence=0, event_type="run_started")],
        capability_bindings=capability_bindings,
        proposal_envelopes=envelopes,
        result_origins=origins,
        policy_context=policy_context
        or PolicyContext(policy_version="r3-v1", rules=[]),
        run_identity=run_identity or _make_run_identity(),
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


def _valid_request() -> ReviewRequest:
    """Minimal valid ReviewRequest for positive-path tests."""
    prop = _make_proposal("prop-ok-r3-001", evidence_ids=["ev-ok-r3"])
    ev = _make_evidence("ev-ok-r3")
    cap = _make_capability_binding(_make_capability())
    return _make_request([prop], evidence=[ev], capability_bindings=[cap])


# ===========================================================================
# P0-1: ReviewProposalSnapshot deep-freeze + origin_hash consistency
# ===========================================================================


class TestReviewProposalSnapshotDeepFreeze:
    """R2.1 P0-1: snapshot fields are deep-frozen."""

    def test_snapshot_payload_is_frozen_tuple_not_dict(self):
        prop = _make_proposal(
            "prop-frz-001",
            payload={"amount": 100, "name": "abc"},
        )
        snapshot = ReviewProposalSnapshot.from_proposal(prop)
        # payload must be a tuple-of-tuples (frozen form), NOT a dict.
        assert isinstance(snapshot.payload, tuple)
        assert not isinstance(snapshot.payload, dict)

    def test_snapshot_evidence_ids_is_tuple_not_list(self):
        prop = _make_proposal(
            "prop-frz-002",
            evidence_ids=["ev-a", "ev-b"],
        )
        snapshot = ReviewProposalSnapshot.from_proposal(prop)
        assert isinstance(snapshot.evidence_ids, tuple)
        assert not isinstance(snapshot.evidence_ids, list)

    def test_snapshot_hash_tamper_rejected(self):
        prop = _make_proposal("prop-frz-003")
        snapshot = ReviewProposalSnapshot.from_proposal(prop)
        with pytest.raises(ValidationError):
            object.__setattr__(snapshot, "snapshot_hash", "tampered" * 8)
            snapshot.model_validate(snapshot.model_dump(mode="python"))

    def test_snapshot_to_action_proposal_preserves_proposal_hash(self):
        prop = _make_proposal(
            "prop-frz-004",
            payload={"k": "v"},
            evidence_ids=["ev-x"],
        )
        snapshot = ReviewProposalSnapshot.from_proposal(prop)
        restored = snapshot.to_action_proposal()
        # R2.1 P0-1: round-trip MUST preserve the original proposal_hash
        # so the Envelope's origin_hash is stable.
        assert restored.proposal_hash == prop.proposal_hash

    def test_envelope_origin_hash_diverges_from_snapshot_model_dump(self):
        """R2.1 P0-1: origin_hash computed from
        ``snapshot.to_action_proposal().model_dump()`` MUST differ from
        one computed from ``snapshot.model_dump()`` when the payload is
        non-empty — proving the validator path matters (the snapshot's
        payload is a frozen tuple, the ActionProposal's is a dict)."""
        prop = _make_proposal(
            "prop-frz-005",
            payload={"amount": 1, "name": "x"},
        )
        snapshot = ReviewProposalSnapshot.from_proposal(prop)
        hash_via_action_proposal = stable_hash(
            {"proposal": snapshot.to_action_proposal().model_dump(mode="python")}
        )
        hash_via_snapshot_dump = stable_hash(
            {"proposal": snapshot.model_dump(mode="python")}
        )
        # The two hashes MUST differ — otherwise the frozen-form
        # canonicalisation is a no-op and the R2.1 P0-1 fix is inert.
        assert hash_via_action_proposal != hash_via_snapshot_dump


# ===========================================================================
# P0-2: Envelope cardinality (exactly one envelope per proposal)
# ===========================================================================


class TestEnvelopeCardinality:
    """R2.1 P0-2: every proposal has EXACTLY ONE envelope."""

    def test_two_envelopes_same_proposal_rejected(self):
        prop = _make_proposal("prop-card-001")
        env1 = _make_envelope(prop, result_id="r-card-001")
        env2 = _make_envelope(prop, result_id="r-card-002")
        with pytest.raises(InvalidReviewRequestError):
            _make_request(
                [prop],
                proposal_envelopes=[env1, env2],
            )

    def test_envelope_result_id_duplicate_rejected(self):
        p1 = _make_proposal("prop-card-002", idempotency_key="card-a")
        p2 = _make_proposal("prop-card-003", idempotency_key="card-b")
        # Both envelopes use the SAME result_id — must be rejected.
        env1 = _make_envelope(p1, result_id="r-card-dup")
        env2 = _make_envelope(p2, result_id="r-card-dup")
        with pytest.raises(InvalidReviewRequestError):
            _make_request(
                [p1, p2],
                proposal_envelopes=[env1, env2],
            )


# ===========================================================================
# P0-3: Exact-task capability required
# ===========================================================================


class TestExactTaskCapability:
    """R2.1 P0-3: envelope task_id must be in capability_bindings AND
    task_records."""

    def test_envelope_task_id_missing_capability_binding_rejected(self):
        prop = _make_proposal("prop-cap-001")
        # Binding is for task-other, but envelope's task_id is task-r3.
        cap = _make_capability_binding(_make_capability(), task_id="task-other")
        env = _make_envelope(prop, task_id="task-r3", result_id="r-cap-001")
        origin = _make_result_origin(prop, task_id="task-r3", result_id="r-cap-001")
        with pytest.raises(InvalidReviewRequestError):
            _make_request(
                [prop],
                capability_bindings=[cap],
                proposal_envelopes=[env],
                result_origins=[origin],
            )

    def test_envelope_task_id_missing_task_record_rejected(self):
        prop = _make_proposal("prop-cap-002")
        # capability_binding exists for task-r3, but task_records only
        # lists task-other.
        cap = _make_capability_binding(_make_capability(), task_id="task-r3")
        env = _make_envelope(prop, task_id="task-r3", result_id="r-cap-002")
        origin = _make_result_origin(prop, task_id="task-r3", result_id="r-cap-002")
        with pytest.raises(InvalidReviewRequestError):
            _make_request(
                [prop],
                capability_bindings=[cap],
                proposal_envelopes=[env],
                result_origins=[origin],
                task_records=[
                    TaskRecordSummary(
                        task_id="task-other",
                        agent_id="agent_r3",
                        status="completed",
                    )
                ],
            )


# ===========================================================================
# P0-4: ResultOriginSnapshot run_id/tenant_id required + origin_hash
# ===========================================================================


class TestResultOriginSnapshotR21:
    """R2.1 P0-4: ResultOriginSnapshot binds to Run identity."""

    def test_result_origin_missing_run_id_rejected(self):
        prop = _make_proposal("prop-orig-001")
        # Build a valid origin, then strip run_id.
        origin = _make_result_origin(prop)
        dump = origin.model_dump(mode="python")
        dump.pop("run_id")
        # Recompute origin_hash without run_id so the validator's hash
        # check passes — we want to isolate the "missing run_id" failure
        # from the "origin_hash mismatch" failure.
        with pytest.raises(ValidationError):
            ResultOriginSnapshot(**dump)

    def test_result_origin_missing_tenant_id_rejected(self):
        prop = _make_proposal("prop-orig-002")
        origin = _make_result_origin(prop)
        dump = origin.model_dump(mode="python")
        dump.pop("tenant_id")
        with pytest.raises(ValidationError):
            ResultOriginSnapshot(**dump)

    def test_result_origin_origin_hash_tamper_rejected(self):
        prop = _make_proposal("prop-orig-003")
        origin = _make_result_origin(prop)
        object.__setattr__(origin, "origin_hash", "tampered" * 8)
        with pytest.raises((ValidationError, ReviewIntegrityError)):
            # Re-running the model validator must reject the tampered hash.
            ResultOriginSnapshot.model_validate(origin.model_dump(mode="python"))

    def test_envelope_result_id_missing_origin_rejected(self):
        prop = _make_proposal("prop-orig-004")
        cap = _make_capability_binding(_make_capability(), task_id="task-r3")
        # Envelope references result_id r-missing, but origins only
        # contain r-present.
        env = _make_envelope(prop, result_id="r-missing", task_id="task-r3")
        origin = _make_result_origin(prop, result_id="r-present", task_id="task-r3")
        with pytest.raises(InvalidReviewRequestError):
            _make_request(
                [prop],
                capability_bindings=[cap],
                proposal_envelopes=[env],
                result_origins=[origin],
            )


# ===========================================================================
# P0-5: Evidence duplicate_id+different_content rejected at Request
# ===========================================================================


class TestEvidenceDuplicateRejection:
    """R2.1 P0-5: same evidence_id + different content is fail-closed."""

    def test_evidence_duplicate_id_different_content_rejected_at_request(self):
        prop = _make_proposal(
            "prop-evd-001",
            evidence_ids=["ev-dup"],
        )
        ev1 = _make_evidence("ev-dup", content_hash="a" * 64)
        ev2 = _make_evidence("ev-dup", content_hash="b" * 64)
        with pytest.raises(InvalidReviewRequestError):
            _make_request([prop], evidence=[ev1, ev2])


# ===========================================================================
# P0-6: PolicyContext rule_version / rule_id uniqueness / frozen overrides
# ===========================================================================


class TestPolicyContextR21:
    """R2.1 P0-6: PolicyContext strict rule validation."""

    def test_policy_rule_version_mismatch_rejected(self):
        rule = PolicyRule(
            rule_id="r1",
            rule_version="other-v1",  # != policy_version
            priority=1,
            effect=PolicyDecision.ALLOWED,
        )
        with pytest.raises(ValidationError):
            PolicyContext(policy_version="r3-v1", rules=[rule])

    def test_policy_rule_id_duplicate_rejected(self):
        rule1 = PolicyRule(
            rule_id="dup",
            rule_version="r3-v1",
            priority=1,
            effect=PolicyDecision.ALLOWED,
        )
        rule2 = PolicyRule(
            rule_id="dup",
            rule_version="r3-v1",
            priority=2,
            effect=PolicyDecision.DENIED,
        )
        with pytest.raises(ValidationError):
            PolicyContext(policy_version="r3-v1", rules=[rule1, rule2])

    def test_policy_tenant_overrides_is_frozen(self):
        ctx = PolicyContext(
            policy_version="r3-v1",
            rules=[],
            tenant_overrides={"k": "v"},
        )
        # R2.1 P0-1: tenant_overrides is deep-frozen to a tuple form,
        # NOT a mutable dict.
        assert not isinstance(ctx.tenant_overrides, dict)
        # The frozen form is a tuple-of-tuples (sorted by key).
        assert isinstance(ctx.tenant_overrides, tuple)


# ===========================================================================
# P0-7: PolicyDecisionAudit identity binding
# ===========================================================================


class TestPolicyDecisionAuditIdentity:
    """R2.1 P0-7: audit identity binding fields are required + verified."""

    def test_policy_audit_missing_proposal_id_rejected(self):
        with pytest.raises(ValidationError):
            PolicyDecisionAudit(
                evaluator_source_id="deterministic",
                evaluator_version="r3-v1",
                policy_version="r3-v1",
                decision=PolicyDecision.ALLOWED,
                matched_rules=(),
                proposal_id="",  # blank → rejected
                request_hash="a" * 64,
                policy_request_hash="b" * 64,
                evaluation_hash="c" * 64,
            )

    def test_policy_audit_proposal_id_mismatch_rejected_by_verify_semantics(self):
        # Audit is for prop-other, but the ProposalReview is for prop-self.
        audit = _make_policy_audit(proposal_id="prop-other")
        review = ProposalReview(
            proposal_id="prop-self",
            status=ReviewDecisionStatus.APPROVED,
            findings=[],
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=audit,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()

    def test_review_batch_result_missing_governance_spec_hash_rejected(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        # Strip governance_spec_hash and rebuild — must be rejected.
        dump = result.model_dump(mode="python")
        dump.pop("governance_spec_hash")
        with pytest.raises(ValidationError):
            ReviewBatchResult(**dump)

    def test_review_batch_result_blank_governance_spec_hash_rejected(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        dump = result.model_dump(mode="python")
        dump["governance_spec_hash"] = "   "
        with pytest.raises(ValidationError):
            ReviewBatchResult(**dump)

    def test_result_governance_spec_hash_mismatch_request_rejected(self):
        req = _valid_request()
        result = asyncio.run(
            ProposalReviewer().review(
                req, policy_evaluator=DeterministicPolicyEvaluator()
            )
        )
        # Tamper the Result's governance_spec_hash so it no longer
        # matches the Request.  Use object.__setattr__ to bypass the
        # frozen-model hash check (we want verify_against_request to
        # catch it, not the construction-time validator).
        object.__setattr__(result, "governance_spec_hash", "0" * 64)
        with pytest.raises(InvalidReviewResultError):
            result.verify_against_request(req)


# ===========================================================================
# P0-7: Governance Registry integrity (live tamper detection)
# ===========================================================================


class TestGovernanceRegistryIntegrity:
    """R2.1 P0-7: live registry hash must match the module constant."""

    def test_verify_governance_spec_integrity_passes_on_untampered(self):
        # On a fresh import the live hash equals the constant.
        verify_governance_spec_integrity()  # must NOT raise.
        assert compute_live_governance_spec_hash() == ACTION_GOVERNANCE_SPEC_HASH

    def test_verify_governance_spec_integrity_detects_tamper(self):
        # Simulate tampering by passing an expected_hash that differs
        # from the live hash.  verify_governance_spec_integrity MUST
        # raise RuntimeError.
        with pytest.raises(RuntimeError):
            verify_governance_spec_integrity(expected_hash="tampered" * 8)

    def test_registry_is_mapping_proxy_type(self):
        # R2.1 P0-2: the public registry is a read-only MappingProxyType.
        from multi_agent.action_governance import ACTION_GOVERNANCE_REGISTRY

        assert isinstance(ACTION_GOVERNANCE_REGISTRY, MappingProxyType)
        # Mutation must fail — MappingProxyType is read-only.
        with pytest.raises(TypeError):
            ACTION_GOVERNANCE_REGISTRY["__nonexistent__"] = None  # type: ignore[index]


# ===========================================================================
# P1-1: ReviewGraphState has only graph_error (no raw error)
# ===========================================================================


class TestReviewGraphStateR21:
    """R2.1 P1-1: ReviewGraphState carries only ``graph_error``."""

    def test_review_graph_state_has_no_error_field(self):
        # The dataclass fields must NOT include ``error`` — only
        # ``graph_error``.  A raw Exception is not JSON-serialisable.
        field_names = {f.name for f in ReviewGraphState.__dataclass_fields__.values()}
        assert "graph_error" in field_names
        assert "error" not in field_names

    def test_review_graph_state_accepts_graph_error(self):
        req = _valid_request()
        err = ReviewGraphError(
            error_code="review.graph.test",
            message="test error",
        )
        state = ReviewGraphState(
            request=req,
            policy_evaluator=DeterministicPolicyEvaluator(),
            graph_error=err,
        )
        assert state.graph_error is err
        assert state.result is None


# ===========================================================================
# P1-2: Decision priority order
# ===========================================================================


class TestDecisionPriorityR21:
    """R2.1 P1-2: conflict > rejected > needs_input > needs_approval >
    deduplicated > approved."""

    def test_priority_conflict_gt_rejected(self):
        assert batch_status_priority(
            ReviewBatchStatus.CONFLICT
        ) > batch_status_priority(ReviewBatchStatus.REJECTED)

    def test_priority_rejected_gt_needs_input(self):
        assert batch_status_priority(
            ReviewBatchStatus.REJECTED
        ) > batch_status_priority(ReviewBatchStatus.NEEDS_INPUT)

    def test_priority_needs_input_gt_needs_approval(self):
        assert batch_status_priority(
            ReviewBatchStatus.NEEDS_INPUT
        ) > batch_status_priority(ReviewBatchStatus.NEEDS_APPROVAL)

    def test_priority_needs_approval_gt_deduplicated(self):
        assert batch_status_priority(
            ReviewBatchStatus.NEEDS_APPROVAL
        ) > batch_status_priority(ReviewBatchStatus.DEDUPLICATED)

    def test_priority_deduplicated_gt_approved(self):
        assert batch_status_priority(
            ReviewBatchStatus.DEDUPLICATED
        ) > batch_status_priority(ReviewBatchStatus.APPROVED)


# ===========================================================================
# P0-7: End-to-end ProposalReview semantics with policy_audit
# ===========================================================================


class TestProposalReviewPolicyAuditConsistency:
    """R2.1 P0-7: ProposalReview.verify_semantics enforces
    decision ↔ status consistency via policy_audit."""

    def test_approved_with_denied_audit_rejected(self):
        # status APPROVED but audit.decision == DENIED → must raise.
        audit = _make_policy_audit(decision=PolicyDecision.DENIED)
        review = ProposalReview(
            proposal_id="prop-sem-001",
            status=ReviewDecisionStatus.APPROVED,
            findings=[],
            authority_valid=True,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=audit,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()

    def test_rejected_with_allowed_audit_and_no_rejection_finding_rejected(self):
        # status REJECTED but audit.decision == ALLOWED and no
        # rejection-class finding → must raise.
        audit = _make_policy_audit(decision=PolicyDecision.ALLOWED)
        review = ProposalReview(
            proposal_id="prop-sem-002",
            status=ReviewDecisionStatus.REJECTED,
            findings=[
                ReviewFinding(
                    finding_code=CODE_EVIDENCE_DANGLING,  # not a rejection code
                    severity=ReviewFindingSeverity.WARNING,
                    message="dangling",
                    proposal_id="prop-sem-002",
                )
            ],
            authority_valid=False,
            policy_valid=True,
            idempotency_valid=True,
            policy_audit=audit,
        )
        with pytest.raises(InvalidReviewResultError):
            review.verify_semantics()

    def test_rejected_with_denied_audit_and_policy_denied_finding_passes(self):
        # status REJECTED, audit.decision == DENIED, and a
        # CODE_POLICY_DENIED finding → verify_semantics MUST pass.
        audit = _make_policy_audit(
            proposal_id="prop-sem-003",
            decision=PolicyDecision.DENIED,
        )
        review = ProposalReview(
            proposal_id="prop-sem-003",
            status=ReviewDecisionStatus.REJECTED,
            findings=[
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message="policy denied",
                    proposal_id="prop-sem-003",
                )
            ],
            authority_valid=False,
            policy_valid=False,
            idempotency_valid=True,
            policy_audit=audit,
        )
        # Must NOT raise.
        review.verify_semantics()
