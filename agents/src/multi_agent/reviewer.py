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

from multi_agent.contracts import (
    ActionProposal,
    AgentAuthority,
    Evidence,
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
    CapabilitySnapshot,
    ProposalReview,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
    ReviewFinding,
    ReviewFindingSeverity,
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
from multi_agent.policy import (
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyEvaluationResult,
    PolicyEvaluator,
)
from multi_agent.evidence_review import (
    build_evidence_index,
    detect_dangling_evidence,
    detect_duplicate_evidence,
    validate_evidence_for_proposal,
)
from multi_agent.conflict_resolution import (
    detect_conflicts,
    detect_duplicates,
)


# ---------------------------------------------------------------------------
# Risk classification — Phase 5A Section 9
# ---------------------------------------------------------------------------

# Action categories → Reviewer-side risk level.  This is INDEPENDENT
# of the Proposal's self-declared ``risk_level`` — a misbehaving Agent
# cannot lower the approval bar by declaring ``risk_level=low``.
_ACTION_RISK: dict[str, ReviewRiskLevel] = {
    "report.generate": ReviewRiskLevel.LOW,
    "summary.compile": ReviewRiskLevel.LOW,
    "metric.query": ReviewRiskLevel.LOW,
    "crm.tag.update": ReviewRiskLevel.MEDIUM,
    "crm.status.update": ReviewRiskLevel.MEDIUM,
    "crm.note.add": ReviewRiskLevel.MEDIUM,
    "crm.owner.assign": ReviewRiskLevel.HIGH,
    "crm.escalate": ReviewRiskLevel.HIGH,
    "refund.issue": ReviewRiskLevel.CRITICAL,
    "contract.amend": ReviewRiskLevel.CRITICAL,
    "notification.bulk_send": ReviewRiskLevel.HIGH,
    "permission.change": ReviewRiskLevel.CRITICAL,
}


def classify_risk(proposal: ActionProposal) -> ReviewRiskLevel:
    """Return the canonical Reviewer-side risk level for *proposal*.

    Falls back to :class:`ReviewRiskLevel.MEDIUM` for unknown actions
    (the Policy evaluator will reject them separately).
    """
    return _ACTION_RISK.get(proposal.action_type, ReviewRiskLevel.MEDIUM)


# ---------------------------------------------------------------------------
# Authority validation — Phase 5A Section 7.3
# ---------------------------------------------------------------------------


def _authority_rank(a: AgentAuthority) -> int:
    return {
        AgentAuthority.READ: 0,
        AgentAuthority.PROPOSE: 1,
        AgentAuthority.EXECUTE: 2,
    }[a]


# Map action_type → minimum AgentAuthority required to propose it.
# These are stricter than the Tool authority floor in
# :mod:`multi_agent.policy` because the Reviewer enforces the
# Capability Snapshot authority, not just the Tool authority.
_ACTION_AUTHORITY_FLOOR: dict[str, AgentAuthority] = {
    "report.generate": AgentAuthority.READ,
    "summary.compile": AgentAuthority.READ,
    "metric.query": AgentAuthority.READ,
    "crm.tag.update": AgentAuthority.PROPOSE,
    "crm.status.update": AgentAuthority.PROPOSE,
    "crm.note.add": AgentAuthority.PROPOSE,
    "crm.owner.assign": AgentAuthority.PROPOSE,
    "crm.escalate": AgentAuthority.PROPOSE,
    "refund.issue": AgentAuthority.PROPOSE,
    "contract.amend": AgentAuthority.PROPOSE,
    "notification.bulk_send": AgentAuthority.PROPOSE,
    "permission.change": AgentAuthority.PROPOSE,
}


# Tool name → action_type mapping for Tool/Action Allowlist validation.
# Phase 5A only validates that the action_type is registered; it does
# not invoke any Tool.  The mapping documents which Tool each
# action_type corresponds to so the Reviewer can verify the Agent's
# ``allowed_tools`` includes it.
_ACTION_TO_TOOL: dict[str, str] = {
    "report.generate": "crm_reader.get_customers",
    "summary.compile": "crm_reader.get_customers",
    "metric.query": "crm_reader.get_customers",
    "crm.tag.update": "crm_writer.propose",
    "crm.status.update": "crm_writer.propose",
    "crm.note.add": "crm_writer.propose",
    "crm.owner.assign": "crm_writer.propose",
    "crm.escalate": "crm_writer.propose",
    "refund.issue": "crm_writer.propose",
    "contract.amend": "crm_writer.propose",
    "notification.bulk_send": "crm_writer.propose",
    "permission.change": "governance.approve",
}


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
    floor = _ACTION_AUTHORITY_FLOOR.get(proposal.action_type)

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
    tool_name = _ACTION_TO_TOOL.get(proposal.action_type)
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

    Phase 5A only validates shape — it never invokes the Tool.
    The actual category allowlist is enforced by the Policy evaluator.
    """
    findings: list[ReviewFinding] = []

    if proposal.action_type not in _ACTION_TO_TOOL:
        findings.append(
            ReviewFinding(
                finding_code=CODE_ACTION_UNKNOWN_TYPE,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"Action type {proposal.action_type!r} is not "
                    f"registered in the Reviewer allowlist"
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

        # 2. Build lookup indices
        evidence_index, excluded_evidence_ids = build_evidence_index(request.evidence)
        capability_snapshots = {
            cb.agent_id: CapabilitySnapshot(
                agent_id=cb.agent_id, capability=cb.capability
            )
            for cb in request.capability_bindings
        }
        task_records = {tr.task_id: tr for tr in request.task_records}

        # 3. Detect duplicate Evidence (cross-Proposal)
        evidence_dup_findings = detect_duplicate_evidence(request.evidence)

        # 4. Detect duplicate Proposals
        dedup_result = detect_duplicates(request.proposals)

        # 5. Detect conflicts (excluding deduped Proposals)
        conflict_result = detect_conflicts(
            request.proposals,
            excluded_proposal_ids=dedup_result.excluded_proposal_ids,
        )

        # 6. Dangling evidence (informational)
        dangling_findings = detect_dangling_evidence(request.proposals, evidence_index)

        # 7. Per-Proposal review
        seen_proposal_ids: set[str] = set()
        per_proposal_reviews: list[ProposalReview] = []
        all_findings: list[ReviewFinding] = []
        all_findings.extend(evidence_dup_findings)
        all_findings.extend(dedup_result.findings)
        all_findings.extend(conflict_result.findings)
        all_findings.extend(dangling_findings)

        for proposal in sorted(request.proposals, key=lambda p: p.proposal_id):
            seen_proposal_ids.add(proposal.proposal_id)
            review = await self._review_single_proposal(
                proposal=proposal,
                request=request,
                evidence_index=evidence_index,
                excluded_evidence_ids=excluded_evidence_ids,
                capability_snapshots=capability_snapshots,
                task_records=task_records,
                policy_evaluator=policy_evaluator,
                dedup_result=dedup_result,
                conflict_result=conflict_result,
                all_findings=all_findings,
            )
            per_proposal_reviews.append(review)

        # 8. Compute batch status
        batch_status = self._compute_batch_status(per_proposal_reviews)

        # 9. Build sorted result lists
        approved_ids = sorted(
            r.proposal_id
            for r in per_proposal_reviews
            if r.status == ReviewDecisionStatus.APPROVED
        )
        rejected_ids = sorted(
            r.proposal_id
            for r in per_proposal_reviews
            if r.status == ReviewDecisionStatus.REJECTED
        )
        approval_required_ids = sorted(
            r.proposal_id
            for r in per_proposal_reviews
            if r.status == ReviewDecisionStatus.NEEDS_APPROVAL
        )
        conflicted_ids = sorted(
            r.proposal_id
            for r in per_proposal_reviews
            if r.status == ReviewDecisionStatus.CONFLICT
        )
        deduplicated_ids = sorted(
            r.proposal_id
            for r in per_proposal_reviews
            if r.status == ReviewDecisionStatus.DEDUPLICATED
        )

        # 10. Build the final ReviewBatchResult
        result = ReviewBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            proposal_reviews=sorted(per_proposal_reviews, key=lambda r: r.proposal_id),
            batch_status=batch_status,
            approved_proposal_ids=approved_ids,
            rejected_proposal_ids=rejected_ids,
            approval_required_proposal_ids=approval_required_ids,
            conflicted_proposal_ids=conflicted_ids,
            deduplicated_proposal_ids=deduplicated_ids,
            findings=sorted(
                all_findings,
                key=lambda f: (f.proposal_id, f.finding_code, f.message),
            ),
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
            result.verify_semantics()
        except InvalidReviewResultError:
            raise
        except Exception as e:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {result.review_id!r} failed semantic "
                f"verification: {e}"
            ) from e

        return result

    # -----------------------------------------------------------------
    # Per-proposal review
    # -----------------------------------------------------------------

    async def _review_single_proposal(
        self,
        *,
        proposal: ActionProposal,
        request: ReviewRequest,
        evidence_index: dict[str, Evidence],
        excluded_evidence_ids: set[str],
        capability_snapshots: dict[str, CapabilitySnapshot],
        task_records: dict[str, TaskRecordSummary],
        policy_evaluator: PolicyEvaluator,
        dedup_result: Any,
        conflict_result: Any,
        all_findings: list[ReviewFinding],
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
        ev_findings = validate_evidence_for_proposal(
            proposal,
            evidence_index,
            capability_snapshots,
            tenant_id=request.tenant_id,
            excluded_evidence_ids=excluded_evidence_ids,
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

        # 5. Authority validation
        cap_snapshot = capability_snapshots.get(proposal.created_by_agent)
        findings.extend(validate_authority(proposal, cap_snapshot))

        # 6. Idempotency validation
        risk_level = classify_risk(proposal)
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
            # Skip policy evaluation if authority already failed
            policy_result = PolicyEvaluationResult(
                proposal_id=proposal.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=[],
                policy_version=request.policy_context.policy_version,
                findings=[],
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
            try:
                policy_result = await policy_evaluator.evaluate(policy_req)
            except ReviewError:
                raise
            except Exception as e:
                raise PolicyEvaluationError(
                    f"Policy evaluator raised for proposal "
                    f"{proposal.proposal_id!r}: {e}"
                ) from e
            # R1 Section VI: validate policy result identity before
            # consuming it — fail-closed on any mismatch.
            self._validate_policy_result_identity(policy_result, proposal, request)
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

        return ProposalReview(
            proposal_id=proposal.proposal_id,
            status=status,
            findings=sorted(findings, key=lambda f: (f.finding_code, f.message)),
            matched_evidence_ids=matched_evidence_ids,
            required_approval=required_approval,
            risk_level=risk_level,
            authority_valid=authority_valid,
            policy_valid=policy_valid,
            idempotency_valid=idempotency_valid,
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
        """Fail-closed validation that the policy result belongs to the
        proposal it claims to be for.

        Raises :class:`PolicyEvaluationError` on any identity mismatch.
        """
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
        for rule in policy_result.matched_rules:
            if not rule.rule_version.strip():
                raise PolicyEvaluationError(
                    f"Matched rule {rule.rule_id!r} has blank rule_version"
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

        Order (R1 Section VII):
            conflict > deduplicated > rejected (rejection-class codes) >
            needs_input (missing-evidence / policy-needs-input) >
            needs_approval (high-risk / policy-needs-approval) > approved
        """
        # 1. Conflict takes priority over everything
        if is_conflict:
            return ReviewDecisionStatus.CONFLICT

        # 2. Exact duplicates are DEDUPLICATED (not CONFLICT, not REJECTED)
        if is_duplicate:
            return ReviewDecisionStatus.DEDUPLICATED

        finding_codes = {f.finding_code for f in findings}

        # 3. Rejection-class findings (ERROR/CRITICAL severity) → REJECTED
        has_rejection = any(
            f.finding_code in REJECTION_FINDING_CODES
            and f.severity
            in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in findings
        )
        if has_rejection:
            return ReviewDecisionStatus.REJECTED

        # 4. Missing evidence or policy needs input → NEEDS_INPUT
        if finding_codes & {CODE_EVIDENCE_MISSING, CODE_POLICY_NEEDS_INPUT}:
            return ReviewDecisionStatus.NEEDS_INPUT

        # 5. Policy needs input (fallback if finding missing)
        if policy_decision == PolicyDecision.NEEDS_INPUT:
            return ReviewDecisionStatus.NEEDS_INPUT

        # 6. High/critical risk → NEEDS_APPROVAL
        if risk_level in (ReviewRiskLevel.HIGH, ReviewRiskLevel.CRITICAL):
            return ReviewDecisionStatus.NEEDS_APPROVAL

        # 7. Policy needs approval
        if policy_decision == PolicyDecision.NEEDS_APPROVAL:
            return ReviewDecisionStatus.NEEDS_APPROVAL

        return ReviewDecisionStatus.APPROVED

    def _compute_batch_status(
        self,
        reviews: list[ProposalReview],
    ) -> ReviewBatchStatus:
        if not reviews:
            return ReviewBatchStatus.APPROVED
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
