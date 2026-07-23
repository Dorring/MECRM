"""Phase 5A Proposal Reviewer — main entry point.

The :class:`ProposalReviewer` is the single entry point for Phase 5A.
It consumes a :class:`ReviewRequest` (built from a Phase 4
:class:`SupervisorRunResult` via :func:`build_review_request` in
:mod:`multi_agent.review_evaluation`) and produces a
:class:`ReviewBatchResult`.

Per Phase 5A Section 12 execution order:

    verify ReviewRequest integrity
    → validate identity
    → validate evidence
    → validate authority
    → validate action/tool
    → evaluate policy
    → classify risk
    → resolve duplicates
    → detect conflicts
    → build per-proposal reviews
    → compute batch status
    → compute hashes
    → validate final result

Determinism rules (Phase 5A Section 12):

The same Request must not read current time, random state, live
registry, live handler, external LLM, or unfrozen configuration.
The Reviewer is a pure function of (request, policy_evaluator).

Phase 5A Section 3 reminder: ``approved`` means "Proposal has passed
review".  It NEVER means "Proposal has been executed".
"""

from __future__ import annotations

from typing import Any

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
    get_action_governance_spec,
    verify_governance_spec_integrity,
)
from multi_agent.conflict_resolution import (
    detect_conflicts,
    detect_duplicates,
)
from multi_agent.contracts import (
    ActionProposal,
    AgentAuthority,
    Evidence,
)
from multi_agent.evidence_review import (
    build_evidence_index,
    detect_dangling_evidence,
    detect_duplicate_evidence,
    validate_evidence_for_proposal,
)
from multi_agent.execution import ExecutionCapabilitySnapshot
from multi_agent.policy import (
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyEvaluationResult,
    PolicyEvaluator,
)
from multi_agent.review_contracts import (
    CODE_ACTION_TOOL_FORBIDDEN,
    CODE_ACTION_UNKNOWN_TYPE,
    CODE_AUTHORITY_EXCEEDS_SNAPSHOT,
    CODE_AUTHORITY_INSUFFICIENT,
    CODE_AUTHORITY_PROPOSE_EXECUTE,
    CODE_AUTHORITY_READ_ONLY_WRITE,
    CODE_EVIDENCE_MISSING,
    CODE_IDEMPOTENCY_BLANK,
    CODE_IDEMPOTENCY_INCONSISTENT,
    CODE_IDEMPOTENCY_MISSING,
    CODE_IDENTITY_DUPLICATE_PROPOSAL_ID,
    CODE_IDENTITY_MISMATCH,
    CODE_POLICY_DENIED,
    CODE_POLICY_NEEDS_INPUT,
    CODE_RISK_CRITICAL_NEEDS_APPROVAL,
    CODE_RISK_HIGH_NEEDS_APPROVAL,
    CODE_TENANT_CROSS_REFERENCE,
    CODE_TENANT_PII_EGRESS,
    CODE_TENANT_SECRET_FIELD,
    REJECTION_FINDING_CODES,
    REVIEWER_VERSION,
    CapabilitySnapshot,
    PolicyDecisionAudit,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewProposalEnvelope,
    ReviewRequest,
    ReviewRiskLevel,
    TaskRecordSummary,
    batch_status_priority,
    proposal_status_to_batch,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    PolicyEvaluationError,
    ReviewError,
    ReviewIntegrityError,
)
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Risk classification — Phase 5A Section 9
# ---------------------------------------------------------------------------

# R2 S14: risk classification reads from ACTION_GOVERNANCE_REGISTRY —
# no local lookup table.  A misbehaving Agent cannot lower the approval
# bar by declaring ``risk_level=low``; the Reviewer recomputes a
# canonical risk from the governance spec.


def classify_risk(proposal: ActionProposal) -> ReviewRiskLevel:
    """Return the canonical Reviewer-side risk level for *proposal*.

    R2 S14: reads from :data:`ACTION_GOVERNANCE_REGISTRY`.  Falls back
    to :class:`ReviewRiskLevel.MEDIUM` for unknown actions (the Policy
    evaluator will reject them separately).
    """
    spec = get_action_governance_spec(proposal.action_type)
    if spec is None:
        return ReviewRiskLevel.MEDIUM
    return spec.canonical_risk


# ---------------------------------------------------------------------------
# Authority validation — Phase 5A Section 7.3
# ---------------------------------------------------------------------------


def _authority_rank(a: AgentAuthority) -> int:
    return {
        AgentAuthority.READ: 0,
        AgentAuthority.PROPOSE: 1,
        AgentAuthority.EXECUTE: 2,
    }[a]


