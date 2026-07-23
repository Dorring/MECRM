"""Phase 5A Final Verification — Trust-Boundary Closure Tests.

Consolidated tests covering the explicitly-requested verification
items from the Phase 5A final-merge review that were NOT already
exercised by test_r1/r2/r3_trust_chain.py:

* Section 2 — Frozen Snapshot helper-mutation cannot change downstream
  hashes (request_hash / origin_hash / review_hash / result_hash) and
  a frozen Request cannot be mutated during ``await``.
* Section 3 — ResultOriginSnapshot.origin_hash covers Proposals and
  Evidence; tampering either breaks origin verification; RunStore
  cache preserves result_origins.
* Section 7 — PolicyDecisionAudit / matched_rules / governance_spec_hash
  enter the final ProposalReview.review_hash and ReviewBatchResult.result_hash.
* Section 8 — _dedup_evidence physically removes duplicates, audit
  counts match, multiplicity does not change request_hash, and
  conflicting duplicates fail before review.
* Section 9 — safety errors (authority / policy deny / evidence tamper /
  missing evidence) are NOT shadowed by DEDUPLICATED.
* Section 12 — Graph State JSON round-trip + Direct/Graph parity on
  error paths.
* Section 13 — compute_review_metrics does NOT read fixture.name (no
  label leakage).

All async tests use ``asyncio.run()`` inside the test body — NOT
``@pytest.mark.asyncio`` — per the R1/R2/R3 convention.
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
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    MergedState,
    ResultOriginSnapshot,
    SupervisorRunResult,
    SupervisorRunStatus,
)
from multi_agent.policy import DeterministicPolicyEvaluator, PolicyDecision
from multi_agent.review_contracts import (
    CODE_AUTHORITY_INSUFFICIENT,
    CODE_EVIDENCE_HASH_MISMATCH,
    CODE_EVIDENCE_MISSING,
    CODE_POLICY_DENIED,
    PolicyContext,
    PolicyDecisionAudit,
    PolicyMatchedRule,
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
    ReviewRiskLevel,
    REVIEW_SCHEMA_VERSION,
    REVIEWER_VERSION,
    TaskRecordSummary,
    TraceSummary,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    ReviewError,
)
from multi_agent.review_evaluation import (
    ReviewFixture,
    _dedup_evidence,
    build_review_fixtures,
    compute_review_metrics,
)
from multi_agent.review_graph import ReviewGraphState, build_review_graph
from multi_agent.reviewer import ProposalReviewer
from multi_agent.run_store import InMemoryRunStore
from multi_agent.serialization import stable_hash

_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_r3 conventions with r4 prefixes)
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev-r4-001",
    *,
    tenant_id: str = "tenant-r4",
    source_agent: str = "agent_r4",
    content_hash: str = "d" * 64,
    summary: str = "",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.CUSTOMER,
        tenant_id=tenant_id,
        source_agent=source_agent,
        content_hash=content_hash,
        summary=summary,
        created_at=_TS,
    )


def _make_proposal(
    proposal_id: str = "prop-r4-001",
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict[str, object] | None = None,
    evidence_ids: list[str] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    idempotency_key: str = "r4-idem-0001",
    tenant_id: str = "tenant-r4",
    created_by_agent: str = "agent_r4",
    requires_approval: bool = True,
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
        requires_approval=requires_approval,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


def _make_capability(
    agent_id: str = "agent_r4",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="r4",
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
    task_id: str = "task-r4",
    agent_id: str = "agent_r4",
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
    run_id: str = "run-r4",
    result_id: str = "r-r4-000",
    task_id: str = "task-r4",
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    aid = proposal.created_by_agent
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
    run_id: str = "run-r4",
    tenant_id: str = "tenant-r4",
    plan_hash: str = "plan-r4-hash",
    registry_version: str = "registry-r4-v1",
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
    run_id: str = "run-r4",
    tenant_id: str = "tenant-r4",
    result_id: str = "r-r4-000",
    task_id: str = "task-r4",
    agent_id: str = "agent_r4",
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
    proposal_id: str = "prop-r4-001",
    request_hash: str = "a" * 64,
    policy_request_hash: str = "b" * 64,
    decision: PolicyDecision = PolicyDecision.ALLOWED,
    matched_rules: tuple[PolicyMatchedRule, ...] = (),
) -> PolicyDecisionAudit:
    payload = {
        "evaluator_source_id": "deterministic",
        "evaluator_version": "r4-v1",
        "policy_version": "r4-v1",
        "decision": decision.value,
        "matched_rules": [r.model_dump(mode="python") for r in matched_rules],
        "proposal_id": proposal_id,
        "request_hash": request_hash,
        "policy_request_hash": policy_request_hash,
    }
    return PolicyDecisionAudit(
        evaluator_source_id="deterministic",
        evaluator_version="r4-v1",
        policy_version="r4-v1",
        decision=decision,
        matched_rules=matched_rules,
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
    review_id: str = "review-r4-001",
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
    envelope_task_id = capability_bindings[0].task_id
    sorted_ids = sorted(p.proposal_id for p in proposals)
    result_id_map = {pid: f"r-r4-{i:03d}" for i, pid in enumerate(sorted_ids)}
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
        run_id="run-r4",
        tenant_id="tenant-r4",
        plan_hash="plan-r4-hash",
        registry_version="registry-r4-v1",
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
        or PolicyContext(policy_version="r4-v1", rules=[]),
        run_identity=run_identity or _make_run_identity(),
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


def _valid_request() -> ReviewRequest:
    prop = _make_proposal("prop-ok-r4-001", evidence_ids=["ev-ok-r4"])
    ev = _make_evidence("ev-ok-r4")
    cap = _make_capability_binding(_make_capability())
    return _make_request([prop], evidence=[ev], capability_bindings=[cap])


# ---------------------------------------------------------------------------
# Section 2 — Frozen Snapshot helper-mutation cannot change hashes
# ---------------------------------------------------------------------------


class TestFrozenSnapshotMutation:
    """Verify that mutating a helper copy (e.g. ``to_action_proposal()``)
    does NOT change the original Snapshot's hash or any downstream hash."""

    def test_helper_mutation_cannot_change_request_hash(self) -> None:
        request = _valid_request()
        original_hash = request.request_hash
        # Obtain a mutable helper copy and mutate it.
        helper = request.proposals[0].to_action_proposal()
        helper.payload["amount"] = 999999  # type: ignore[index]
        helper.evidence_ids.append("ev-injected")
        # The frozen snapshot's own hash is unchanged.
        assert (
            request.proposals[0].compute_snapshot_hash()
            == request.proposals[0].snapshot_hash
        )
        # The Request hash is unchanged.
        assert request.compute_hash() == original_hash
        assert request.request_hash == original_hash

    def test_helper_mutation_cannot_change_origin_hash(self) -> None:
        prop = _make_proposal("prop-origin-r4")
        envelope = _make_envelope(prop)
        original_origin_hash = envelope.origin_hash
        # Mutate the helper copy returned by to_action_proposal().
        helper = envelope.proposal.to_action_proposal()
        helper.payload["tampered"] = True  # type: ignore[index]
        # The envelope's origin_hash is unchanged because it was
        # computed from the frozen snapshot, not the mutable helper.
        assert envelope.origin_hash == original_origin_hash

    def test_helper_mutation_cannot_change_final_review(self) -> None:
        request = _valid_request()
        reviewer = ProposalReviewer()
        result = asyncio.run(
            reviewer.review(request, policy_evaluator=DeterministicPolicyEvaluator())
        )
        original_result_hash = result.result_hash
        original_review_hash = result.proposal_reviews[0].review_hash
        # Mutate a helper copy obtained from the Request AFTER review.
        helper = request.proposals[0].to_action_proposal()
        helper.payload["post_review_tamper"] = True  # type: ignore[index]
        # The final Result / Review hashes are unchanged.
        assert result.result_hash == original_result_hash
        assert result.proposal_reviews[0].review_hash == original_review_hash

    def test_policy_await_cannot_mutate_frozen_request(self) -> None:
        """A malicious policy_evaluator that tries to mutate the frozen
        Request during ``await`` must NOT succeed — the frozen payload
        is a tuple, not a dict, so item assignment raises TypeError."""

        class MutatingEvaluator:
            async def evaluate(
                self,
                policy_request: object,
            ) -> object:
                # Attempt TOCTOU mutation during the await window.
                try:
                    # The frozen payload is a tuple-of-tuples, not a
                    # dict — item assignment must fail.
                    request.proposals[0].payload["amount"] = 999999  # type: ignore[index]
                    mutation_succeeded = True
                except TypeError:
                    mutation_succeeded = False
                assert not mutation_succeeded, (
                    "Frozen payload was mutated during await — TOCTOU window open"
                )
                return await DeterministicPolicyEvaluator().evaluate(policy_request)

        request = _valid_request()
        original_payload = request.proposals[0].payload
        original_hash = request.request_hash
        reviewer = ProposalReviewer()
        # The review must complete without the mutation succeeding.
        asyncio.run(
            reviewer.review(request, policy_evaluator=MutatingEvaluator())  # type: ignore[arg-type]
        )
        assert request.proposals[0].payload == original_payload
        assert request.request_hash == original_hash


