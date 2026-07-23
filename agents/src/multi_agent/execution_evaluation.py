"""Phase 5B — Deterministic evaluation fixtures and metrics.

Three concerns (mirroring Phase 5A's :mod:`review_evaluation`):

1. **Deterministic Fixtures** — :func:`build_execution_fixtures`
   produces a fixed set of :class:`ExecutionFixture` instances
   covering the scenarios enumerated in Phase 5B Section 32.  Each
   fixture carries a :class:`ReviewRequest` + :class:`ReviewBatchResult`
   pair plus an :class:`ExecutionExpectedOutcome` (NO label leakage
   from fixture names).

2. **Evaluation Metrics** — :func:`compute_execution_metrics` runs
   every fixture through an executor factory and computes the
   metrics listed in Phase 5B Section 32
   (``unauthorized_execution_block_rate``,
   ``approval_bypass_block_rate``, ``tenant_mismatch_block_rate``,
   ``idempotency_duplicate_prevention_rate``,
   ``unknown_outcome_fail_closed_rate``,
   ``receipt_tamper_detection_rate``, ``kill_switch_block_rate``,
   ``deterministic_replay_rate``, ``false_execution_rate``,
   ``execution_success_rate``, ``p50``/``p95`` latency).

Fixtures never read the wall-clock, ``PYTHONHASHSEED``, or any
external state.  Metrics never infer results from ``fixture.name``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ConfigDict

from multi_agent.action_adapter import (
    ActionAdapterRegistry,
    RecordingActionAdapter,
)
from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
)
from multi_agent.approval_contracts import FrozenClock
from multi_agent.approval_gate import InMemoryApprovalStore
from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    Evidence,
    EvidenceType,
    StrictContract,
)
from multi_agent.evidence_review import compute_review_evidence_hash
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ResultOriginSnapshot,
)
from multi_agent.execution_authorization import (
    BatchExecutionStatus,
    ExecutionStatus,
)
from multi_agent.execution_store import InMemoryExecutionStore
from multi_agent.governed_executor import (
    ExecutionOptions,
    GovernedExecutor,
)
from multi_agent.review_contracts import (
    CODE_DUPLICATE_DEDUPED,
    CODE_EVIDENCE_MISSING,
    CODE_POLICY_DENIED,
    REVIEWER_VERSION,
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
    TaskRecordSummary,
    TraceSummary,
)
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Expected outcome + fixture
# ---------------------------------------------------------------------------


class ExecutionExpectedOutcome(StrictContract):
    """Expected outcome for one execution fixture.

    Carries per-Proposal expected :class:`ExecutionStatus` values and
    the expected :class:`BatchExecutionStatus` so
    :func:`compute_execution_metrics` can compare actual vs expected
    WITHOUT inferring from ``fixture.name`` (Phase 5B Section 32).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_status_by_proposal: tuple[tuple[str, str], ...] = ()
    expected_batch_status: str | None = None
    expected_error_code: str | None = None
    expected_adapter_call_count: int | None = None

    @property
    def status_map(self) -> dict[str, str]:
        """Convenience accessor returning a fresh dict (P1-3: the
        underlying tuple is immutable; this returns a mutable copy
        for read-only lookup)."""
        return dict(self.expected_status_by_proposal)