def validate_authority(
    proposal: ActionProposal,
    capability: CapabilitySnapshot | None,
) -> list[ReviewFinding]:
    """Validate that the Agent's Capability Snapshot authority is
    sufficient for the Action it proposed.

    Phase 5A Section 7.3:

    * Agent must have authority to propose the Action
    * Agent authority must satisfy the Action's risk level
    * Read-only Agent cannot propose Write/Execute Actions
    * Proposal authority must not exceed the Capability Snapshot

    R2 S14: authority floor and required tool are read from
    :data:`ACTION_GOVERNANCE_REGISTRY` — no local lookup table.

    Returns a list of findings — empty means authority is valid.
    """
    findings: list[ReviewFinding] = []

    if capability is None:
        findings.append(
            ReviewFinding(
                finding_code=CODE_AUTHORITY_EXCEEDS_SNAPSHOT,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Agent {proposal.created_by_agent!r} has no Capability "
                    f"Snapshot in the ReviewRequest"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={"agent_id": proposal.created_by_agent},
            )
        )
        return findings

    cap = capability.capability
    # R2 S14: read authority floor + required tool from governance spec.
    spec = get_action_governance_spec(proposal.action_type)
    floor = spec.minimum_authority if spec is not None else None
    tool_name = spec.required_tool if spec is not None else None

    if floor is not None and _authority_rank(cap.authority) < _authority_rank(floor):
        # Read-only agent proposing a Write/Execute action
        if cap.authority == AgentAuthority.READ and floor != AgentAuthority.READ:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_AUTHORITY_READ_ONLY_WRITE,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"READ-only agent {proposal.created_by_agent!r} "
                        f"cannot propose {proposal.action_type!r} which "
                        f"requires {floor.value!r} authority"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "agent_authority": cap.authority.value,
                        "required_authority": floor.value,
                        "action_type": proposal.action_type,
                    },
                )
            )
        elif (
            cap.authority == AgentAuthority.PROPOSE and floor == AgentAuthority.EXECUTE
        ):
            findings.append(
                ReviewFinding(
                    finding_code=CODE_AUTHORITY_PROPOSE_EXECUTE,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"PROPOSE agent {proposal.created_by_agent!r} "
                        f"cannot propose execute-level action "
                        f"{proposal.action_type!r}"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "agent_authority": cap.authority.value,
                        "required_authority": floor.value,
                        "action_type": proposal.action_type,
                    },
                )
            )
        else:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_AUTHORITY_INSUFFICIENT,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Agent {proposal.created_by_agent!r} authority "
                        f"{cap.authority.value!r} is insufficient for "
                        f"{proposal.action_type!r} (requires {floor.value!r})"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "agent_authority": cap.authority.value,
                        "required_authority": floor.value,
                        "action_type": proposal.action_type,
                    },
                )
            )

    # Verify the action's Tool is in the Agent's allowed_tools
    # R2 S14: tool name from governance spec.
    if tool_name is not None and tool_name not in cap.allowed_tools:
        findings.append(
            ReviewFinding(
                finding_code=CODE_ACTION_TOOL_FORBIDDEN,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Agent {proposal.created_by_agent!r} is not allowed "
                    f"to use tool {tool_name!r} required by action "
                    f"{proposal.action_type!r}"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={
                    "agent_id": proposal.created_by_agent,
                    "tool_name": tool_name,
                    "allowed_tools": sorted(cap.allowed_tools),
                    "action_type": proposal.action_type,
                },
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Tenant & sensitive-data validation — Phase 5A Section 7.5
# ---------------------------------------------------------------------------


def validate_tenant_safety(
    proposal: ActionProposal,
    *,
    tenant_id: str,
) -> list[ReviewFinding]:
    """Validate tenant isolation and sensitive-data rules.

    Phase 5A Section 7.5 — does NOT implement complex DLP; uses
    deterministic rules and a replaceable interface.
    """
    findings: list[ReviewFinding] = []

    if proposal.tenant_id != tenant_id:
        findings.append(
            ReviewFinding(
                finding_code=CODE_TENANT_CROSS_REFERENCE,
                severity=ReviewFindingSeverity.CRITICAL,
                message=(
                    f"Proposal {proposal.proposal_id!r} tenant "
                    f"{proposal.tenant_id!r} != expected {tenant_id!r}"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={
                    "proposal_tenant": proposal.tenant_id,
                    "expected_tenant": tenant_id,
                },
            )
        )

    # Sensitive-key scan on payload (delegates to the shared scanner
    # used by ActionProposal.payload validation).
    from multi_agent.contracts import _reject_sensitive_keys

    try:
        _reject_sensitive_keys(
            proposal.payload, f"Proposal {proposal.proposal_id}.payload"
        )
    except ValueError as e:
        findings.append(
            ReviewFinding(
                finding_code=CODE_TENANT_SECRET_FIELD,
                severity=ReviewFindingSeverity.CRITICAL,
                message=str(e),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={"proposal_id": proposal.proposal_id},
            )
        )

    # PII egress heuristic — flag if payload contains an email field
    # AND the action is a bulk notification.  This is intentionally
    # conservative; a real DLP service is a Phase 5B concern.
    if proposal.action_type == "notification.bulk_send":
        has_email = any("email" in str(k).lower() for k in proposal.payload.keys())
        if has_email:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_TENANT_PII_EGRESS,
                    severity=ReviewFindingSeverity.WARNING,
                    message=(
                        f"Bulk notification {proposal.proposal_id!r} "
                        f"contains an email field — PII egress"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "action_type": proposal.action_type,
                        "has_email_field": True,
                    },
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Idempotency validation — Phase 5A Section 7.6
# ---------------------------------------------------------------------------


def validate_idempotency(
    proposal: ActionProposal,
    *,
    risk_level: ReviewRiskLevel,
) -> list[ReviewFinding]:
    """Validate idempotency-key requirements.

    High-risk or executable Proposals must have a stable, non-blank
    idempotency_key consistent with the Action identity.
    """
    findings: list[ReviewFinding] = []

    key = proposal.idempotency_key
    if not key:
        findings.append(
            ReviewFinding(
                finding_code=CODE_IDEMPOTENCY_MISSING,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Proposal {proposal.proposal_id!r} is missing an idempotency_key"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={"risk_level": risk_level.value},
            )
        )
        return findings

    if not key.strip():
        findings.append(
            ReviewFinding(
                finding_code=CODE_IDEMPOTENCY_BLANK,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Proposal {proposal.proposal_id!r} has a blank idempotency_key"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={"risk_level": risk_level.value},
            )
        )
        return findings

    # High-risk + critical Proposals: idempotency_key must be at least
    # 8 chars to avoid accidental collision.
    if risk_level in (ReviewRiskLevel.HIGH, ReviewRiskLevel.CRITICAL):
        if len(key.strip()) < 8:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_IDEMPOTENCY_INCONSISTENT,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"High-risk Proposal {proposal.proposal_id!r} "
                        f"idempotency_key is too short ({len(key.strip())} "
                        f"chars; minimum 8)"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "risk_level": risk_level.value,
                        "key_length": len(key.strip()),
                        "minimum": 8,
                    },
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Identity validation — Phase 5A Section 7.1
# ---------------------------------------------------------------------------


def validate_identity(
    proposal: ActionProposal,
    request: ReviewRequest,
    *,
    task_records: dict[str, TaskRecordSummary],
    seen_proposal_ids: set[str],
) -> list[ReviewFinding]:
    """Validate Proposal identity fields against the ReviewRequest."""
    findings: list[ReviewFinding] = []

    if proposal.tenant_id != request.tenant_id:
        findings.append(
            ReviewFinding(
                finding_code=CODE_IDENTITY_MISMATCH,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Proposal {proposal.proposal_id!r} tenant "
                    f"{proposal.tenant_id!r} != request "
                    f"{request.tenant_id!r}"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={
                    "field": "tenant_id",
                    "proposal_value": proposal.tenant_id,
                    "request_value": request.tenant_id,
                },
            )
        )

    # task_id is not carried on ActionProposal directly; we verify
    # the agent_id is present in the task_records.  The Phase 4
    # adapter associates each Proposal with the Task that produced it
    # via the AgentResult.action_proposals list.
    if proposal.created_by_agent not in {tr.agent_id for tr in task_records.values()}:
        findings.append(
            ReviewFinding(
                finding_code=CODE_IDENTITY_MISMATCH,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Proposal {proposal.proposal_id!r} agent "
                    f"{proposal.created_by_agent!r} is not present in "
                    f"any Task Record"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={
                    "field": "agent_id",
                    "proposal_agent": proposal.created_by_agent,
                    "task_record_agents": sorted(
                        {tr.agent_id for tr in task_records.values()}
                    ),
                },
            )
        )

    if proposal.proposal_id in seen_proposal_ids:
        findings.append(
            ReviewFinding(
                finding_code=CODE_IDENTITY_DUPLICATE_PROPOSAL_ID,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Duplicate proposal_id {proposal.proposal_id!r} "
                    f"within the same ReviewRequest"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={"proposal_id": proposal.proposal_id},
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Action / Tool Allowlist validation — Phase 5A Section 7.4
# ---------------------------------------------------------------------------


def validate_action_allowlist(
    proposal: ActionProposal,
) -> list[ReviewFinding]:
    """Validate that the action_type is registered.

    R2 S14: checks :data:`ACTION_GOVERNANCE_REGISTRY` instead of a
    local lookup table.  Phase 5A only validates shape — it never
    invokes the Tool.  The actual category allowlist is enforced by
    the Policy evaluator.
    """
    findings: list[ReviewFinding] = []

    spec = get_action_governance_spec(proposal.action_type)
    if spec is None:
        findings.append(
            ReviewFinding(
                finding_code=CODE_ACTION_UNKNOWN_TYPE,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Action type {proposal.action_type!r} is not "
                    f"registered in the Action Governance Spec"
                ),
                proposal_id=proposal.proposal_id,
                agent_id=proposal.created_by_agent,
                policy_source="reviewer@ma-05a",
                details={"action_type": proposal.action_type},
            )
        )

    return findings


# ---------------------------------------------------------------------------
# ProposalReviewer
# ---------------------------------------------------------------------------


class ProposalReviewer:
    """Single entry point for Phase 5A Proposal review.

    The Reviewer is a pure function of (request, policy_evaluator).
    It reads ONLY from the frozen :class:`ReviewRequest` and the
    injected :class:`PolicyEvaluator` — never from a live registry,
    wall-clock, or random state.

    Phase 5A Section 3: ``approved`` means "Proposal has passed
    review".  It NEVER means "Proposal has been executed".
    """

    def __init__(self) -> None:
        # No state — every review is a pure function of the inputs.
        pass

    async def review(
        self,
        request: ReviewRequest,
        *,
        policy_evaluator: PolicyEvaluator,
    ) -> ReviewBatchResult:
        """Review every Proposal in *request* and return a
        :class:`ReviewBatchResult`.

        Execution order per Phase 5A Section 12.

        R2: iterates over :class:`ReviewProposalEnvelope` (NOT raw
        Proposals) so per-task capability binding is available for
        every Proposal.  Builds a :class:`PolicyDecisionAudit` for
        every Proposal (including skipped-authority-failure).  Calls
        :meth:`verify_against_request` before returning.
        """
        # 1. Verify ReviewRequest integrity
        try:
            request.verify_integrity()
        except ReviewIntegrityError:
            raise
        except Exception as e:
            raise InvalidReviewRequestError(
                f"ReviewRequest {request.review_id!r} failed integrity "
                f"verification: {e}"
            ) from e

        # R2 S3 / R2.1 P0-7: verify governance spec version/hash match
        # the live registry so a Request built against an older spec
        # (or a tampered registry) is rejected.
        if request.governance_spec_version != ACTION_GOVERNANCE_SPEC_VERSION:
            raise InvalidReviewRequestError(
                f"ReviewRequest {request.review_id!r}: governance_spec_version "
                f"{request.governance_spec_version!r} != live "
                f"{ACTION_GOVERNANCE_SPEC_VERSION!r}"
            )
        # R2.1 P0-7: verify the live registry hash matches the module
        # constant — detects a tampered ACTION_GOVERNANCE_REGISTRY even
        # if the Request carries the old constant hash.
        try:
            verify_governance_spec_integrity(expected_hash=request.governance_spec_hash)
        except RuntimeError as e:
            raise InvalidReviewRequestError(
                f"ReviewRequest {request.review_id!r}: governance spec "
                f"integrity check failed: {e}"
            ) from e
        # R2.1 P0-7: unconditional comparison (both sides are required
        # non-blank — the old ``if request.governance_spec_hash`` guard
        # is removed).
        if request.governance_spec_hash != ACTION_GOVERNANCE_SPEC_HASH:
            raise InvalidReviewRequestError(
                f"ReviewRequest {request.review_id!r}: governance_spec_hash "
                f"{request.governance_spec_hash[:12]!r} != live "
                f"{ACTION_GOVERNANCE_SPEC_HASH[:12]!r}"
            )

        # 2. Build lookup indices
        evidence_index, excluded_evidence_ids = build_evidence_index(request.evidence)
        # R2 P0-2: capability_bindings are unique by task_id (NOT
        # agent_id).  The Reviewer looks up capability by
        # envelope.task_id so a Proposal cannot borrow another Task's
        # binding for the same Agent.
        cap_by_task: dict[str, ExecutionCapabilitySnapshot] = {
            cb.task_id: cb for cb in request.capability_bindings
        }
        # Agent-based lookup retained for evidence source-agent
        # consistency validation (backward compat).
        capability_snapshots = {
            cb.agent_id: CapabilitySnapshot(
                agent_id=cb.agent_id, capability=cb.capability
            )
            for cb in request.capability_bindings
        }
        task_records = {tr.task_id: tr for tr in request.task_records}
        # R2 P0-1: envelope lookup by proposal_id for per-task
        # capability binding.
        envelope_by_pid = {
            e.proposal.proposal_id: e for e in request.proposal_envelopes
        }

        # 3. Detect duplicate Evidence (cross-Proposal)
        evidence_dup_findings = detect_duplicate_evidence(request.evidence)

        # R2.1 P0-1: convert frozen ReviewProposalSnapshots to fresh
        # ActionProposal copies ONCE for all Phase 2 helper functions
        # (detect_duplicates / detect_conflicts / detect_dangling_evidence
        # / _review_single_proposal).  These helpers were written against
        # the mutable ActionProposal contract (dict ``payload``) and
        # cannot operate on the snapshot's frozen tuple payload.  The
        # snapshots themselves remain the audited objects; these copies
        # are internal scratch space.
        proposals_for_helpers: list[ActionProposal] = [
            s.to_action_proposal() for s in request.proposals
        ]

        # 4. Detect duplicate Proposals
        dedup_result = detect_duplicates(proposals_for_helpers)

        # 5. Detect conflicts (excluding deduped Proposals)
        conflict_result = detect_conflicts(
            proposals_for_helpers,
            excluded_proposal_ids=dedup_result.excluded_proposal_ids,
        )

        # 6. Dangling evidence (informational)
        dangling_findings = detect_dangling_evidence(
            proposals_for_helpers, evidence_index
        )

        # 7. Per-Proposal review — iterate over envelopes (R2 P0-1)
        per_proposal_reviews: list[ProposalReview] = []
        all_findings: list[ReviewFinding] = []
        all_findings.extend(evidence_dup_findings)
        all_findings.extend(dedup_result.findings)
        all_findings.extend(conflict_result.findings)
        all_findings.extend(dangling_findings)

        for snapshot in sorted(request.proposals, key=lambda p: p.proposal_id):
            # R2.1 P0-1: convert the frozen ReviewProposalSnapshot to a
            # fresh ActionProposal copy for internal use by the Phase 2
            # helper functions.  The snapshot itself is frozen and its
            # ``snapshot_hash`` was verified at the Request boundary —
            # this copy is NOT the audited object, so mutations to it
            # do not affect the Request hash or the audit trail.
            proposal = snapshot.to_action_proposal()
            envelope = envelope_by_pid.get(snapshot.proposal_id)
            review = await self._review_single_proposal(
                proposal=proposal,
                envelope=envelope,
                request=request,
                evidence_index=evidence_index,
                excluded_evidence_ids=excluded_evidence_ids,
                capability_snapshots=capability_snapshots,
                cap_by_task=cap_by_task,
                task_records=task_records,
                policy_evaluator=policy_evaluator,
                dedup_result=dedup_result,
                conflict_result=conflict_result,
            )
            per_proposal_reviews.append(review)

        # 8. Compute batch status
        batch_status = self._compute_batch_status(per_proposal_reviews)

        # 9. Build sorted result lists (R2 S1: tuples)
        approved_ids = tuple(
            sorted(
                r.proposal_id
                for r in per_proposal_reviews
                if r.status == ReviewDecisionStatus.APPROVED
            )
        )
        rejected_ids = tuple(
            sorted(
                r.proposal_id
                for r in per_proposal_reviews
                if r.status == ReviewDecisionStatus.REJECTED
            )
        )
        approval_required_ids = tuple(
            sorted(
                r.proposal_id
                for r in per_proposal_reviews
                if r.status == ReviewDecisionStatus.NEEDS_APPROVAL
            )
        )
        conflicted_ids = tuple(
            sorted(
                r.proposal_id
                for r in per_proposal_reviews
                if r.status == ReviewDecisionStatus.CONFLICT
            )
        )
        deduplicated_ids = tuple(
            sorted(
                r.proposal_id
                for r in per_proposal_reviews
                if r.status == ReviewDecisionStatus.DEDUPLICATED
            )
        )

        # 10. Build the final ReviewBatchResult (R2: governance_spec_hash +
        #     reviewer_version)
        result = ReviewBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            proposal_reviews=tuple(
                sorted(per_proposal_reviews, key=lambda r: r.proposal_id)
            ),
            batch_status=batch_status,
            approved_proposal_ids=approved_ids,
            rejected_proposal_ids=rejected_ids,
            approval_required_proposal_ids=approval_required_ids,
            conflicted_proposal_ids=conflicted_ids,
            deduplicated_proposal_ids=deduplicated_ids,
            findings=tuple(
                sorted(
                    all_findings,
                    key=lambda f: (f.proposal_id, f.finding_code, f.message),
                )
            ),
            governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
            reviewer_version=REVIEWER_VERSION,
        )

        # 11. Validate final result
        try:
            result.verify_integrity()
        except ReviewIntegrityError:
            raise
        except Exception as e:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {result.review_id!r} failed integrity "
                f"verification: {e}"
            ) from e

        # 12. Semantic validation — status/finding/flag consistency
        try:
            result.verify_semantics(reviewer_version=request.reviewer_version)
        except InvalidReviewResultError:
            raise
        except Exception as e:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {result.review_id!r} failed semantic "
                f"verification: {e}"
            ) from e

        # 13. R2 S8: bind Result back to Request
        try:
            result.verify_against_request(request)
        except InvalidReviewResultError:
            raise
        except Exception as e:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {result.review_id!r} failed "
                f"verify_against_request: {e}"
            ) from e

        return result

    # -----------------------------------------------------------------
    # Per-proposal review
    # -----------------------------------------------------------------

    async def _review_single_proposal(
        self,
        *,
        proposal: ActionProposal,
        envelope: ReviewProposalEnvelope | None,
        request: ReviewRequest,
        evidence_index: dict[str, Evidence],
        excluded_evidence_ids: set[str],
        capability_snapshots: dict[str, CapabilitySnapshot],
        cap_by_task: dict[str, ExecutionCapabilitySnapshot],
        task_records: dict[str, TaskRecordSummary],
        policy_evaluator: PolicyEvaluator,
        dedup_result: Any,
        conflict_result: Any,
    ) -> ProposalReview:
        findings: list[ReviewFinding] = []
        matched_evidence_ids: list[str] = []

        # 1. Identity validation
        findings.extend(
            validate_identity(
                proposal,
                request,
                task_records=task_records,
                seen_proposal_ids=set(),  # duplicates already flagged
            )
        )

        # 2. Tenant & sensitive-data validation
        findings.extend(validate_tenant_safety(proposal, tenant_id=request.tenant_id))

        # 3. Action allowlist validation
        findings.extend(validate_action_allowlist(proposal))

        # 4. Evidence validation (with tamper-detection excluded set)
        # R2: pass canonical_risk_level so a misbehaving Agent cannot
        # lower the evidence bar by declaring risk_level=low.
        risk_level = classify_risk(proposal)
        ev_findings = validate_evidence_for_proposal(
            proposal,
            evidence_index,
            capability_snapshots,
            tenant_id=request.tenant_id,
            excluded_evidence_ids=excluded_evidence_ids,
            canonical_risk_level=risk_level,
        )
        findings.extend(ev_findings)
        # Matched evidence = referenced evidence that passed validation
        # (no finding against it)
        flagged_ev_ids = {
            eid
            for f in ev_findings
            for eid in f.evidence_ids
            if f.severity
            in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
        }
        matched_evidence_ids = sorted(set(proposal.evidence_ids) - flagged_ev_ids)

        # 5. Authority validation — R2 P0-2 / R2.1 P0-3: per-task
        # capability lookup via envelope.task_id (NOT
        # proposal.created_by_agent).  The agent_id Legacy Fallback is
        # REMOVED — if the exact-task capability binding is missing,
        # ``cap_snapshot`` stays ``None`` and :func:`validate_authority`
        # fails-closed with CODE_AUTHORITY_INSUFFICIENT.
        cap_snapshot: CapabilitySnapshot | None = None
        if envelope is not None:
            ec = cap_by_task.get(envelope.task_id)
            if ec is not None:
                cap_snapshot = CapabilitySnapshot(
                    agent_id=ec.agent_id, capability=ec.capability
                )
        # R2.1 P0-3: NO agent_id fallback.  A missing exact-task
        # binding means the Proposal's authority cannot be verified
        # and must be rejected.  Previously the code fell back to
        # ``capability_snapshots.get(proposal.created_by_agent)``
        # which allowed cross-Task Capability Borrowing.
        findings.extend(validate_authority(proposal, cap_snapshot))

        # 6. Idempotency validation
        findings.extend(validate_idempotency(proposal, risk_level=risk_level))

        # 7. Risk classification findings
        if risk_level == ReviewRiskLevel.HIGH:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_RISK_HIGH_NEEDS_APPROVAL,
                    severity=ReviewFindingSeverity.WARNING,
                    message=(
                        f"High-risk action {proposal.action_type!r} "
                        f"requires human approval"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "action_type": proposal.action_type,
                        "risk_level": risk_level.value,
                    },
                )
            )
        elif risk_level == ReviewRiskLevel.CRITICAL:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_RISK_CRITICAL_NEEDS_APPROVAL,
                    severity=ReviewFindingSeverity.WARNING,
                    message=(
                        f"Critical-risk action {proposal.action_type!r} "
                        f"requires human approval"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source="reviewer@ma-05a",
                    details={
                        "action_type": proposal.action_type,
                        "risk_level": risk_level.value,
                    },
                )
            )

        # 8. Policy evaluation
        authority_valid = not any(
            f.finding_code.startswith("review.authority.") for f in findings
        )
        if not authority_valid:
            # R2 S5: skip policy evaluation if authority already failed,
            # but still build a PolicyDecisionAudit with
            # evaluator_source_id="skipped-authority-failure".
            policy_result = PolicyEvaluationResult(
                proposal_id=proposal.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=(),
                policy_version=request.policy_context.policy_version,
                findings=(),
            )
            # R2.1 P0-7: policy_request_hash for the skipped case uses
            # the ReviewRequest hash (no actual PolicyEvaluationRequest
            # was built).  This is still a non-blank hash that binds
            # the audit to the Request.
            policy_audit = self._build_policy_audit(
                policy_result,
                evaluator_source_id="skipped-authority-failure",
                evaluator_version="reviewer-skipped",
                proposal_id=proposal.proposal_id,
                request_hash=request.request_hash,
                policy_request_hash=request.request_hash,
            )
        else:
            policy_req = PolicyEvaluationRequest(
                review_id=request.review_id,
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                proposal_id=proposal.proposal_id,
                action_type=proposal.action_type,
                target_entity=proposal.target_entity,
                target_id=proposal.target_id,
                payload=proposal.payload,
                risk_level=risk_level.value,
                agent_authority=(
                    cap_snapshot.capability.authority.value if cap_snapshot else "read"
                ),
                policy_context=request.policy_context,
            )
            # R2.1 P0-7: compute the policy_request_hash from the
            # canonical PolicyEvaluationRequest content.
            policy_request_hash = stable_hash(policy_req.model_dump(mode="python"))
            try:
                policy_result = await policy_evaluator.evaluate(policy_req)
            except ReviewError:
                raise
            except Exception as e:
                raise PolicyEvaluationError(
                    f"Policy evaluator raised for proposal "
                    f"{proposal.proposal_id!r}: {e}"
                ) from e
            # R1 Section VI / R2.1 P0-6: validate policy result identity
            # before consuming it — fail-closed on any mismatch.
            self._validate_policy_result_identity(policy_result, proposal, request)
            policy_audit = self._build_policy_audit(
                policy_result,
                evaluator_source_id=policy_evaluator.__class__.__name__,
                evaluator_version=request.policy_context.policy_version,
                proposal_id=proposal.proposal_id,
                request_hash=request.request_hash,
                policy_request_hash=policy_request_hash,
            )
        findings.extend(policy_result.findings)

        # R1: synthesize policy-decision findings if the policy
        # evaluator did not emit one.  verify_semantics() requires
        # REJECTED ↔ rejection-class finding (CODE_POLICY_DENIED) and
        # NEEDS_INPUT ↔ CODE_EVIDENCE_MISSING/CODE_POLICY_NEEDS_INPUT.
        if policy_result.decision == PolicyDecision.DENIED and not any(
            f.finding_code == CODE_POLICY_DENIED for f in findings
        ):
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=f"Policy denied proposal {proposal.proposal_id!r}",
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source=f"policy@{policy_result.policy_version}",
                    details={
                        "policy_version": policy_result.policy_version,
                        "matched_rule_count": len(policy_result.matched_rules),
                    },
                )
            )
        if policy_result.decision == PolicyDecision.NEEDS_INPUT and not any(
            f.finding_code in (CODE_POLICY_NEEDS_INPUT, CODE_EVIDENCE_MISSING)
            for f in findings
        ):
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_NEEDS_INPUT,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Policy requested additional input for proposal "
                        f"{proposal.proposal_id!r}"
                    ),
                    proposal_id=proposal.proposal_id,
                    agent_id=proposal.created_by_agent,
                    policy_source=f"policy@{policy_result.policy_version}",
                    details={
                        "policy_version": policy_result.policy_version,
                        "matched_rule_count": len(policy_result.matched_rules),
                    },
                )
            )

        policy_valid = policy_result.decision != PolicyDecision.DENIED

        # 9. Idempotency validity
        idempotency_valid = not any(
            f.finding_code.startswith("review.idempotency.") for f in findings
        )

        # 10. Conflict / duplicate status
        is_duplicate = proposal.proposal_id in dedup_result.excluded_proposal_ids
        is_conflict = proposal.proposal_id in conflict_result.conflicted_proposal_ids

        # Add dedup finding to this proposal's own findings so that
        # verify_semantics() can confirm DEDUPLICATED ↔ CODE_DUPLICATE_DEDUPED.
        if is_duplicate:
            findings.extend(
                f
                for f in dedup_result.findings
                if f.proposal_id == proposal.proposal_id
            )

        # R1: thread conflict findings to this proposal's own findings
        # so that verify_semantics() can confirm CONFLICT ↔ review.conflict.*.
        if is_conflict:
            findings.extend(
                f
                for f in conflict_result.findings
                if f.proposal_id == proposal.proposal_id
            )

        # 11. Decision
        status = self._compute_decision(
            findings=findings,
            risk_level=risk_level,
            policy_decision=policy_result.decision,
            is_duplicate=is_duplicate,
            is_conflict=is_conflict,
        )

        # 12. required_approval flag
        required_approval = status in (
            ReviewDecisionStatus.NEEDS_APPROVAL,
        ) or risk_level in (ReviewRiskLevel.HIGH, ReviewRiskLevel.CRITICAL)

        # R2 P0-8: DEDUPLICATED proposals carry the primary's id
        primary_proposal_id: str | None = None
        if is_duplicate:
            for dg in dedup_result.duplicate_groups:
                if proposal.proposal_id in dg.duplicate_proposal_ids:
                    primary_proposal_id = dg.primary_proposal_id
                    break

        return ProposalReview(
            proposal_id=proposal.proposal_id,
            status=status,
            findings=tuple(sorted(findings, key=lambda f: (f.finding_code, f.message))),
            matched_evidence_ids=tuple(matched_evidence_ids),
            required_approval=required_approval,
            risk_level=risk_level,
            authority_valid=authority_valid,
            policy_valid=policy_valid,
            idempotency_valid=idempotency_valid,
            policy_audit=policy_audit,
            primary_proposal_id=primary_proposal_id,
        )

    # -----------------------------------------------------------------
    # Policy result identity validation — R1 Section VI
    # -----------------------------------------------------------------

    @staticmethod
    def _validate_policy_result_identity(
        policy_result: PolicyEvaluationResult,
        proposal: ActionProposal,
        request: ReviewRequest,
    ) -> None:
        """R2.1 P0-6: fail-closed validation that the policy result
        belongs to the proposal it claims to be for AND that every
        matched rule exists in the Request's frozen Policy Snapshot.

        Checks (in order):

        1. ``policy_result.verify_semantics()`` — decision ↔ finding
           consistency (R2.1 P0-6: previously never called).
        2. ``proposal_id`` matches.
        3. ``policy_version`` matches the Request's PolicyContext.
        4. Every finding belongs to this proposal.
        5. Every matched rule's ``rule_id`` / ``rule_version`` /
           ``effect`` exists in ``request.policy_context.rules`` —
           prevents an external Policy Adapter from returning rules
           that are not part of the current Policy Snapshot.

        Raises :class:`PolicyEvaluationError` on any mismatch.
        """
        # R2.1 P0-6: verify_semantics() MUST be called before the
        # result is consumed.  Previously it was never invoked, so a
        # Policy Adapter could return a semantically inconsistent
        # result (e.g. decision=DENIED but no CODE_POLICY_DENIED
        # finding) and the Reviewer would still process it.
        policy_result.verify_semantics()

        if policy_result.proposal_id != proposal.proposal_id:
            raise PolicyEvaluationError(
                f"Policy result proposal_id {policy_result.proposal_id!r} != "
                f"expected {proposal.proposal_id!r}"
            )
        if policy_result.policy_version != request.policy_context.policy_version:
            raise PolicyEvaluationError(
                f"Policy result policy_version {policy_result.policy_version!r} "
                f"!= expected {request.policy_context.policy_version!r}"
            )
        for finding in policy_result.findings:
            if finding.proposal_id != proposal.proposal_id:
                raise PolicyEvaluationError(
                    f"Policy finding proposal_id {finding.proposal_id!r} != "
                    f"expected {proposal.proposal_id!r}"
                )

        # R2.1 P0-6: every matched rule must exist in the Request's
        # frozen Policy Snapshot.  Build a lookup from
        # (rule_id, rule_version, effect) → exists.  Synthetic rules
        # emitted by the DeterministicPolicyEvaluator
        # (``governance-spec-allowlist``, ``authority-floor``,
        # ``always-needs-approval``, ``high-risk-needs-approval``) are
        # also accepted — they use ``policy_version`` as their
        # ``rule_version`` and are generated by the evaluator itself,
        # not by an external adapter.
        request_rules: set[tuple[str, str, str]] = {
            (r.rule_id, r.rule_version, r.effect.value)
            for r in request.policy_context.rules
        }
        synthetic_rule_ids: frozenset[str] = frozenset(
            {
                "governance-spec-allowlist",
                "authority-floor",
                "always-needs-approval",
                "high-risk-needs-approval",
            }
        )
        for rule in policy_result.matched_rules:
            if not rule.rule_version.strip():
                raise PolicyEvaluationError(
                    f"Matched rule {rule.rule_id!r} has blank rule_version"
                )
            key = (rule.rule_id, rule.rule_version, rule.effect.value)
            if key not in request_rules:
                # Allow synthetic rules generated by the
                # DeterministicPolicyEvaluator (they are not in
                # PolicyContext.rules but are legitimate).
                if (
                    rule.rule_id in synthetic_rule_ids
                    and rule.rule_version == request.policy_context.policy_version
                ):
                    continue
                raise PolicyEvaluationError(
                    f"Matched rule {rule.rule_id!r} (version="
                    f"{rule.rule_version!r}, effect={rule.effect.value!r}) "
                    f"does not exist in the Request's Policy Snapshot"
                )

    # -----------------------------------------------------------------
    # R2 S5: Policy decision audit builder
    # -----------------------------------------------------------------

    @staticmethod
    def _build_policy_audit(
        policy_result: PolicyEvaluationResult,
        *,
        evaluator_source_id: str,
        evaluator_version: str,
        proposal_id: str,
        request_hash: str,
        policy_request_hash: str,
    ) -> PolicyDecisionAudit:
        """Build a :class:`PolicyDecisionAudit` from a
        :class:`PolicyEvaluationResult`.

        R2 S5 / R2.1 P0-7: every Proposal — including those that
        skipped external Policy because Authority failed — MUST carry
        one audit.  The ``proposal_id`` / ``request_hash`` /
        ``policy_request_hash`` bind the audit to the exact Proposal
        and Request it answers, so a tampered or replayed audit cannot
        be attached to a different Proposal / Request.

        The ``evaluation_hash`` is computed over the same canonical
        fields that
        :class:`PolicyDecisionAudit._verify_evaluation_hash` checks,
        so a tampered audit is detectable at construction.
        """
        matched_rules = tuple(policy_result.matched_rules)
        evaluation_hash = stable_hash(
            {
                "evaluator_source_id": evaluator_source_id,
                "evaluator_version": evaluator_version,
                "policy_version": policy_result.policy_version,
                "decision": policy_result.decision.value,
                "matched_rules": [r.model_dump(mode="python") for r in matched_rules],
                "proposal_id": proposal_id,
                "request_hash": request_hash,
                "policy_request_hash": policy_request_hash,
            }
        )
        return PolicyDecisionAudit(
            evaluator_source_id=evaluator_source_id,
            evaluator_version=evaluator_version,
            policy_version=policy_result.policy_version,
            decision=policy_result.decision,
            matched_rules=matched_rules,
            proposal_id=proposal_id,
            request_hash=request_hash,
            policy_request_hash=policy_request_hash,
            evaluation_hash=evaluation_hash,
        )

    # -----------------------------------------------------------------
    # Decision logic — R1 Section VII
    # -----------------------------------------------------------------

    def _compute_decision(
        self,
        *,
        findings: list[ReviewFinding],
        risk_level: ReviewRiskLevel,
        policy_decision: PolicyDecision,
        is_duplicate: bool,
        is_conflict: bool,
    ) -> ReviewDecisionStatus:
        """Classify the per-proposal decision by finding CODE priority.

        R2.1 P1-2 order (safety errors cannot be masked by
        Deduplication):

            conflict > rejected > needs_input > needs_approval >
            deduplicated > approved

        Only a Proposal that has NO identity, tenant, evidence,
        authority, policy, or idempotency errors is eligible for
        DEDUPLICATED status.  Previously ``deduplicated`` took
        priority over ``rejected``, which let a Proposal with an
        authority violation be silently marked as DEDUPLICATED
        instead of REJECTED — hiding the security violation from
        the audit trail.
        """
        # 1. Conflict takes priority over everything
        if is_conflict:
            return ReviewDecisionStatus.CONFLICT

        finding_codes = {f.finding_code for f in findings}

        # 2. Rejection-class findings (ERROR/CRITICAL severity) → REJECTED
        #    R2.1 P1-2: REJECTED now takes priority over DEDUPLICATED.
        has_rejection = any(
            f.finding_code in REJECTION_FINDING_CODES
            and f.severity
            in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in findings
        )
        if has_rejection:
            return ReviewDecisionStatus.REJECTED

        # 3. Missing evidence or policy needs input → NEEDS_INPUT
        if finding_codes & {CODE_EVIDENCE_MISSING, CODE_POLICY_NEEDS_INPUT}:
            return ReviewDecisionStatus.NEEDS_INPUT

        # 4. Policy needs input (fallback if finding missing)
        if policy_decision == PolicyDecision.NEEDS_INPUT:
            return ReviewDecisionStatus.NEEDS_INPUT

        # 5. High/critical risk → NEEDS_APPROVAL
        if risk_level in (ReviewRiskLevel.HIGH, ReviewRiskLevel.CRITICAL):
            return ReviewDecisionStatus.NEEDS_APPROVAL

        # 6. Policy needs approval
        if policy_decision == PolicyDecision.NEEDS_APPROVAL:
            return ReviewDecisionStatus.NEEDS_APPROVAL

        # 7. R2.1 P1-2: DEDUPLICATED is now AFTER all safety checks.
        #    Only a Proposal that passed identity, tenant, evidence,
        #    authority, policy, and idempotency validation is eligible.
        if is_duplicate:
            return ReviewDecisionStatus.DEDUPLICATED

        return ReviewDecisionStatus.APPROVED

    def _compute_batch_status(
        self,
        reviews: list[ProposalReview],
    ) -> ReviewBatchStatus:
        # R2 S7: empty batch → NO_ACTIONS (NOT APPROVED) so Phase 5B
        # cannot mis-treat an empty Review as authorisation.
        if not reviews:
            return ReviewBatchStatus.NO_ACTIONS
        # Highest-priority status wins
        statuses = [proposal_status_to_batch(r.status) for r in reviews]
        return max(statuses, key=batch_status_priority)


__all__ = [
    "ProposalReviewer",
    "classify_risk",
    "validate_action_allowlist",
    "validate_authority",
    "validate_idempotency",
    "validate_identity",
    "validate_tenant_safety",
]