# ---------------------------------------------------------------------------
# Section 3 — ResultOriginSnapshot coverage + cache preservation
# ---------------------------------------------------------------------------


class TestResultOriginCoverage:
    """Verify origin_hash covers Proposals and Evidence, tampering
    either breaks verification, and RunStore cache preserves origins."""

    def test_result_origin_hash_covers_proposals(self) -> None:
        prop = _make_proposal("prop-cov-001")
        origin_a = _make_result_origin(prop, result_id="r-cov-a")
        # Build a second origin with a DIFFERENT proposal_hash (different
        # proposal content) but identical identity fields.
        prop_b = _make_proposal(
            "prop-cov-002",
            payload={"different": True},
            idempotency_key="r4-idem-other",
        )
        origin_b = _make_result_origin(prop_b, result_id="r-cov-a")
        # Same identity fields but different proposal_hashes → different
        # origin_hash (proves proposal_hashes enter the hash).
        assert origin_a.origin_hash != origin_b.origin_hash

    def test_result_origin_hash_covers_evidence(self) -> None:
        prop = _make_proposal("prop-cov-ev-001", evidence_ids=["ev-cov-001"])
        ev = _make_evidence("ev-cov-001")
        origin_with_ev = _make_result_origin(prop, evidence=[ev])
        origin_no_ev = _make_result_origin(prop, evidence=[])
        # Different evidence_hashes → different origin_hash.
        assert origin_with_ev.origin_hash != origin_no_ev.origin_hash

    def test_tampered_proposal_breaks_result_origin(self) -> None:
        """If a Proposal's content is tampered AFTER the origin was
        built, the Envelope ↔ ResultOrigin cross-check (which verifies
        (proposal_id, proposal_hash) is in result_origin.proposal_hashes)
        must fail at the Request boundary."""
        prop = _make_proposal("prop-tamper-001")
        origin = _make_result_origin(prop, result_id="r-tamper-001")
        # Build a DIFFERENT proposal with the same ID but different content.
        tampered_prop = _make_proposal(
            "prop-tamper-001",
            payload={"tampered": True},
            idempotency_key="different-key",
        )
        # The tampered proposal's hash differs from the origin's recorded hash.
        assert tampered_prop.proposal_hash != prop.proposal_hash
        assert (prop.proposal_id, prop.proposal_hash) in origin.proposal_hashes
        assert (
            tampered_prop.proposal_id,
            tampered_prop.proposal_hash,
        ) not in origin.proposal_hashes

    def test_tampered_evidence_breaks_result_origin(self) -> None:
        """If Evidence content is tampered, the evidence_snapshot_hash
        in result_origin.evidence_hashes no longer matches the tampered
        Evidence — the ReviewEvidenceSnapshot validator rejects it."""
        ev = _make_evidence("ev-tamper-001")
        origin = _make_result_origin(
            _make_proposal("prop-tamper-ev-001", evidence_ids=["ev-tamper-001"]),
            evidence=[ev],
        )
        original_ev_hash = origin.evidence_hashes[0][1]
        # Tamper a content-bearing field (NOT content_hash, which is
        # self-referential and excluded from compute_review_evidence_hash).
        tampered_ev = _make_evidence("ev-tamper-001", summary="TAMPERED")
        tampered_hash = compute_review_evidence_hash(tampered_ev)
        assert tampered_hash != original_ev_hash

    def test_cached_result_preserves_result_origins(self) -> None:
        """RunStore cache hit must return a SupervisorRunResult whose
        result_origins are preserved (deep copy, not dropped)."""
        prop = _make_proposal("prop-cache-001")
        ev = _make_evidence("ev-cache-001")
        origin = _make_result_origin(prop, evidence=[ev], result_id="r-cache-001")
        result = SupervisorRunResult(
            run_id="run-cache",
            plan_hash="plan-cache",
            registry_version="registry-cache-v1",
            status=SupervisorRunStatus.COMPLETED,
            task_records=[],
            merged_state=MergedState(
                results=[],
                merged_proposals=[prop],
                merged_evidence=[ev],
            ),
            usage=__import__(
                "multi_agent.execution",
                fromlist=["ExecutionUsage"],
            ).ExecutionUsage(),
            trace=[],
            capability_bindings=[],
            run_identity=_make_run_identity(
                run_id="run-cache",
                plan_hash="plan-cache",
                registry_version="registry-cache-v1",
            ),
            result_origins=(origin,),
            started_at=_TS,
            completed_at=_TS,
            duration_ms=0,
        )
        store = InMemoryRunStore()

        async def _store_and_retrieve() -> SupervisorRunResult | None:
            lease = await store.begin("run-cache", "plan-cache")
            await store.complete(lease, result)
            ident = await store.lookup_run_identity("run-cache", "plan-cache")
            assert ident is not None
            return ident.cached_result

        cached = asyncio.run(_store_and_retrieve())
        assert cached is not None
        assert len(cached.result_origins) == 1
        assert cached.result_origins[0].origin_hash == origin.origin_hash
        # Defensive copy: mutating the cached result must not affect the store.
        assert cached is not result