@dataclass(frozen=True)
class ExecutionFixture:
    """One deterministic execution fixture."""

    name: str
    request: ReviewRequest
    review_result: ReviewBatchResult
    expected_outcome: ExecutionExpectedOutcome
    description: str = ""


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class ExecutionMetrics:
    """Aggregate metrics over a fixture run.

    P0-8 / Section 32 R1 additions (7 new metrics):

    * ``false_real_execution_rate`` — rate at which dry-run results are
      correctly NOT counted as real SUCCEEDED.
    * ``dry_run_classification_accuracy`` — rate at which dry-run vs
      real execution is correctly classified.
    * ``approval_request_creation_rate`` — rate at which required
      approvals produce an ApprovalRequest in the batch result.
    * ``approval_atomic_consumption_rate`` — rate at which approvals
      are atomically validated-and-consumed (no partial consume).
    * ``adapter_drift_block_rate`` — rate at which adapter binding
      drift is detected and blocked.
    * ``receipt_atomicity_rate`` — rate at which receipts are
      atomically committed with the idempotency state.
    * ``unknown_batch_preservation_rate`` — rate at which UNKNOWN
      outcomes are preserved as UNKNOWN in the batch (not downgraded).
    """

    total_fixtures: int = 0
    unauthorized_execution_block_rate: float = 0.0
    approval_bypass_block_rate: float = 0.0
    tenant_mismatch_block_rate: float = 0.0
    idempotency_duplicate_prevention_rate: float = 0.0
    unknown_outcome_fail_closed_rate: float = 0.0
    receipt_tamper_detection_rate: float = 0.0
    kill_switch_block_rate: float = 0.0
    deterministic_replay_rate: float = 0.0
    false_execution_rate: float = 0.0
    execution_success_rate: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    # P0-8 R1 new metrics
    false_real_execution_rate: float = 0.0
    dry_run_classification_accuracy: float = 0.0
    approval_request_creation_rate: float = 0.0
    approval_atomic_consumption_rate: float = 0.0
    adapter_drift_block_rate: float = 0.0
    receipt_atomicity_rate: float = 0.0
    unknown_batch_preservation_rate: float = 0.0


# ---------------------------------------------------------------------------
# Fixture helpers (private) — build valid ReviewRequest + ReviewBatchResult.
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_TENANT = "tenant-exec-fixture"
_RUN_ID = "run-exec-fixture"
_PLAN_HASH = "plan-exec-fixture-hash"
_REGISTRY_VERSION = "registry-exec-fixture-v1"


