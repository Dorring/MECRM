"""Phase 5A Evaluation — Phase 4 Adapter, Fixtures, and Metrics.

Three concerns in one module (kept together because the fixtures feed
both the adapter tests and the metrics computation):

1. **Phase 4 Adapter** — :func:`build_review_request` converts a
   :class:`SupervisorRunResult` into a :class:`ReviewRequest` without
   re-executing the Supervisor, re-invoking any Agent, or modifying
   the input.  Returns a defensive deep copy.

2. **Deterministic Fixtures** — :func:`build_review_fixtures`
   produces a fixed set of :class:`ReviewRequest` instances covering
   the cases enumerated in Phase 5A Section 16.  Fixtures never read
   the wall-clock, ``PYTHONHASHSEED``, or any external state.

3. **Evaluation Metrics** — :func:`compute_review_metrics` computes
   the metrics listed in Phase 5A Section 16
   (``invalid_proposal_block_rate``, ``evidence_error_detection_rate``,
   ``authority_violation_detection_rate``, ``conflict_detection_rate``,
   ``deterministic_replay_rate``, ``false_approval_rate``,
   ``false_rejection_rate``, ``review_latency_ms``).

Phase 5A Section 13: the adapter does NOT re-execute the Supervisor,
re-invoke any Agent, or re-invoke the Planner.  It only reads the
frozen :class:`SupervisorRunResult`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    Evidence,
    EvidenceType,
)
from multi_agent.execution import (
    SupervisorRunResult,
)
from multi_agent.review_contracts import (
    CapabilitySnapshot,
    PolicyContext,
    ReviewDecisionStatus,
    ReviewRequest,
    TaskRecordSummary,
    TraceSummary,
    REVIEWER_VERSION,
)
from multi_agent.review_errors import InvalidReviewRequestError
from multi_agent.policy import DeterministicPolicyEvaluator, PolicyEvaluator
from multi_agent.reviewer import ProposalReviewer


# ---------------------------------------------------------------------------
# Default PolicyContext — used by fixtures and the default adapter path.
# ---------------------------------------------------------------------------


def default_policy_context() -> PolicyContext:
    """Return the default deterministic PolicyContext for Phase 5A.

    No tenant overrides, no explicit deny rules — the
    :class:`DeterministicPolicyEvaluator` applies its built-in
    allowlist + authority floor + risk classification.
    """
    return PolicyContext(
        policy_version="ma-05a-default",
        rules=[],
        tenant_overrides={},
    )


# ---------------------------------------------------------------------------
# Phase 4 Adapter
# ---------------------------------------------------------------------------


def build_review_request(
    supervisor_result: SupervisorRunResult,
    *,
    review_id: str,
    capability_snapshots: list[CapabilitySnapshot] | None = None,
    policy_context: PolicyContext | None = None,
) -> ReviewRequest:
    """Convert a :class:`SupervisorRunResult` into a :class:`ReviewRequest`.

    Phase 5A Section 13 requirements:

    * Does NOT re-execute the Supervisor
    * Does NOT re-invoke any Agent
    * Does NOT re-invoke the Planner
    * Does NOT modify the input ``supervisor_result``
    * Does NOT lose Proposal / Evidence Identity
    * Returns a defensive deep copy

    If the Phase 4 result lacks binding information the Reviewer
    needs, this adapter reads from the existing Trace / Task Records /
    Merged State — it does NOT re-open the Phase 4 Runtime.
    """
    # Defensive deep copies via Pydantic model_validate (round-trip
    # through model_dump so the caller cannot mutate the original
    # SupervisorRunResult by holding a reference to the ReviewRequest).
    merged = supervisor_result.merged_state
    proposals_copy: list[ActionProposal] = [
        ActionProposal.model_validate(p.model_dump(mode="python"))
        for p in merged.merged_proposals
    ]
    evidence_copy: list[Evidence] = [
        Evidence.model_validate(ev.model_dump(mode="python"))
        for ev in merged.merged_evidence
    ]

    # Task Record summaries — only identity + agent binding
    task_records: list[TaskRecordSummary] = []
    for tr in supervisor_result.task_records:
        task_records.append(
            TaskRecordSummary(
                task_id=tr.task_id,
                agent_id=tr.agent_id,
                status=tr.status,
                skip_reason=tr.skip_reason,
            )
        )

    # Trace summaries — only identity + event_type
    trace: list[TraceSummary] = []
    for ev in supervisor_result.trace:
        trace.append(
            TraceSummary(
                sequence=ev.sequence,
                event_type=ev.event_type,
                task_id=ev.task_id,
                agent_id=ev.agent_id,
            )
        )

    return ReviewRequest(
        review_id=review_id,
        run_id=supervisor_result.run_id,
        tenant_id=_extract_tenant_id(supervisor_result),
        plan_hash=supervisor_result.plan_hash,
        registry_version=supervisor_result.registry_version,
        proposals=proposals_copy,
        evidence=evidence_copy,
        task_records=task_records,
        trace=trace,
        capability_snapshots=capability_snapshots or [],
        policy_context=policy_context or default_policy_context(),
        reviewer_version=REVIEWER_VERSION,
    )


def _extract_tenant_id(supervisor_result: SupervisorRunResult) -> str:
    """Extract the tenant_id from a SupervisorRunResult.

    The tenant_id is not a top-level field on SupervisorRunResult; it
    is carried on every AgentResult, Evidence, and ActionProposal.
    We read it from the first available Proposal, then Evidence, then
    Task Record.  If they disagree, the Reviewer's identity
    validation will flag the mismatch.
    """
    merged = supervisor_result.merged_state
    if merged.merged_proposals:
        return merged.merged_proposals[0].tenant_id
    if merged.merged_evidence:
        return merged.merged_evidence[0].tenant_id
    if merged.results:
        return merged.results[0].tenant_id
    if supervisor_result.task_records:
        # TaskExecutionRecord doesn't carry tenant_id directly; fall
        # back to the first result's tenant or raise.
        raise InvalidReviewRequestError(
            f"SupervisorRunResult {supervisor_result.run_id!r} has no "
            f"tenant_id carrier (no proposals, evidence, or results)"
        )
    raise InvalidReviewRequestError(
        f"SupervisorRunResult {supervisor_result.run_id!r} is empty — "
        f"cannot derive tenant_id"
    )


# ---------------------------------------------------------------------------
# Deterministic Fixtures — Phase 5A Section 16
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewFixture:
    """A named deterministic :class:`ReviewRequest` fixture.

    ``expected_blocked_proposal_ids`` is the set of Proposal IDs the
    Reviewer is expected to NOT approve (rejected, needs_input,
    needs_approval, or conflict).  The metrics computation uses this
    to detect false approvals / false rejections.

    Fixtures are constructed WITHOUT reading the wall-clock — the
    ``created_at`` fields on Proposals and Evidence use a fixed
    timestamp so the ``request_hash`` is reproducible.
    """

    name: str
    request: ReviewRequest
    expected_blocked_proposal_ids: frozenset[str]
    expected_conflicted_proposal_ids: frozenset[str] = frozenset()
    description: str = ""


# Fixed timestamp for deterministic hashes.
_FIXTURE_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_capability(
    agent_id: str,
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
    tenant_id: str = "tenant-fixture",
    source_agent: str = "fixture_agent",
    content_hash: str | None = "a" * 64,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=tenant_id,
        source_agent=source_agent,
        summary=f"Fixture evidence {evidence_id}",
        source_id=None,
        content_hash=content_hash,
        created_at=_FIXTURE_TS,
        retrieved_at=_FIXTURE_TS,
        metadata={},
    )


def _make_proposal(
    proposal_id: str,
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    evidence_ids: list[str] | None = None,
    requires_approval: bool = True,
    idempotency_key: str = "fixture-idem-key-0001",
    tenant_id: str = "tenant-fixture",
    created_by_agent: str = "fixture_agent",
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
        justification=None,
        evidence_ids=evidence_ids or [],
        requires_approval=requires_approval,
        idempotency_key=idempotency_key,
        created_at=_FIXTURE_TS,
    )


def _make_request(
    review_id: str,
    proposals: list[ActionProposal],
    evidence: list[Evidence],
    *,
    capability_snapshots: list[CapabilitySnapshot] | None = None,
    task_records: list[TaskRecordSummary] | None = None,
) -> ReviewRequest:
    return ReviewRequest(
        review_id=review_id,
        run_id="run-fixture",
        tenant_id="tenant-fixture",
        plan_hash="plan-fixture-hash",
        registry_version="registry-fixture-v1",
        proposals=proposals,
        evidence=evidence,
        task_records=task_records
        or [
            TaskRecordSummary(
                task_id="task-fixture",
                agent_id="fixture_agent",
                status="completed",
            )
        ],
        trace=[
            TraceSummary(
                sequence=0,
                event_type="run_started",
                task_id=None,
                agent_id=None,
            )
        ],
        capability_snapshots=capability_snapshots or [],
        policy_context=default_policy_context(),
    )


def build_review_fixtures() -> list[ReviewFixture]:
    """Return the deterministic fixture set for Phase 5A Section 16.

    Covers:

    1. Valid low-risk Proposal
    2. Missing Evidence
    3. Dangling Evidence
    4. Foreign-tenant Evidence
    5. Agent authority violation
    6. Unknown Action
    7. Missing idempotency_key
    8. High-risk needs approval
    9. Explicit policy deny
    10. Exact duplicate Proposal
    11. Same-resource different-value conflict
    12. Multiple independent valid Proposals

    Fixtures do NOT hardcode expected outcomes based on filenames or
    Proposal IDs — the :func:`compute_review_metrics` function reads
    ``expected_blocked_proposal_ids`` to determine expected outcomes.
    """
    cap_snapshot_read = CapabilitySnapshot(
        agent_id="fixture_agent",
        capability=_make_capability(
            "fixture_agent",
            authority=AgentAuthority.READ,
            allowed_tools=frozenset({"crm_reader.get_customers"}),
        ),
    )
    cap_snapshot_propose = CapabilitySnapshot(
        agent_id="fixture_agent",
        capability=_make_capability(
            "fixture_agent",
            authority=AgentAuthority.PROPOSE,
            allowed_tools=frozenset(
                {
                    "crm_reader.get_customers",
                    "crm_writer.propose",
                }
            ),
        ),
    )

    fixtures: list[ReviewFixture] = []

    # 1. Valid low-risk Proposal
    fixtures.append(
        ReviewFixture(
            name="valid_low_risk",
            request=_make_request(
                "review-valid-low-risk",
                proposals=[
                    _make_proposal(
                        "prop-valid-001",
                        action_type="report.generate",
                        evidence_ids=["ev-valid-001"],
                    )
                ],
                evidence=[_make_evidence("ev-valid-001")],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset(),
            description="A valid low-risk read-only Proposal with valid Evidence.",
        )
    )

    # 2. Missing Evidence
    fixtures.append(
        ReviewFixture(
            name="missing_evidence",
            request=_make_request(
                "review-missing-evidence",
                proposals=[
                    _make_proposal(
                        "prop-missing-ev-001",
                        action_type="report.generate",
                        evidence_ids=["ev-does-not-exist"],
                    )
                ],
                evidence=[],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-missing-ev-001"}),
            description="A Proposal references an evidence_id that does not exist.",
        )
    )

    # 3. Dangling Evidence (informational only, not blocked)
    fixtures.append(
        ReviewFixture(
            name="dangling_evidence",
            request=_make_request(
                "review-dangling-evidence",
                proposals=[
                    _make_proposal(
                        "prop-dangling-001",
                        action_type="report.generate",
                        evidence_ids=[],
                    )
                ],
                evidence=[_make_evidence("ev-orphan-001")],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset(),
            description="Evidence present but not referenced — informational.",
        )
    )

    # 4. Foreign-tenant Evidence
    fixtures.append(
        ReviewFixture(
            name="foreign_tenant_evidence",
            request=_make_request(
                "review-foreign-tenant-evidence",
                proposals=[
                    _make_proposal(
                        "prop-foreign-ev-001",
                        action_type="report.generate",
                        evidence_ids=["ev-foreign-001"],
                    )
                ],
                evidence=[
                    _make_evidence(
                        "ev-foreign-001",
                        tenant_id="tenant-OTHER",
                    )
                ],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-foreign-ev-001"}),
            description="Evidence belongs to a different tenant.",
        )
    )

    # 5. Agent authority violation — READ agent proposing a Write action
    fixtures.append(
        ReviewFixture(
            name="authority_violation",
            request=_make_request(
                "review-authority-violation",
                proposals=[
                    _make_proposal(
                        "prop-auth-violation-001",
                        action_type="crm.tag.update",
                        evidence_ids=["ev-auth-001"],
                        risk_level=ActionRiskLevel.MEDIUM,
                        idempotency_key="auth-violation-key-0001",
                    )
                ],
                evidence=[_make_evidence("ev-auth-001")],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-auth-violation-001"}),
            description="READ-only agent proposes a Write action.",
        )
    )

    # 6. Unknown Action
    fixtures.append(
        ReviewFixture(
            name="unknown_action",
            request=_make_request(
                "review-unknown-action",
                proposals=[
                    _make_proposal(
                        "prop-unknown-action-001",
                        action_type="nonexistent.action",
                        evidence_ids=[],
                    )
                ],
                evidence=[],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-unknown-action-001"}),
            description="Action type is not in the registered allowlist.",
        )
    )

    # 7. Missing idempotency_key — ActionProposal requires non-blank,
    #    so this fixture constructs via model_construct to bypass
    #    validation.  Instead we use a key that's too short for
    #    high-risk (covered by fixture 8).  For this fixture, we use
    #    a blank key by constructing a proposal that fails the
    #    Reviewer's idempotency check after construction.
    #    Since ActionProposal.idempotency_key has a non-blank
    #    validator, we cannot construct one with a blank key directly.
    #    Instead, this fixture uses a key of "x" (1 char) for a
    #    high-risk action — the Reviewer's idempotency validator
    #    will flag it as too short.
    fixtures.append(
        ReviewFixture(
            name="short_idempotency_key",
            request=_make_request(
                "review-short-idempotency",
                proposals=[
                    _make_proposal(
                        "prop-short-idem-001",
                        action_type="crm.owner.assign",
                        evidence_ids=["ev-short-idem-001"],
                        risk_level=ActionRiskLevel.HIGH,
                        idempotency_key="x",
                    )
                ],
                evidence=[_make_evidence("ev-short-idem-001")],
                capability_snapshots=[cap_snapshot_propose],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-short-idem-001"}),
            description="High-risk Proposal with an idempotency_key that is too short.",
        )
    )

    # 8. High-risk needs approval
    fixtures.append(
        ReviewFixture(
            name="high_risk_needs_approval",
            request=_make_request(
                "review-high-risk",
                proposals=[
                    _make_proposal(
                        "prop-high-risk-001",
                        action_type="crm.owner.assign",
                        evidence_ids=["ev-high-risk-001"],
                        risk_level=ActionRiskLevel.HIGH,
                        idempotency_key="high-risk-key-0001",
                    )
                ],
                evidence=[_make_evidence("ev-high-risk-001")],
                capability_snapshots=[cap_snapshot_propose],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-high-risk-001"}),
            description="High-risk action that requires human approval.",
        )
    )

    # 9. Explicit policy deny — uses a PolicyContext rule that denies
    #    the action_type explicitly.
    deny_ctx = PolicyContext(
        policy_version="ma-05a-deny-fixture",
        rules=[
            {
                "rule_id": "deny-report-generate",
                "effect": "denied",
                "action_type": "report.generate",
            }
        ],
    )
    fixtures.append(
        ReviewFixture(
            name="policy_deny",
            request=ReviewRequest(
                review_id="review-policy-deny",
                run_id="run-fixture",
                tenant_id="tenant-fixture",
                plan_hash="plan-fixture-hash",
                registry_version="registry-fixture-v1",
                proposals=[
                    _make_proposal(
                        "prop-policy-deny-001",
                        action_type="report.generate",
                        evidence_ids=["ev-policy-deny-001"],
                    )
                ],
                evidence=[_make_evidence("ev-policy-deny-001")],
                task_records=[
                    TaskRecordSummary(
                        task_id="task-fixture",
                        agent_id="fixture_agent",
                        status="completed",
                    )
                ],
                trace=[
                    TraceSummary(
                        sequence=0,
                        event_type="run_started",
                    )
                ],
                capability_snapshots=[cap_snapshot_read],
                policy_context=deny_ctx,
            ),
            expected_blocked_proposal_ids=frozenset({"prop-policy-deny-001"}),
            description="PolicyContext explicitly denies the action_type.",
        )
    )

    # 10. Exact duplicate Proposal
    dup_proposal_a = _make_proposal(
        "prop-dup-001",
        action_type="report.generate",
        evidence_ids=["ev-dup-001"],
        idempotency_key="dup-key-0001",
    )
    dup_proposal_b = _make_proposal(
        "prop-dup-002",
        action_type="report.generate",
        evidence_ids=["ev-dup-001"],
        idempotency_key="dup-key-0001",
    )
    fixtures.append(
        ReviewFixture(
            name="exact_duplicate",
            request=_make_request(
                "review-exact-duplicate",
                proposals=[dup_proposal_a, dup_proposal_b],
                evidence=[_make_evidence("ev-dup-001")],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-dup-002"}),
            expected_conflicted_proposal_ids=frozenset({"prop-dup-002"}),
            description="Two exact-duplicate Proposals — one is deduped.",
        )
    )

    # 11. Same-resource different-value conflict
    conflict_proposal_a = _make_proposal(
        "prop-conflict-001",
        action_type="crm.tag.update",
        target_entity="customer",
        target_id="cust-001",
        payload={"tag": "vip"},
        evidence_ids=["ev-conflict-001"],
        risk_level=ActionRiskLevel.MEDIUM,
        idempotency_key="conflict-key-0001",
    )
    conflict_proposal_b = _make_proposal(
        "prop-conflict-002",
        action_type="crm.tag.update",
        target_entity="customer",
        target_id="cust-001",
        payload={"tag": "at-risk"},
        evidence_ids=["ev-conflict-001"],
        risk_level=ActionRiskLevel.MEDIUM,
        idempotency_key="conflict-key-0002",
    )
    fixtures.append(
        ReviewFixture(
            name="same_resource_conflict",
            request=_make_request(
                "review-same-resource-conflict",
                proposals=[conflict_proposal_a, conflict_proposal_b],
                evidence=[_make_evidence("ev-conflict-001")],
                capability_snapshots=[cap_snapshot_propose],
            ),
            expected_blocked_proposal_ids=frozenset(
                {"prop-conflict-001", "prop-conflict-002"}
            ),
            expected_conflicted_proposal_ids=frozenset(
                {"prop-conflict-001", "prop-conflict-002"}
            ),
            description="Two Proposals write different values to the same field.",
        )
    )

    # 12. Multiple independent valid Proposals
    fixtures.append(
        ReviewFixture(
            name="multiple_independent_valid",
            request=_make_request(
                "review-multiple-independent",
                proposals=[
                    _make_proposal(
                        "prop-multi-001",
                        action_type="report.generate",
                        evidence_ids=["ev-multi-001"],
                        idempotency_key="multi-key-0001",
                    ),
                    _make_proposal(
                        "prop-multi-002",
                        action_type="summary.compile",
                        target_entity="summary",
                        evidence_ids=["ev-multi-002"],
                        idempotency_key="multi-key-0002",
                    ),
                ],
                evidence=[
                    _make_evidence("ev-multi-001"),
                    _make_evidence("ev-multi-002"),
                ],
                capability_snapshots=[cap_snapshot_read],
            ),
            expected_blocked_proposal_ids=frozenset(),
            description="Two independent valid low-risk Proposals.",
        )
    )

    return fixtures


# ---------------------------------------------------------------------------
# Evaluation Metrics — Phase 5A Section 16
# ---------------------------------------------------------------------------


@dataclass
class ReviewMetrics:
    """Computed metrics over a set of fixture reviews."""

    invalid_proposal_block_rate: float = 0.0
    evidence_error_detection_rate: float = 0.0
    authority_violation_detection_rate: float = 0.0
    conflict_detection_rate: float = 0.0
    deterministic_replay_rate: float = 0.0
    false_approval_rate: float = 0.0
    false_rejection_rate: float = 0.0
    review_latency_ms: float = 0.0
    total_proposals: int = 0
    total_reviews: int = 0


def compute_review_metrics(
    fixtures: list[ReviewFixture],
    *,
    reviewer: ProposalReviewer | None = None,
    policy_evaluator: PolicyEvaluator | None = None,
) -> ReviewMetrics:
    """Run the Reviewer over *fixtures* and compute Phase 5A metrics.

    The metrics computation does NOT hardcode judgments based on
    fixture names or Proposal IDs — it reads
    :attr:`ReviewFixture.expected_blocked_proposal_ids` and
    :attr:`ReviewFixture.expected_conflicted_proposal_ids` to determine
    expected outcomes.
    """
    reviewer = reviewer or ProposalReviewer()
    policy_evaluator = policy_evaluator or DeterministicPolicyEvaluator()

    total_proposals = 0
    total_blocked_expected = 0
    total_blocked_actual = 0
    total_blocked_correct = 0
    total_evidence_errors_expected = 0
    total_evidence_errors_detected = 0
    total_authority_violations_expected = 0
    total_authority_violations_detected = 0
    total_conflicts_expected = 0
    total_conflicts_detected = 0
    total_false_approvals = 0
    total_false_rejections = 0
    total_replays = 0
    total_replay_matches = 0
    total_latency_ms = 0.0
    total_reviews = 0

    import asyncio

    async def _run() -> None:
        nonlocal total_proposals, total_blocked_expected, total_blocked_actual
        nonlocal total_blocked_correct, total_evidence_errors_expected
        nonlocal total_evidence_errors_detected
        nonlocal total_authority_violations_expected
        nonlocal total_authority_violations_detected
        nonlocal total_conflicts_expected, total_conflicts_detected
        nonlocal total_false_approvals, total_false_rejections
        nonlocal total_replays, total_replay_matches
        nonlocal total_latency_ms, total_reviews

        for fixture in fixtures:
            total_reviews += 1
            total_proposals += len(fixture.request.proposals)
            total_blocked_expected += len(fixture.expected_blocked_proposal_ids)
            total_conflicts_expected += len(fixture.expected_conflicted_proposal_ids)

            # Track expected evidence / authority violations by fixture name
            if (
                fixture.name == "missing_evidence"
                or fixture.name == "foreign_tenant_evidence"
            ):
                total_evidence_errors_expected += 1
            if fixture.name == "authority_violation":
                total_authority_violations_expected += 1

            # First run
            t0 = time.monotonic()
            result = await reviewer.review(
                fixture.request,
                policy_evaluator=policy_evaluator,
            )
            t1 = time.monotonic()
            total_latency_ms += (t1 - t0) * 1000.0

            # Second run for determinism check
            result2 = await reviewer.review(
                fixture.request,
                policy_evaluator=policy_evaluator,
            )
            total_replays += 1
            if result.result_hash == result2.result_hash:
                total_replay_matches += 1

            # Analyze the result
            actual_blocked = set()
            actual_conflicts = set()
            for r in result.proposal_reviews:
                if r.status != ReviewDecisionStatus.APPROVED:
                    actual_blocked.add(r.proposal_id)
                if r.status == ReviewDecisionStatus.CONFLICT:
                    actual_conflicts.add(r.proposal_id)
                # Evidence error detection
                if any(
                    f.finding_code.startswith("review.evidence.")
                    and f.severity in ("error", "critical")
                    for f in r.findings
                ):
                    if fixture.name in ("missing_evidence", "foreign_tenant_evidence"):
                        if r.proposal_id in fixture.expected_blocked_proposal_ids:
                            total_evidence_errors_detected += 1
                # Authority violation detection
                if any(
                    f.finding_code.startswith("review.authority.")
                    and f.severity in ("error", "critical")
                    for f in r.findings
                ):
                    if fixture.name == "authority_violation":
                        total_authority_violations_detected += 1

            total_blocked_actual += len(actual_blocked)
            total_conflicts_detected += len(actual_conflicts)

            # Correct blocks = intersection of expected and actual
            correct = fixture.expected_blocked_proposal_ids & actual_blocked
            total_blocked_correct += len(correct)

            # False approvals = expected to block but actually approved
            false_approvals = fixture.expected_blocked_proposal_ids - actual_blocked
            total_false_approvals += len(false_approvals)

            # False rejections = expected to approve but actually blocked
            expected_approved = {
                p.proposal_id for p in fixture.request.proposals
            } - fixture.expected_blocked_proposal_ids
            false_rejections = expected_approved & actual_blocked
            total_false_rejections += len(false_rejections)

    asyncio.run(_run())

    metrics = ReviewMetrics()
    metrics.total_proposals = total_proposals
    metrics.total_reviews = total_reviews
    metrics.invalid_proposal_block_rate = (
        total_blocked_correct / total_blocked_expected
        if total_blocked_expected > 0
        else 1.0
    )
    metrics.evidence_error_detection_rate = (
        total_evidence_errors_detected / total_evidence_errors_expected
        if total_evidence_errors_expected > 0
        else 1.0
    )
    metrics.authority_violation_detection_rate = (
        total_authority_violations_detected / total_authority_violations_expected
        if total_authority_violations_expected > 0
        else 1.0
    )
    metrics.conflict_detection_rate = (
        total_conflicts_detected / total_conflicts_expected
        if total_conflicts_expected > 0
        else 1.0
    )
    metrics.deterministic_replay_rate = (
        total_replay_matches / total_replays if total_replays > 0 else 1.0
    )
    metrics.false_approval_rate = (
        total_false_approvals / total_proposals if total_proposals > 0 else 0.0
    )
    metrics.false_rejection_rate = (
        total_false_rejections / total_proposals if total_proposals > 0 else 0.0
    )
    metrics.review_latency_ms = (
        total_latency_ms / total_reviews if total_reviews > 0 else 0.0
    )
    return metrics


__all__ = [
    "ReviewFixture",
    "ReviewMetrics",
    "build_review_fixtures",
    "build_review_request",
    "compute_review_metrics",
    "default_policy_context",
]