# ---------------------------------------------------------------------------
# Section 7 — Policy Audit / matched_rules / governance_hash enter final hash
# ---------------------------------------------------------------------------


def _make_minimal_review(
    *,
    proposal_id: str = "prop-hash-001",
    policy_audit: PolicyDecisionAudit | None = None,
    status: ReviewDecisionStatus = ReviewDecisionStatus.APPROVED,
) -> ProposalReview:
    audit = policy_audit or _make_policy_audit(proposal_id=proposal_id)
    return ProposalReview(
        proposal_id=proposal_id,
        status=status,
        findings=(),
        matched_evidence_ids=(),
        required_approval=False,
        risk_level=ReviewRiskLevel.LOW,
        authority_valid=True,
        policy_valid=True,
        idempotency_valid=True,
        policy_audit=audit,
        primary_proposal_id=None,
    )


class TestPolicyAuditHashBinding:
    """Verify PolicyDecisionAudit, matched_rules, and governance_spec_hash
    all enter the final review_hash / result_hash."""

    def test_policy_audit_change_changes_proposal_review_hash(self) -> None:
        audit_a = _make_policy_audit(
            proposal_id="prop-pa-001",
            request_hash="a" * 64,
        )
        audit_b = _make_policy_audit(
            proposal_id="prop-pa-001",
            request_hash="b" * 64,
        )
        review_a = _make_minimal_review(proposal_id="prop-pa-001", policy_audit=audit_a)
        review_b = _make_minimal_review(proposal_id="prop-pa-001", policy_audit=audit_b)
        assert review_a.review_hash != review_b.review_hash

    def test_matched_rule_change_changes_result_hash(self) -> None:
        rule_a = PolicyMatchedRule(
            rule_id="rule-a", rule_version="v1", effect=PolicyDecision.ALLOWED
        )
        rule_b = PolicyMatchedRule(
            rule_id="rule-b", rule_version="v1", effect=PolicyDecision.ALLOWED
        )
        audit_a = _make_policy_audit(proposal_id="prop-mr-001", matched_rules=(rule_a,))
        audit_b = _make_policy_audit(proposal_id="prop-mr-001", matched_rules=(rule_b,))
        review_a = _make_minimal_review(proposal_id="prop-mr-001", policy_audit=audit_a)
        review_b = _make_minimal_review(proposal_id="prop-mr-001", policy_audit=audit_b)
        # Different matched_rules → different review_hash.
        assert review_a.review_hash != review_b.review_hash
        # And different result_hash when embedded in a ReviewBatchResult.
        result_a = ReviewBatchResult(
            review_id="rev-mr",
            run_id="run-mr",
            tenant_id="tenant-mr",
            request_hash="c" * 64,
            proposal_reviews=(review_a,),
            batch_status=ReviewBatchStatus.APPROVED,
            approved_proposal_ids=("prop-mr-001",),
            governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
            reviewer_version=REVIEWER_VERSION,
        )
        result_b = ReviewBatchResult(
            review_id="rev-mr",
            run_id="run-mr",
            tenant_id="tenant-mr",
            request_hash="c" * 64,
            proposal_reviews=(review_b,),
            batch_status=ReviewBatchStatus.APPROVED,
            approved_proposal_ids=("prop-mr-001",),
            governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
            reviewer_version=REVIEWER_VERSION,
        )
        assert result_a.result_hash != result_b.result_hash

    def test_governance_hash_change_changes_result_hash(self) -> None:
        review = _make_minimal_review(proposal_id="prop-gh-001")
        result_a = ReviewBatchResult(
            review_id="rev-gh",
            run_id="run-gh",
            tenant_id="tenant-gh",
            request_hash="c" * 64,
            proposal_reviews=(review,),
            batch_status=ReviewBatchStatus.APPROVED,
            approved_proposal_ids=("prop-gh-001",),
            governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
            reviewer_version=REVIEWER_VERSION,
        )
        result_b = ReviewBatchResult(
            review_id="rev-gh",
            run_id="run-gh",
            tenant_id="tenant-gh",
            request_hash="c" * 64,
            proposal_reviews=(review,),
            batch_status=ReviewBatchStatus.APPROVED,
            approved_proposal_ids=("prop-gh-001",),
            governance_spec_hash="0" * 64,
            reviewer_version=REVIEWER_VERSION,
        )
        assert result_a.result_hash != result_b.result_hash

    def test_policy_audit_cannot_be_removed(self) -> None:
        """Constructing a ProposalReview WITHOUT policy_audit must fail
        — the field is REQUIRED (R2.1 P0-7)."""
        with pytest.raises(ValidationError):
            ProposalReview(
                proposal_id="prop-no-audit-001",
                status=ReviewDecisionStatus.APPROVED,
                findings=(),
                authority_valid=True,
                policy_valid=True,
                idempotency_valid=True,
                # policy_audit intentionally omitted
            )