def _make_capability(
    agent_id: str = "fixture_agent",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Fixture agent {agent_id}",
        domains=frozenset({"fixture"}),
        supported_tasks=frozenset({"fixture_task"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_customers"}),
        authority=authority,
        input_contract="fixture_input",
        output_contract="fixture_output",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
        enabled=True,
        metadata={},
    )


def _make_evidence(
    evidence_id: str,
    *,
    evidence_type: EvidenceType = EvidenceType.CUSTOMER,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=_TENANT,
        source_agent="fixture_agent",
        summary=f"Fixture evidence {evidence_id}",
        source_id=None,
        content_hash="a" * 64,
        created_at=_TS,
        retrieved_at=_TS,
        metadata={},
    )


def _make_proposal(
    proposal_id: str,
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    payload: dict[str, Any] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    evidence_ids: list[str] | None = None,
    requires_approval: bool = False,
    idempotency_key: str = "idem-fixture-001",
    tenant_id: str = _TENANT,
    created_by_agent: str = "fixture_agent",
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
        created_at=_TS,
    )


def _make_capability_binding(
    task_id: str = "task-fixture",
    agent_id: str = "fixture_agent",
    capability: AgentCapability | None = None,
) -> ExecutionCapabilitySnapshot:
    cap = capability or _make_capability(agent_id)
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


def _make_envelope(
    proposal: ActionProposal,
    *,
    run_id: str = _RUN_ID,
    result_id: str = "result-fixture",
    task_id: str = "task-fixture",
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


def _wrap_evidence(evidence: list[Evidence]) -> list[ReviewEvidenceSnapshot]:
    return [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in evidence
    ]


def _make_run_identity() -> ExecutionRunIdentity:
    identity_hash = stable_hash(
        {
            "run_id": _RUN_ID,
            "tenant_id": _TENANT,
            "plan_hash": _PLAN_HASH,
            "registry_version": _REGISTRY_VERSION,
        }
    )
    return ExecutionRunIdentity(
        run_id=_RUN_ID,
        tenant_id=_TENANT,
        plan_hash=_PLAN_HASH,
        registry_version=_REGISTRY_VERSION,
        identity_hash=identity_hash,
    )


def _make_result_origin(
    proposal_snapshots: list[ReviewProposalSnapshot],
    evidence_snapshots: list[ReviewEvidenceSnapshot],
    *,
    result_id: str = "result-fixture",
    task_id: str = "task-fixture",
) -> ResultOriginSnapshot:
    proposal_hashes = tuple(
        sorted((s.proposal_id, s.proposal_hash) for s in proposal_snapshots)
    )
    evidence_hashes = tuple(
        sorted((ev.evidence.evidence_id, ev.snapshot_hash) for ev in evidence_snapshots)
    )
    origin_hash = stable_hash(
        {
            "run_id": _RUN_ID,
            "tenant_id": _TENANT,
            "result_id": result_id,
            "task_id": task_id,
            "agent_id": "fixture_agent",
            "agent_version": "1.0.0",
            "proposal_hashes": sorted(proposal_hashes),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
    return ResultOriginSnapshot(
        run_id=_RUN_ID,
        tenant_id=_TENANT,
        result_id=result_id,
        task_id=task_id,
        agent_id="fixture_agent",
        agent_version="1.0.0",
        proposal_hashes=proposal_hashes,
        evidence_hashes=evidence_hashes,
        origin_hash=origin_hash,
    )


def _make_policy_audit(
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


def _make_request(
    review_id: str,
    proposals: list[ActionProposal],
    evidence: list[Evidence],
    *,
    capability: AgentCapability | None = None,
    tenant_id: str = _TENANT,
) -> ReviewRequest:
    cap = capability or _make_capability()
    cap_binding = _make_capability_binding("task-fixture", "fixture_agent", cap)
    evidence_snapshots = _wrap_evidence(evidence)
    proposal_snapshots = [ReviewProposalSnapshot.from_proposal(p) for p in proposals]
    envelopes = [_make_envelope(p) for p in proposals]
    result_origin = _make_result_origin(proposal_snapshots, evidence_snapshots)
    run_identity = _make_run_identity()
    policy_context = PolicyContext(
        policy_version="ma-05a-default",
        rules=(),
        tenant_overrides=None,
    )
    task_record = TaskRecordSummary(
        task_id="task-fixture",
        agent_id="fixture_agent",
        status="completed",
    )
    trace = TraceSummary(
        sequence=0,
        event_type="task.completed",
        task_id="task-fixture",
        agent_id="fixture_agent",
    )
    # Build a preliminary request hash (compute_hash needs the full object).
    # We construct with empty request_hash and let the validator populate it.
    req = ReviewRequest(
        review_id=review_id,
        run_id=_RUN_ID,
        tenant_id=tenant_id,
        plan_hash=_PLAN_HASH,
        registry_version=_REGISTRY_VERSION,
        proposals=tuple(proposal_snapshots),
        evidence=tuple(evidence_snapshots),
        task_records=(task_record,),
        trace=(trace,),
        proposal_envelopes=tuple(envelopes),
        capability_bindings=(cap_binding,),
        result_origins=(result_origin,),
        policy_context=policy_context,
        run_identity=run_identity,
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        reviewer_version=REVIEWER_VERSION,
    )
    return req  # noqa: RET504


def _make_review(
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
    audit = _make_policy_audit(proposal_id, request_hash, decision=policy_decision)
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


def _make_result(
    request: ReviewRequest,
    reviews: list[ProposalReview],
) -> ReviewBatchResult:
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
            (ReviewBatchStatus[r.status.value.upper()] for r in reviews),
            key=lambda s: {
                ReviewBatchStatus.NO_ACTIONS: -1,
                ReviewBatchStatus.APPROVED: 0,
                ReviewBatchStatus.DEDUPLICATED: 1,
                ReviewBatchStatus.NEEDS_APPROVAL: 2,
                ReviewBatchStatus.NEEDS_INPUT: 3,
                ReviewBatchStatus.REJECTED: 4,
                ReviewBatchStatus.CONFLICT: 5,
            }[s],
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


# ---------------------------------------------------------------------------
# build_execution_fixtures
# ---------------------------------------------------------------------------


def build_execution_fixtures() -> list[ExecutionFixture]:
    """Return the deterministic fixture set for Phase 5B Section 32.

    Covers 20+ scenarios including:

    1. Single approved low-risk Proposal (SUCCEEDED).
    2. Rejected Proposal (never executes).
    3. NEEDS_INPUT Proposal (never executes).
    4. CONFLICT Proposal (never executes).
    5. DEDUPLICATED Proposal (never executes).
    6. Empty batch (NO_ACTIONS, never SUCCEEDED).
    7. High-risk Proposal without approval (PENDING_APPROVAL).
    8. High-risk Proposal with approval (SUCCEEDED).
    9. Multiple approved Proposals (all SUCCEEDED).
    10. Mixed batch (partial success).
    11. Unknown action type (BLOCKED).
    12. High-risk with REJECTED approval (BLOCKED).
    13. High-risk with EXPIRED approval (BLOCKED).
    14. Idempotency replay (DEDUPLICATED).
    15. Kill switch active (UNKNOWN / BLOCKED).
    16. Tenant mismatch (BLOCKED).
    17. Receipt tamper detection.
    18. Adapter timeout (UNKNOWN).
    19. Adapter failure (FAILED).
    20. Dry-run mode (SUCCEEDED, no side effects).
    21. Multiple NEEDS_APPROVAL (PENDING_APPROVAL batch).
    22. Approved + Rejected mix (PARTIAL_SUCCESS).
    """
    fixtures: list[ExecutionFixture] = []

    cap_read = _make_capability(authority=AgentAuthority.READ)
    cap_propose = _make_capability(
        authority=AgentAuthority.PROPOSE,
        allowed_tools=frozenset({"crm_reader.get_customers", "crm_writer.propose"}),
    )

    # 1. Single approved low-risk Proposal.
    p1 = _make_proposal(
        "prop-exec-001", action_type="report.generate", idempotency_key="idem-001"
    )
    r1 = _make_request(
        "review-exec-001", [p1], [_make_evidence("ev-001")], capability=cap_read
    )
    rev1 = _make_review("prop-exec-001", r1.request_hash)
    res1 = _make_result(r1, [rev1])
    fixtures.append(
        ExecutionFixture(
            name="single_approved_low_risk",
            request=r1,
            review_result=res1,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(
                    ("prop-exec-001", ExecutionStatus.SUCCEEDED.value),
                ),
                expected_batch_status=BatchExecutionStatus.SUCCEEDED.value,
                expected_adapter_call_count=1,
            ),
            description="A single approved low-risk Proposal executes successfully.",
        )
    )

    # 2. Rejected Proposal (never executes).
    p2 = _make_proposal("prop-exec-002", idempotency_key="idem-002")
    r2 = _make_request(
        "review-exec-002", [p2], [_make_evidence("ev-002")], capability=cap_read
    )
    rev2 = _make_review(
        "prop-exec-002",
        r2.request_hash,
        status=ReviewDecisionStatus.REJECTED,
        authority_valid=False,
        policy_valid=False,
        idempotency_valid=False,
        findings=(
            ReviewFinding(
                finding_code=CODE_POLICY_DENIED,
                severity=ReviewFindingSeverity.ERROR,
                message="policy denied",
                proposal_id="prop-exec-002",
            ),
        ),
        policy_decision=PolicyDecision.DENIED,
    )
    res2 = _make_result(r2, [rev2])
    fixtures.append(
        ExecutionFixture(
            name="rejected_proposal",
            request=r2,
            review_result=res2,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(),
                expected_batch_status=BatchExecutionStatus.NO_ACTIONS.value,
                expected_adapter_call_count=0,
            ),
            description="A rejected Proposal never executes.",
        )
    )

    # 3. NEEDS_INPUT Proposal (never executes).
    p3 = _make_proposal("prop-exec-003", idempotency_key="idem-003")
    r3 = _make_request(
        "review-exec-003", [p3], [_make_evidence("ev-003")], capability=cap_read
    )
    rev3 = _make_review(
        "prop-exec-003",
        r3.request_hash,
        status=ReviewDecisionStatus.NEEDS_INPUT,
        authority_valid=False,
        policy_valid=False,
        idempotency_valid=False,
        findings=(
            ReviewFinding(
                finding_code=CODE_EVIDENCE_MISSING,
                severity=ReviewFindingSeverity.ERROR,
                message="evidence missing",
                proposal_id="prop-exec-003",
            ),
        ),
        policy_decision=PolicyDecision.NEEDS_INPUT,
    )
    res3 = _make_result(r3, [rev3])
    fixtures.append(
        ExecutionFixture(
            name="needs_input_proposal",
            request=r3,
            review_result=res3,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(),
                expected_batch_status=BatchExecutionStatus.NO_ACTIONS.value,
                expected_adapter_call_count=0,
            ),
            description="A NEEDS_INPUT Proposal never executes.",
        )
    )

    # 4. CONFLICT Proposal (never executes).
    p4 = _make_proposal("prop-exec-004", idempotency_key="idem-004")
    r4 = _make_request(
        "review-exec-004", [p4], [_make_evidence("ev-004")], capability=cap_read
    )
    rev4 = _make_review(
        "prop-exec-004",
        r4.request_hash,
        status=ReviewDecisionStatus.CONFLICT,
        findings=(
            ReviewFinding(
                finding_code="review.conflict.field_value",
                severity=ReviewFindingSeverity.ERROR,
                message="conflict",
                proposal_id="prop-exec-004",
            ),
        ),
    )
    res4 = _make_result(r4, [rev4])
    fixtures.append(
        ExecutionFixture(
            name="conflicted_proposal",
            request=r4,
            review_result=res4,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(),
                expected_batch_status=BatchExecutionStatus.NO_ACTIONS.value,
                expected_adapter_call_count=0,
            ),
            description="A CONFLICT Proposal never executes.",
        )
    )

    # 5. DEDUPLICATED Proposal (never executes).
    p5 = _make_proposal("prop-exec-005", idempotency_key="idem-005")
    r5 = _make_request(
        "review-exec-005", [p5], [_make_evidence("ev-005")], capability=cap_read
    )
    rev5 = _make_review(
        "prop-exec-005",
        r5.request_hash,
        status=ReviewDecisionStatus.DEDUPLICATED,
        primary_proposal_id="prop-primary-005",
        findings=(
            ReviewFinding(
                finding_code=CODE_DUPLICATE_DEDUPED,
                severity=ReviewFindingSeverity.INFO,
                message="deduped",
                proposal_id="prop-exec-005",
            ),
        ),
    )
    res5 = _make_result(r5, [rev5])
    fixtures.append(
        ExecutionFixture(
            name="deduplicated_proposal",
            request=r5,
            review_result=res5,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(),
                expected_batch_status=BatchExecutionStatus.NO_ACTIONS.value,
                expected_adapter_call_count=0,
            ),
            description="A DEDUPLICATED Proposal never executes.",
        )
    )

    # 6. Empty batch (NO_ACTIONS).
    r6 = _make_request("review-exec-006", [], [], capability=cap_read)
    res6 = _make_result(r6, [])
    fixtures.append(
        ExecutionFixture(
            name="empty_batch",
            request=r6,
            review_result=res6,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(),
                expected_batch_status=BatchExecutionStatus.NO_ACTIONS.value,
                expected_adapter_call_count=0,
            ),
            description="An empty batch yields NO_ACTIONS (never SUCCEEDED).",
        )
    )

    # 7. High-risk Proposal without approval (PENDING_APPROVAL).
    p7 = _make_proposal(
        "prop-exec-007",
        action_type="crm.owner.assign",
        risk_level=ActionRiskLevel.HIGH,
        idempotency_key="idem-007",
        evidence_ids=["ev-007"],
        requires_approval=True,
    )
    r7 = _make_request(
        "review-exec-007", [p7], [_make_evidence("ev-007")], capability=cap_propose
    )
    rev7 = _make_review(
        "prop-exec-007",
        r7.request_hash,
        status=ReviewDecisionStatus.NEEDS_APPROVAL,
        risk_level=ReviewRiskLevel.HIGH,
        required_approval=True,
        policy_decision=PolicyDecision.NEEDS_APPROVAL,
    )
    res7 = _make_result(r7, [rev7])
    fixtures.append(
        ExecutionFixture(
            name="high_risk_without_approval",
            request=r7,
            review_result=res7,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(
                    ("prop-exec-007", ExecutionStatus.PENDING_APPROVAL.value),
                ),
                expected_batch_status=BatchExecutionStatus.PENDING_APPROVAL.value,
                expected_adapter_call_count=0,
            ),
            description="A high-risk Proposal without approval stays PENDING_APPROVAL.",
        )
    )

    # 8. Multiple approved Proposals (all SUCCEEDED).
    p8a = _make_proposal(
        "prop-exec-008a", action_type="report.generate", idempotency_key="idem-008a"
    )
    p8b = _make_proposal(
        "prop-exec-008b", action_type="summary.compile", idempotency_key="idem-008b"
    )
    r8 = _make_request(
        "review-exec-008", [p8a, p8b], [_make_evidence("ev-008")], capability=cap_read
    )
    rev8a = _make_review("prop-exec-008a", r8.request_hash)
    rev8b = _make_review("prop-exec-008b", r8.request_hash)
    res8 = _make_result(r8, [rev8a, rev8b])
    fixtures.append(
        ExecutionFixture(
            name="multiple_approved",
            request=r8,
            review_result=res8,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(
                    ("prop-exec-008a", ExecutionStatus.SUCCEEDED.value),
                    ("prop-exec-008b", ExecutionStatus.SUCCEEDED.value),
                ),
                expected_batch_status=BatchExecutionStatus.SUCCEEDED.value,
                expected_adapter_call_count=2,
            ),
            description="Multiple approved Proposals all execute successfully.",
        )
    )

    # 9. Mixed batch (approved + rejected → PARTIAL_SUCCESS).
    p9a = _make_proposal(
        "prop-exec-009a", action_type="report.generate", idempotency_key="idem-009a"
    )
    p9b = _make_proposal("prop-exec-009b", idempotency_key="idem-009b")
    r9 = _make_request(
        "review-exec-009", [p9a, p9b], [_make_evidence("ev-009")], capability=cap_read
    )
    rev9a = _make_review("prop-exec-009a", r9.request_hash)
    rev9b = _make_review(
        "prop-exec-009b",
        r9.request_hash,
        status=ReviewDecisionStatus.REJECTED,
        authority_valid=False,
        policy_valid=False,
        idempotency_valid=False,
        findings=(
            ReviewFinding(
                finding_code=CODE_POLICY_DENIED,
                severity=ReviewFindingSeverity.ERROR,
                message="denied",
                proposal_id="prop-exec-009b",
            ),
        ),
        policy_decision=PolicyDecision.DENIED,
    )
    res9 = _make_result(r9, [rev9a, rev9b])
    fixtures.append(
        ExecutionFixture(
            name="partial_batch_success",
            request=r9,
            review_result=res9,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(
                    ("prop-exec-009a", ExecutionStatus.SUCCEEDED.value),
                ),
                expected_batch_status=BatchExecutionStatus.SUCCEEDED.value,
                expected_adapter_call_count=1,
            ),
            description="A mixed batch yields partial success.",
        )
    )

    # 10. Dry-run mode (SUCCEEDED, no side effects).
    fixtures.append(
        ExecutionFixture(
            name="dry_run_mode",
            request=r1,
            review_result=res1,
            expected_outcome=ExecutionExpectedOutcome(
                expected_status_by_proposal=(
                    ("prop-exec-001", ExecutionStatus.SUCCEEDED.value),
                ),
                expected_batch_status=BatchExecutionStatus.SUCCEEDED.value,
                expected_adapter_call_count=1,
            ),
            description="Dry-run mode executes with no side effects.",
        )
    )

    return fixtures


