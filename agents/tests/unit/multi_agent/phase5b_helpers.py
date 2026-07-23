"""Shared test helpers for Phase 5B execution tests.

Centralises the construction of valid :class:`ReviewRequest` +
:class:`ReviewBatchResult` pairs, :class:`ExecutionAuthorization`,
adapters, stores, and kill switches so every test file can build a
deterministic, CI-safe fixture without duplicating boilerplate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from multi_agent.action_adapter import (
    ActionAdapterRegistry,
    RecordingActionAdapter,
)
from multi_agent.action_governance import ACTION_GOVERNANCE_REGISTRY
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
from multi_agent.review_contracts import (
    CODE_CONFLICT_FIELD_VALUE,
    CODE_DUPLICATE_DEDUPED,
    CODE_EVIDENCE_MISSING,
    CODE_POLICY_DENIED,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionAudit,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewEvidenceSnapshot,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewProposalEnvelope,
    ReviewProposalSnapshot,
    ReviewRequest,
    ReviewRiskLevel,
    REVIEWER_VERSION,
    TaskRecordSummary,
    TraceSummary,
)
from multi_agent.serialization import stable_hash

TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
TENANT = "tenant-exec-test"
RUN_ID = "run-exec-test"
PLAN_HASH = "plan-exec-test-hash"
REGISTRY_VERSION = "registry-exec-test-v1"


def make_capability(
    agent_id: str = "test_agent",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Test agent {agent_id}",
        domains=frozenset({"test"}),
        supported_tasks=frozenset({"test_task"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_customers"}),
        authority=authority,
        input_contract="test_input",
        output_contract="test_output",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
        enabled=True,
        metadata={},
    )


def make_evidence(
    evidence_id: str = "ev-test-001",
    *,
    evidence_type: EvidenceType = EvidenceType.CUSTOMER,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=TENANT,
        source_agent="test_agent",
        summary=f"Test evidence {evidence_id}",
        source_id=None,
        content_hash="a" * 64,
        created_at=TS,
        retrieved_at=TS,
        metadata={},
    )


def make_proposal(
    proposal_id: str = "prop-test-001",
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    payload: dict[str, Any] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    evidence_ids: list[str] | None = None,
    requires_approval: bool = False,
    idempotency_key: str = "idem-test-001",
    tenant_id: str = TENANT,
    created_by_agent: str = "test_agent",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity=target_entity,
        target_id=None,
        payload=payload or {},
        priority="medium",
        risk_level=risk_level,
        justification=None,
        evidence_ids=evidence_ids or [],
        requires_approval=requires_approval,
        idempotency_key=idempotency_key,
        created_at=TS,
    )


def make_capability_binding(
    task_id: str = "task-test",
    agent_id: str = "test_agent",
    capability: AgentCapability | None = None,
) -> ExecutionCapabilitySnapshot:
    cap = capability or make_capability(agent_id)
    binding_hash = stable_hash(
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_version": cap.version,
            "capability": cap.model_dump(mode="python"),
        }
    )
    return ExecutionCapabilitySnapshot(
        task_id=task_id,
        agent_id=agent_id,
        agent_version=cap.version,
        capability=cap,
        binding_hash=binding_hash,
    )


def make_envelope(
    proposal: ActionProposal,
    *,
    run_id: str = RUN_ID,
    result_id: str = "result-test",
    task_id: str = "task-test",
    agent_id: str | None = None,
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    aid = agent_id or proposal.created_by_agent
    snapshot = ReviewProposalSnapshot.from_proposal(proposal)
    origin_hash = stable_hash(
        {
            "proposal": snapshot.to_action_proposal().model_dump(mode="python"),
            "run_id": run_id,
            "result_id": result_id,
            "task_id": task_id,
            "agent_id": aid,
            "agent_version": agent_version,
        }
    )
    return ReviewProposalEnvelope(
        proposal=snapshot,
        run_id=run_id,
        result_id=result_id,
        task_id=task_id,
        agent_id=aid,
        agent_version=agent_version,
        origin_hash=origin_hash,
    )


def make_run_identity() -> ExecutionRunIdentity:
    identity_hash = stable_hash(
        {
            "run_id": RUN_ID,
            "tenant_id": TENANT,
            "plan_hash": PLAN_HASH,
            "registry_version": REGISTRY_VERSION,
        }
    )
    return ExecutionRunIdentity(
        run_id=RUN_ID,
        tenant_id=TENANT,
        plan_hash=PLAN_HASH,
        registry_version=REGISTRY_VERSION,
        identity_hash=identity_hash,
    )


def make_result_origin(
    proposal_snapshots: list[ReviewProposalSnapshot],
    evidence_snapshots: list[ReviewEvidenceSnapshot],
    *,
    result_id: str = "result-test",
    task_id: str = "task-test",
) -> ResultOriginSnapshot:
    proposal_hashes = tuple(
        sorted((s.proposal_id, s.proposal_hash) for s in proposal_snapshots)
    )
    evidence_hashes = tuple(
        sorted((ev.evidence.evidence_id, ev.snapshot_hash) for ev in evidence_snapshots)
    )
    origin_hash = stable_hash(
        {
            "run_id": RUN_ID,
            "tenant_id": TENANT,
            "result_id": result_id,
            "task_id": task_id,
            "agent_id": "test_agent",
            "agent_version": "1.0.0",
            "proposal_hashes": sorted(proposal_hashes),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
    return ResultOriginSnapshot(
        run_id=RUN_ID,
        tenant_id=TENANT,
        result_id=result_id,
        task_id=task_id,
        agent_id="test_agent",
        agent_version="1.0.0",
        proposal_hashes=proposal_hashes,
        evidence_hashes=evidence_hashes,
        origin_hash=origin_hash,
    )


def make_policy_audit(
    proposal_id: str,
    request_hash: str,
    *,
    decision: PolicyDecision = PolicyDecision.ALLOWED,
) -> PolicyDecisionAudit:
    policy_request_hash = stable_hash(
        {"proposal_id": proposal_id, "request_hash": request_hash}
    )
    evaluation_hash = stable_hash(
        {
            "evaluator_source_id": "deterministic-policy",
            "evaluator_version": "1.0.0",
            "policy_version": "ma-05a-default",
            "decision": decision.value,
            "matched_rules": [],
            "proposal_id": proposal_id,
            "request_hash": request_hash,
            "policy_request_hash": policy_request_hash,
        }
    )
    return PolicyDecisionAudit(
        evaluator_source_id="deterministic-policy",
        evaluator_version="1.0.0",
        policy_version="ma-05a-default",
        decision=decision,
        matched_rules=(),
        proposal_id=proposal_id,
        request_hash=request_hash,
        policy_request_hash=policy_request_hash,
        evaluation_hash=evaluation_hash,
    )


def _required_finding_for_status(
    proposal_id: str,
    status: ReviewDecisionStatus,
) -> tuple[ReviewFinding, ...]:
    """Return the minimal finding set ``verify_semantics`` requires.

    APPROVED and NEEDS_APPROVAL need no findings (only validity
    flags / ``required_approval``).  REJECTED / NEEDS_INPUT / CONFLICT
    / DEDUPLICATED each require at least one finding of a specific
    code class, otherwise ``verify_against_request`` rejects the
    Result and the GovernedExecutor returns BLOCKED instead of the
    expected NO_ACTIONS / PENDING_APPROVAL outcome.
    """
    if status == ReviewDecisionStatus.REJECTED:
        return (
            ReviewFinding(
                finding_code=CODE_POLICY_DENIED,
                severity=ReviewFindingSeverity.ERROR,
                message=f"policy denied for {proposal_id}",
                proposal_id=proposal_id,
            ),
        )
    if status == ReviewDecisionStatus.NEEDS_INPUT:
        return (
            ReviewFinding(
                finding_code=CODE_EVIDENCE_MISSING,
                severity=ReviewFindingSeverity.WARNING,
                message=f"missing evidence for {proposal_id}",
                proposal_id=proposal_id,
            ),
        )
    if status == ReviewDecisionStatus.CONFLICT:
        return (
            ReviewFinding(
                finding_code=CODE_CONFLICT_FIELD_VALUE,
                severity=ReviewFindingSeverity.ERROR,
                message=f"conflicting field for {proposal_id}",
                proposal_id=proposal_id,
            ),
        )
    if status == ReviewDecisionStatus.DEDUPLICATED:
        return (
            ReviewFinding(
                finding_code=CODE_DUPLICATE_DEDUPED,
                severity=ReviewFindingSeverity.INFO,
                message=f"duplicate deduped {proposal_id}",
                proposal_id=proposal_id,
            ),
        )
    return ()


def make_review(
    proposal_id: str,
    request_hash: str,
    *,
    status: ReviewDecisionStatus = ReviewDecisionStatus.APPROVED,
    risk_level: ReviewRiskLevel = ReviewRiskLevel.LOW,
    required_approval: bool = False,
    authority_valid: bool = True,
    policy_valid: bool = True,
    idempotency_valid: bool = True,
    findings: tuple[ReviewFinding, ...] = (),
    primary_proposal_id: str | None = None,
    policy_decision: PolicyDecision = PolicyDecision.ALLOWED,
) -> ProposalReview:
    audit = make_policy_audit(proposal_id, request_hash, decision=policy_decision)
    # When no explicit findings are supplied, auto-derive the minimal
    # finding set required by verify_semantics for the chosen status
    # so the Result can pass verify_against_request in the executor.
    if not findings:
        findings = _required_finding_for_status(proposal_id, status)
    # verify_semantics requires validity flags consistent with status:
    # REJECTED with a policy-denied finding requires policy_valid=False;
    # CONFLICT requires both authority_valid and policy_valid=False.
    if status == ReviewDecisionStatus.REJECTED and not policy_valid:
        pass  # caller already set it
    elif status == ReviewDecisionStatus.REJECTED:
        policy_valid = False
    if status == ReviewDecisionStatus.CONFLICT:
        authority_valid = False
        policy_valid = False
    return ProposalReview(
        proposal_id=proposal_id,
        status=status,
        findings=findings,
        required_approval=required_approval,
        risk_level=risk_level,
        authority_valid=authority_valid,
        policy_valid=policy_valid,
        idempotency_valid=idempotency_valid,
        policy_audit=audit,
        primary_proposal_id=primary_proposal_id,
    )


def make_request(
    review_id: str = "review-test",
    proposals: list[ActionProposal] | None = None,
    evidence: list[Evidence] | None = None,
    *,
    capability: AgentCapability | None = None,
    tenant_id: str = TENANT,
) -> ReviewRequest:
    # Distinguish None (use default) from [] (explicitly empty).
    if proposals is None:
        proposals = [make_proposal()]
    if evidence is None:
        evidence = [make_evidence()]
    cap = capability or make_capability()
    cap_binding = make_capability_binding("task-test", "test_agent", cap)
    evidence_snapshots = [
        ReviewEvidenceSnapshot(
            evidence=ev, snapshot_hash=compute_review_evidence_hash(ev)
        )
        for ev in evidence
    ]
    proposal_snapshots = [ReviewProposalSnapshot.from_proposal(p) for p in proposals]
    envelopes = [make_envelope(p) for p in proposals]
    result_origin = make_result_origin(proposal_snapshots, evidence_snapshots)
    run_identity = make_run_identity()
    policy_context = PolicyContext(
        policy_version="ma-05a-default",
        rules=(),
        tenant_overrides=None,
    )
    task_record = TaskRecordSummary(
        task_id="task-test",
        agent_id="test_agent",
        status="completed",
    )
    trace = TraceSummary(
        sequence=0,
        event_type="task.completed",
        task_id="task-test",
        agent_id="test_agent",
    )
    return ReviewRequest(
        review_id=review_id,
        run_id=RUN_ID,
        tenant_id=tenant_id,
        plan_hash=PLAN_HASH,
        registry_version=REGISTRY_VERSION,
        proposals=tuple(proposal_snapshots),
        evidence=tuple(evidence_snapshots),
        task_records=(task_record,),
        trace=(trace,),
        proposal_envelopes=tuple(envelopes),
        capability_bindings=(cap_binding,),
        result_origins=(result_origin,),
        policy_context=policy_context,
        run_identity=run_identity,
        governance_spec_version="ma-05a.action-governance.1.0",
        governance_spec_hash=stable_hash({"placeholder": "governance_spec_hash_test"})
        if False
        else __import__(
            "multi_agent.action_governance", fromlist=["ACTION_GOVERNANCE_SPEC_HASH"]
        ).ACTION_GOVERNANCE_SPEC_HASH,
        reviewer_version=REVIEWER_VERSION,
    )


def make_result(
    request: ReviewRequest,
    reviews: list[ProposalReview],
) -> ReviewBatchResult:
    from multi_agent.review_contracts import (
        batch_status_priority,
        proposal_status_to_batch,
    )

    approved = tuple(
        sorted(
            r.proposal_id for r in reviews if r.status == ReviewDecisionStatus.APPROVED
        )
    )
    rejected = tuple(
        sorted(
            r.proposal_id for r in reviews if r.status == ReviewDecisionStatus.REJECTED
        )
    )
    approval_required = tuple(
        sorted(
            r.proposal_id
            for r in reviews
            if r.status == ReviewDecisionStatus.NEEDS_APPROVAL
        )
    )
    conflicted = tuple(
        sorted(
            r.proposal_id for r in reviews if r.status == ReviewDecisionStatus.CONFLICT
        )
    )
    deduped = tuple(
        sorted(
            r.proposal_id
            for r in reviews
            if r.status == ReviewDecisionStatus.DEDUPLICATED
        )
    )
    batch_status = (
        ReviewBatchStatus.NO_ACTIONS
        if not reviews
        else max(
            (proposal_status_to_batch(r.status) for r in reviews),
            key=batch_status_priority,
        )
    )
    return ReviewBatchResult(
        review_id=request.review_id,
        run_id=request.run_id,
        tenant_id=request.tenant_id,
        request_hash=request.request_hash,
        proposal_reviews=tuple(reviews),
        batch_status=batch_status,
        approved_proposal_ids=approved,
        rejected_proposal_ids=rejected,
        approval_required_proposal_ids=approval_required,
        conflicted_proposal_ids=conflicted,
        deduplicated_proposal_ids=deduped,
        findings=(),
        governance_spec_hash=request.governance_spec_hash,
        reviewer_version=REVIEWER_VERSION,
    )


def make_approved_request_result(
    *,
    proposal_id: str = "prop-test-001",
    action_type: str = "report.generate",
    review_id: str = "review-test",
) -> tuple[ReviewRequest, ReviewBatchResult, ProposalReview]:
    """Build a minimal approved ReviewRequest + ReviewBatchResult pair."""
    proposal = make_proposal(proposal_id, action_type=action_type)
    request = make_request(review_id, [proposal], [make_evidence()])
    review = make_review(proposal_id, request.request_hash)
    result = make_result(request, [review])
    return request, result, review


class NoKillSwitch:
    """Kill switch that is never active."""

    async def is_cancelled(self, run_id: str) -> bool:
        return False

    async def is_kill_switch_active(self, tenant_id: str) -> bool:
        return False


class AlwaysKillSwitch:
    """Kill switch that is always active for a given tenant."""

    def __init__(self, tenant_id: str = TENANT) -> None:
        self._tenant_id = tenant_id

    async def is_cancelled(self, run_id: str) -> bool:
        return False

    async def is_kill_switch_active(self, tenant_id: str) -> bool:
        return tenant_id == self._tenant_id


class CancelledRun:
    """Kill switch that reports a specific run as cancelled."""

    def __init__(self, run_id: str = RUN_ID) -> None:
        self._run_id = run_id

    async def is_cancelled(self, run_id: str) -> bool:
        return run_id == self._run_id

    async def is_kill_switch_active(self, tenant_id: str) -> bool:
        return False


def make_recording_registry(
    sink: list,
    *,
    outcome_status=None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ActionAdapterRegistry:
    """Build a registry with a :class:`RecordingActionAdapter` bound to
    every action type in the governance registry."""
    from multi_agent.execution_authorization import ExecutionStatus

    registry = ActionAdapterRegistry()
    adapter = RecordingActionAdapter(
        sink=sink,
        supported_action_types=frozenset(ACTION_GOVERNANCE_REGISTRY),
        outcome_status=outcome_status or ExecutionStatus.SUCCEEDED,
        error_code=error_code,
        error_message=error_message,
    )
    registry.register(adapter)
    return registry


def run_async(coro):
    """Run an async coroutine synchronously (for tests without
    @pytest.mark.asyncio)."""
    return asyncio.run(coro)