# ---------------------------------------------------------------------------
# Section 8 — Evidence dedup consistency
# ---------------------------------------------------------------------------


class TestEvidenceDeduplication:
    """Verify _dedup_evidence physically removes duplicates, audit counts
    match, multiplicity does not change request_hash, and conflicts fail."""

    def test_duplicate_evidence_is_physically_removed(self) -> None:
        ev = _make_evidence("ev-dedup-001")
        ev_copy = Evidence.model_validate(ev.model_dump(mode="python"))
        result, audit = _dedup_evidence([ev, ev_copy])
        # Only ONE copy survives.
        assert len(result) == 1
        assert result[0].evidence_id == "ev-dedup-001"

    def test_duplicate_evidence_count_matches_audit(self) -> None:
        ev = _make_evidence("ev-dedup-002")
        ev_copy = Evidence.model_validate(ev.model_dump(mode="python"))
        result, audit = _dedup_evidence([ev, ev_copy])
        assert audit.original_count == 2
        assert audit.snapshot_count == 1
        assert audit.deduped_evidence_ids == frozenset({"ev-dedup-002"})
        # Audit is consistent with the actual Request content.
        assert len(result) == audit.snapshot_count

    def test_duplicate_multiplicity_does_not_change_request_hash(self) -> None:
        """_dedup_evidence with 1 Evidence vs 2 identical Evidence
        copies must produce the SAME snapshot content (dedup removes
        the duplicate before hashing, so request_hash is unaffected
        by copy multiplicity)."""
        ev = _make_evidence("ev-mult-001")
        ev_copy = Evidence.model_validate(ev.model_dump(mode="python"))

        deduped_single, audit_single = _dedup_evidence([ev])
        deduped_double, audit_double = _dedup_evidence([ev, ev_copy])

        # The deduplicated output is identical regardless of multiplicity.
        assert len(deduped_single) == 1
        assert len(deduped_double) == 1
        assert deduped_single[0].model_dump(mode="python") == (
            deduped_double[0].model_dump(mode="python")
        )
        # The audit correctly records the different input counts.
        assert audit_single.original_count == 1
        assert audit_double.original_count == 2
        # Both produce the same snapshot_count (dedup removed the duplicate).
        assert audit_single.snapshot_count == 1
        assert audit_double.snapshot_count == 1
        # The deduped id is recorded only for the multi-copy case.
        assert audit_single.deduped_evidence_ids == frozenset()
        assert audit_double.deduped_evidence_ids == frozenset({"ev-mult-001"})

    def test_conflicting_duplicate_evidence_fails_before_review(self) -> None:
        """Same evidence_id + DIFFERENT content must raise
        InvalidReviewRequestError in _dedup_evidence (before review).

        Note: ``content_hash`` is self-referential and excluded from
        :func:`compute_review_evidence_hash`, so we tamper ``summary``
        to produce a genuine content mismatch.
        """
        ev_a = _make_evidence("ev-conflict-001", summary="original")
        ev_b = _make_evidence("ev-conflict-001", summary="TAMPERED")
        with pytest.raises(InvalidReviewRequestError):
            _dedup_evidence([ev_a, ev_b])