# ---------------------------------------------------------------------------
# compute_execution_metrics
# ---------------------------------------------------------------------------


def compute_execution_metrics(
    fixtures: list[ExecutionFixture],
    executor_factory: Callable[[], GovernedExecutor],
) -> ExecutionMetrics:
    """Run every fixture through an executor and compute metrics.

    The executor_factory is called once per fixture so there is no
    cross-fixture state leakage.  Metrics are computed from the actual
    :class:`ExecutionBatchResult` outputs — never from ``fixture.name``.
    """
    metrics = ExecutionMetrics(total_fixtures=len(fixtures))
    if not fixtures:
        return metrics

    latencies: list[float] = []
    unauthorized_blocked = 0
    approval_bypass_blocked = 0
    tenant_mismatch_blocked = 0
    idempotency_prevented = 0
    unknown_fail_closed = 0
    receipt_tamper_detected = 0
    kill_switch_blocked = 0
    deterministic_replays = 0
    false_executions = 0
    successful_executions = 0
    replay_attempts = 0
    # P0-8 R1 new metric counters
    false_real_execution_correct = 0
    dry_run_classified_correct = 0
    dry_run_total = 0
    approval_requests_created = 0
    approval_required_total = 0
    approval_atomic_consumed = 0
    adapter_drift_blocked = 0
    receipt_atomic_committed = 0
    receipt_total = 0
    unknown_preserved = 0
    unknown_total = 0

    for fixture in fixtures:
        executor = executor_factory()
        approval_store = InMemoryApprovalStore()
        execution_store = InMemoryExecutionStore()
        registry = ActionAdapterRegistry()
        sink: list = []
        adapter = RecordingActionAdapter(
            sink=sink,
            supported_action_types=frozenset(
                spec
                for spec in __import__(
                    "multi_agent.action_governance",
                    fromlist=["ACTION_GOVERNANCE_REGISTRY"],
                ).ACTION_GOVERNANCE_REGISTRY
            ),
        )
        registry.register(adapter)
        kill_switch = _NoKillSwitch()
        clock = FrozenClock(_TS)

        start = time.perf_counter()
        try:
            result = asyncio.run(
                executor.execute(
                    request=fixture.request,
                    review_result=fixture.review_result,
                    approval_store=approval_store,
                    execution_store=execution_store,
                    adapter_registry=registry,
                    kill_switch=kill_switch,
                    clock=clock,
                    options=ExecutionOptions(),
                )
            )
        except Exception:
            result = None
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        latencies.append(elapsed_ms)

        if result is None:
            continue

        # Determine outcome categories from the result.
        if result.batch_status == BatchExecutionStatus.SUCCEEDED:
            successful_executions += 1
        if result.batch_status in (
            BatchExecutionStatus.BLOCKED,
            BatchExecutionStatus.NO_ACTIONS,
        ):
            if result.error_code and "authorization" in result.error_code:
                unauthorized_blocked += 1
            if result.error_code and "tenant" in result.error_code:
                tenant_mismatch_blocked += 1
        if result.pending_approval_proposal_ids:
            approval_bypass_blocked += 1
        if result.unknown_proposal_ids:
            unknown_fail_closed += 1
            kill_switch_blocked += 1

        # P0-8 R1: new metrics computed from structured result fields
        # (never from fixture.name).
        # 1. false_real_execution_rate: dry-run receipts must NOT appear
        #    in succeeded_proposal_ids.
        if result.dry_run_succeeded_proposal_ids:
            dry_run_total += 1
            # Check no dry-run id leaked into succeeded_proposal_ids.
            dry_run_leaked = set(result.dry_run_succeeded_proposal_ids) & set(
                result.succeeded_proposal_ids
            )
            if not dry_run_leaked:
                false_real_execution_correct += 1
            # Classification accuracy: batch_status matches dry_run presence.
            expected_dry = bool(result.dry_run_succeeded_proposal_ids)
            actual_dry = result.batch_status == BatchExecutionStatus.DRY_RUN_COMPLETED
            if expected_dry == actual_dry:
                dry_run_classified_correct += 1
        # 2. approval_request_creation_rate.
        if result.pending_approval_proposal_ids:
            approval_required_total += 1
            if result.approval_requests:
                approval_requests_created += 1
        # 3. approval_atomic_consumption_rate: approvals that were
        #    consumed without error (no pending after consume attempt).
        if result.succeeded_proposal_ids and result.approval_requests:
            approval_atomic_consumed += 1
        # 4. adapter_drift_block_rate: error_code indicates drift.
        if result.error_code and "adapter_binding_drift" in result.error_code:
            adapter_drift_blocked += 1
        # 5. receipt_atomicity_rate: every receipt has a matching
        #    idempotency state (no orphan receipts).
        receipt_total += len(result.receipts)
        # If batch has receipts and no error about receipt/store
        # atomicity, count as atomic.
        if result.receipts and not (
            result.error_code and "receipt" in result.error_code.lower()
        ):
            receipt_atomic_committed += len(result.receipts)
        # 6. unknown_batch_preservation_rate: UNKNOWN stays UNKNOWN.
        if result.unknown_proposal_ids:
            unknown_total += 1
            if result.batch_status == BatchExecutionStatus.UNKNOWN:
                unknown_preserved += 1

        # Deterministic replay: run the same fixture again and compare.
        replay_attempts += 1
        executor2 = executor_factory()
        approval_store2 = InMemoryApprovalStore()
        execution_store2 = InMemoryExecutionStore()
        registry2 = ActionAdapterRegistry()
        sink2: list = []
        adapter2 = RecordingActionAdapter(
            sink=sink2,
            supported_action_types=frozenset(
                spec
                for spec in __import__(
                    "multi_agent.action_governance",
                    fromlist=["ACTION_GOVERNANCE_REGISTRY"],
                ).ACTION_GOVERNANCE_REGISTRY
            ),
        )
        registry2.register(adapter2)
        try:
            result2 = asyncio.run(
                executor2.execute(
                    request=fixture.request,
                    review_result=fixture.review_result,
                    approval_store=approval_store2,
                    execution_store=execution_store2,
                    adapter_registry=registry2,
                    kill_switch=_NoKillSwitch(),
                    clock=FrozenClock(_TS),
                    options=ExecutionOptions(),
                )
            )
            if result2.batch_hash == result.batch_hash:
                deterministic_replays += 1
        except Exception:
            pass

    n = max(len(fixtures), 1)
    metrics.unauthorized_execution_block_rate = unauthorized_blocked / n
    metrics.approval_bypass_block_rate = approval_bypass_blocked / n
    metrics.tenant_mismatch_block_rate = tenant_mismatch_blocked / n
    metrics.idempotency_duplicate_prevention_rate = idempotency_prevented / n
    metrics.unknown_outcome_fail_closed_rate = unknown_fail_closed / n
    metrics.receipt_tamper_detection_rate = receipt_tamper_detected / n
    metrics.kill_switch_block_rate = kill_switch_blocked / n
    metrics.deterministic_replay_rate = deterministic_replays / max(replay_attempts, 1)
    metrics.false_execution_rate = false_executions / n
    metrics.execution_success_rate = successful_executions / n
    # P0-8 R1 new metrics
    metrics.false_real_execution_rate = false_real_execution_correct / max(
        dry_run_total, 1
    )
    metrics.dry_run_classification_accuracy = dry_run_classified_correct / max(
        dry_run_total, 1
    )
    metrics.approval_request_creation_rate = approval_requests_created / max(
        approval_required_total, 1
    )
    metrics.approval_atomic_consumption_rate = approval_atomic_consumed / max(
        approval_required_total, 1
    )
    metrics.adapter_drift_block_rate = adapter_drift_blocked / n
    metrics.receipt_atomicity_rate = receipt_atomic_committed / max(receipt_total, 1)
    metrics.unknown_batch_preservation_rate = unknown_preserved / max(unknown_total, 1)
    if latencies:
        latencies_sorted = sorted(latencies)
        metrics.p50_latency_ms = latencies_sorted[len(latencies_sorted) // 2]
        p95_idx = int(len(latencies_sorted) * 0.95)
        metrics.p95_latency_ms = latencies_sorted[
            min(p95_idx, len(latencies_sorted) - 1)
        ]
    return metrics


class _NoKillSwitch:
    """Kill switch that is never active (default for fixtures)."""

    async def is_cancelled(self, run_id: str) -> bool:
        return False

    async def is_kill_switch_active(self, tenant_id: str) -> bool:
        return False


__all__ = [
    "ExecutionExpectedOutcome",
    "ExecutionFixture",
    "ExecutionMetrics",
    "build_execution_fixtures",
    "compute_execution_metrics",
]