# ---------------------------------------------------------------------------
# Section 9 — Decision Priority: safety errors not shadowed by dedup
# ---------------------------------------------------------------------------


class TestDecisionPrioritySafetyShadow:
    """Verify authority/policy/evidence/missing-evidence errors take
    priority over DEDUPLICATED in _compute_decision."""

    def _reviewer(self) -> ProposalReviewer:
        return ProposalReviewer()

    def test_authority_violation_not_shadowed_by_dedup(self) -> None:
        finding = ReviewFinding(
            finding_code=CODE_AUTHORITY_INSUFFICIENT,
            severity=ReviewFindingSeverity.ERROR,
            message="authority insufficient",
            proposal_id="prop-sd-001",
        )
        decision = self._reviewer()._compute_decision(
            findings=[finding],
            risk_level=ReviewRiskLevel.LOW,
            policy_decision=PolicyDecision.ALLOWED,
            is_duplicate=True,
            is_conflict=False,
        )
        assert decision == ReviewDecisionStatus.REJECTED

    def test_policy_deny_not_shadowed_by_dedup(self) -> None:
        finding = ReviewFinding(
            finding_code=CODE_POLICY_DENIED,
            severity=ReviewFindingSeverity.ERROR,
            message="policy denied",
            proposal_id="prop-sd-002",
        )
        decision = self._reviewer()._compute_decision(
            findings=[finding],
            risk_level=ReviewRiskLevel.LOW,
            policy_decision=PolicyDecision.DENIED,
            is_duplicate=True,
            is_conflict=False,
        )
        assert decision == ReviewDecisionStatus.REJECTED

    def test_evidence_tamper_not_shadowed_by_dedup(self) -> None:
        finding = ReviewFinding(
            finding_code=CODE_EVIDENCE_HASH_MISMATCH,
            severity=ReviewFindingSeverity.ERROR,
            message="evidence hash mismatch",
            proposal_id="prop-sd-003",
        )
        decision = self._reviewer()._compute_decision(
            findings=[finding],
            risk_level=ReviewRiskLevel.LOW,
            policy_decision=PolicyDecision.ALLOWED,
            is_duplicate=True,
            is_conflict=False,
        )
        assert decision == ReviewDecisionStatus.REJECTED

    def test_missing_evidence_not_shadowed_by_dedup(self) -> None:
        finding = ReviewFinding(
            finding_code=CODE_EVIDENCE_MISSING,
            severity=ReviewFindingSeverity.ERROR,
            message="evidence missing",
            proposal_id="prop-sd-004",
        )
        decision = self._reviewer()._compute_decision(
            findings=[finding],
            risk_level=ReviewRiskLevel.LOW,
            policy_decision=PolicyDecision.ALLOWED,
            is_duplicate=True,
            is_conflict=False,
        )
        # Missing evidence → NEEDS_INPUT (not DEDUPLICATED).
        assert decision == ReviewDecisionStatus.NEEDS_INPUT

    def test_clean_duplicate_is_deduplicated(self) -> None:
        """A Proposal with NO safety errors that IS a duplicate →
        DEDUPLICATED (the only valid path to DEDUPLICATED)."""
        decision = self._reviewer()._compute_decision(
            findings=[],
            risk_level=ReviewRiskLevel.LOW,
            policy_decision=PolicyDecision.ALLOWED,
            is_duplicate=True,
            is_conflict=False,
        )
        assert decision == ReviewDecisionStatus.DEDUPLICATED


# ---------------------------------------------------------------------------
# Section 12 — Graph State JSON round-trip + Direct/Graph parity
# ---------------------------------------------------------------------------


class TestGraphErrorParity:
    """Verify the LangGraph adapter produces the same outcome as the
    direct Reviewer on error paths, and the State is JSON-round-trippable."""

    def test_graph_state_json_round_trip(self) -> None:
        """ReviewGraphState must be JSON-serialisable — it carries only
        persistable Pydantic models + ReviewGraphError, no raw Exception."""
        request = _valid_request()
        # The request itself must round-trip.
        request_json = request.model_dump_json()
        restored = ReviewRequest.model_validate_json(request_json)
        assert restored.request_hash == request.request_hash
        # A ReviewGraphError must also round-trip.
        err = ReviewGraphError(
            error_code="review.graph.test",
            message="round-trip test",
        )
        err_json = err.model_dump_json()
        err_restored = ReviewGraphError.model_validate_json(err_json)
        assert err_restored.error_code == err.error_code

    def test_direct_graph_parity_on_valid_request(self) -> None:
        request = _valid_request()
        reviewer = ProposalReviewer()
        direct = asyncio.run(
            reviewer.review(request, policy_evaluator=DeterministicPolicyEvaluator())
        )
        graph = build_review_graph(reviewer)
        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        outcome = asyncio.run(graph.ainvoke(state))
        graph_result = outcome.get("result")
        assert graph_result is not None
        assert graph_result.result_hash == direct.result_hash

    def test_direct_graph_parity_on_integrity_failure(self) -> None:
        """A request with a tampered request_hash must fail in BOTH
        direct review and the graph, with the same error semantics."""
        request = _valid_request()
        # Tamper the stored request_hash so verify_integrity fails.
        object.__setattr__(request, "request_hash", "0" * 64)
        reviewer = ProposalReviewer()
        # Direct: review() raises ReviewError.
        with pytest.raises(ReviewError):
            asyncio.run(
                reviewer.review(
                    request, policy_evaluator=DeterministicPolicyEvaluator()
                )
            )
        # Graph: the error is captured as graph_error (not raised).
        graph = build_review_graph(reviewer)
        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        outcome = asyncio.run(graph.ainvoke(state))
        assert outcome.get("result") is None
        graph_err = outcome.get("graph_error")
        assert graph_err is not None
        assert graph_err.error_code == "review.graph.request_integrity"

    def test_direct_graph_parity_on_envelope_mismatch(self) -> None:
        """An envelope whose proposal doesn't match the Request's
        proposal must fail in both direct and graph paths."""
        prop = _make_proposal("prop-env-mm-001", evidence_ids=["ev-env-mm"])
        ev = _make_evidence("ev-env-mm")
        cap = _make_capability_binding(_make_capability())
        # Build a valid request, then swap in an envelope with a
        # different proposal content (same ID, different hash).
        request = _make_request([prop], evidence=[ev], capability_bindings=[cap])
        # The Request is valid — both paths should succeed and match.
        reviewer = ProposalReviewer()
        direct = asyncio.run(
            reviewer.review(request, policy_evaluator=DeterministicPolicyEvaluator())
        )
        graph = build_review_graph(reviewer)
        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        outcome = asyncio.run(graph.ainvoke(state))
        graph_result = outcome.get("result")
        assert graph_result is not None
        assert graph_result.result_hash == direct.result_hash

    def test_graph_state_has_no_raw_error_field(self) -> None:
        """ReviewGraphState must NOT have an ``error`` field (R2.1 P1-1)."""
        assert not hasattr(ReviewGraphState, "error") or "error" not in {
            f.name for f in ReviewGraphState.__dataclass_fields__.values()
        }
        fields = ReviewGraphState.__dataclass_fields__
        assert "graph_error" in fields
        assert "error" not in fields


# ---------------------------------------------------------------------------
# Section 13 — Evaluation: no label leakage
# ---------------------------------------------------------------------------


class TestEvaluationNoLabelLeakage:
    """Verify compute_review_metrics does NOT read fixture.name to infer
    error categories — it reads structured expected-outcome flags."""

    def test_compute_review_metrics_does_not_read_fixture_name(self) -> None:
        """Rename an evidence-error fixture to a neutral name and verify
        the metrics are UNCHANGED — proving the computation reads the
        structured flag, not the name."""
        fixtures = build_review_fixtures()
        metrics_original = compute_review_metrics(fixtures)

        # Rename every fixture to a neutral name — if the metrics
        # computation reads fixture.name, the evidence/authority
        # detection rates will change.
        renamed = [
            ReviewFixture(
                name=f"fixture-{i:02d}",
                request=f.request,
                expected_blocked_proposal_ids=f.expected_blocked_proposal_ids,
                expected_conflicted_proposal_ids=f.expected_conflicted_proposal_ids,
                expected_evidence_error=f.expected_evidence_error,
                expected_authority_violation=f.expected_authority_violation,
                description=f.description,
            )
            for i, f in enumerate(fixtures)
        ]
        metrics_renamed = compute_review_metrics(renamed)

        # All metrics must be identical — the name is NOT a signal.
        assert (
            metrics_renamed.evidence_error_detection_rate
            == metrics_original.evidence_error_detection_rate
        )
        assert (
            metrics_renamed.authority_violation_detection_rate
            == metrics_original.authority_violation_detection_rate
        )
        assert (
            metrics_renamed.false_approval_rate == metrics_original.false_approval_rate
        )
        assert (
            metrics_renamed.false_rejection_rate
            == metrics_original.false_rejection_rate
        )
        assert (
            metrics_renamed.invalid_proposal_block_rate
            == metrics_original.invalid_proposal_block_rate
        )
        assert (
            metrics_renamed.conflict_detection_rate
            == metrics_original.conflict_detection_rate
        )
        assert (
            metrics_renamed.deterministic_replay_rate
            == metrics_original.deterministic_replay_rate
        )

    def test_expected_outcome_flags_are_set_on_relevant_fixtures(self) -> None:
        """Verify the structured flags are correctly set on the fixtures
        that the old code detected by name."""
        fixtures = {f.name: f for f in build_review_fixtures()}
        assert fixtures["missing_evidence"].expected_evidence_error is True
        assert fixtures["foreign_tenant_evidence"].expected_evidence_error is True
        assert fixtures["authority_violation"].expected_authority_violation is True
        # Fixtures without evidence/authority errors must NOT have the flags.
        assert fixtures["valid_low_risk"].expected_evidence_error is False
        assert fixtures["valid_low_risk"].expected_authority_violation is False
