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
    SupervisorRunResult,
)
from multi_agent.policy import DeterministicPolicyEvaluator, PolicyEvaluator
from multi_agent.review_contracts import (
    REVIEW_SCHEMA_VERSION,
    REVIEWER_VERSION,
    EvidenceDeduplicationAudit,
    PolicyContext,
    PolicyDecision,
    PolicyRule,
    ReviewDecisionStatus,
    ReviewEvidenceSnapshot,
    ReviewProposalEnvelope,
    ReviewProposalSnapshot,
    ReviewRequest,
    TaskRecordSummary,
    TraceSummary,
)
from multi_agent.review_errors import InvalidReviewRequestError
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash

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
        rules=(),
        tenant_overrides={},
    )


# ---------------------------------------------------------------------------
# Phase 4 Adapter
# ---------------------------------------------------------------------------


def build_review_request(
    supervisor_result: SupervisorRunResult,
    *,
    review_id: str,
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

    R1: ``capability_bindings`` and ``proposal_envelopes`` are built
    from the Phase 4 result's ``capability_bindings`` and
    ``merged_state.results`` — the caller no longer supplies
    ``capability_snapshots``.
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

    # R2.1 P0-5: REAL Evidence Dedup — same evidence_id + same content
    # → keep one copy; same evidence_id + different content → Fail-
    # Closed.  Previously the Adapter only recorded dedup metadata but
    # passed ALL copies (including duplicates) to the Request, which
    # caused three contradictions: Audit says deduped, Request contains
    # duplicates, and Hash is affected by copy count.
    evidence_copy, evidence_dedup_audit = _dedup_evidence(evidence_copy)

    # R2 P0-3: wrap every Evidence in a ReviewEvidenceSnapshot so the
    # snapshot_hash is verified at the Request boundary.
    evidence_snapshots: list[ReviewEvidenceSnapshot] = [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in evidence_copy
    ]

    # R2.1 P0-1: convert every ActionProposal to a deep-frozen
    # ReviewProposalSnapshot.  The Reviewer consumes ONLY snapshots —
    # never the original mutable ActionProposal.
    proposal_snapshots: list[ReviewProposalSnapshot] = [
        ReviewProposalSnapshot.from_proposal(p) for p in proposals_copy
    ]
    snapshot_by_id = {s.proposal_id: s for s in proposal_snapshots}

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

    # R1: capability_bindings from Phase 4 (defensive copy)
    capability_bindings: list[ExecutionCapabilitySnapshot] = [
        ExecutionCapabilitySnapshot.model_validate(cb.model_dump(mode="python"))
        for cb in supervisor_result.capability_bindings
    ]

    # R1: proposal_envelopes from Phase 4 results — bind each Proposal
    # to the exact AgentResult that produced it.
    proposal_envelopes: list[ReviewProposalEnvelope] = []

    # R2.1 P0-4: COPY result_origins from the SupervisorRunResult
    # (generated by SupervisorRuntime._finalize) — do NOT regenerate
    # them here.  Previously the Adapter built ResultOriginSnapshot
    # from merged_state.results, which made Envelope and Result Origin
    # two self-proofs from the same input.  Now Result Origins are
    # independent Phase 4 facts.
    result_origins: list[ResultOriginSnapshot] = [
        ResultOriginSnapshot.model_validate(ro.model_dump(mode="python"))
        for ro in supervisor_result.result_origins
    ]

    for result in merged.results:
        for ap in result.action_proposals:
            snapshot = snapshot_by_id.get(ap.proposal_id)
            if snapshot is None:
                continue
            # R2.1 P0-1: origin_hash MUST match
            # ReviewProposalEnvelope._verify_origin_hash, which
            # computes it from
            # ``self.proposal.to_action_proposal().model_dump(mode="python")``
            # (NOT from the snapshot's own model_dump).  The snapshot's
            # payload is a frozen tuple, while the ActionProposal's
            # payload is a plain dict — using the snapshot's model_dump
            # here would produce a different hash and fail validation.
            origin_hash = stable_hash(
                {
                    "proposal": snapshot.to_action_proposal().model_dump(mode="python"),
                    "run_id": supervisor_result.run_id,
                    "result_id": result.result_id,
                    "task_id": result.task_id,
                    "agent_id": result.agent_id,
                    "agent_version": result.agent_version,
                }
            )
            proposal_envelopes.append(
                ReviewProposalEnvelope(
                    proposal=snapshot,
                    run_id=supervisor_result.run_id,
                    result_id=result.result_id,
                    task_id=result.task_id,
                    agent_id=result.agent_id,
                    agent_version=result.agent_version,
                    origin_hash=origin_hash,
                )
            )

    # R2.1 P0-4: ExecutionRunIdentity is REQUIRED for a formal Phase
    # 5A Request.  The ``_extract_tenant_id()`` Legacy Fallback is
    # REMOVED — a SupervisorRunResult without run_identity cannot
    # produce a trustworthy ReviewRequest.
    run_identity = supervisor_result.run_identity
    if run_identity is None:
        raise InvalidReviewRequestError(
            f"SupervisorRunResult {supervisor_result.run_id!r} has no "
            f"run_identity — Phase 5A Adapter requires authoritative "
            f"ExecutionRunIdentity from Phase 4"
        )
    tenant_id = run_identity.tenant_id

    return ReviewRequest(
        review_id=review_id,
        run_id=supervisor_result.run_id,
        tenant_id=tenant_id,
        plan_hash=supervisor_result.plan_hash,
        registry_version=supervisor_result.registry_version,
        proposals=tuple(proposal_snapshots),
        evidence=tuple(evidence_snapshots),
        task_records=tuple(task_records),
        trace=tuple(trace),
        capability_bindings=tuple(capability_bindings),
        proposal_envelopes=tuple(proposal_envelopes),
        result_origins=tuple(result_origins),
        policy_context=policy_context or default_policy_context(),
        run_identity=run_identity,
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        evidence_dedup_audit=evidence_dedup_audit,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


def _dedup_evidence(
    evidence: list[Evidence],
) -> tuple[list[Evidence], EvidenceDeduplicationAudit]:
    """R2.1 P0-5: REAL Evidence Dedup — not just audit recording.

    Same ``evidence_id`` + same content hash → keep ONE copy and
    record the id in ``deduped_evidence_ids``.

    Same ``evidence_id`` + DIFFERENT content hash → Fail-Closed:
    raise :class:`InvalidReviewRequestError` so the tampered Evidence
    cannot enter the Review pipeline.

    Returns ``(deduplicated_evidence, audit)`` where
    ``deduplicated_evidence`` contains at most one copy per
    ``evidence_id``.

    Previously the Adapter only recorded dedup metadata but passed ALL
    copies to the Request — causing three contradictions: Audit said
    deduped, Request contained duplicates, and Hash was affected by
    copy count.
    """
    from multi_agent.evidence_review import compute_review_evidence_hash

    # Group by evidence_id to detect content mismatches.
    by_id: dict[str, list[Evidence]] = {}
    for ev in evidence:
        by_id.setdefault(ev.evidence_id, []).append(ev)

    deduped_ids: set[str] = set()
    result: list[Evidence] = []
    for ev_id in sorted(by_id):
        group = by_id[ev_id]
        if len(group) == 1:
            result.append(group[0])
            continue
        # Multiple entries with the same evidence_id — check content.
        hashes = {compute_review_evidence_hash(ev) for ev in group}
        if len(hashes) == 1:
            # Same content — benign duplicate, keep one.
            deduped_ids.add(ev_id)
            result.append(group[0])
        else:
            # R2.1 P0-5: different content → Fail-Closed.
            raise InvalidReviewRequestError(
                f"Evidence {ev_id!r} has {len(hashes)} distinct content "
                f"hashes across {len(group)} copies — Adapter rejects "
                f"to prevent tampered Evidence from entering the Review"
            )

    audit_hash = stable_hash(
        {
            "deduped_evidence_ids": sorted(deduped_ids),
            "original_count": len(evidence),
            "snapshot_count": len(result),
        }
    )
    audit = EvidenceDeduplicationAudit(
        deduped_evidence_ids=frozenset(deduped_ids),
        original_count=len(evidence),
        snapshot_count=len(result),
        audit_hash=audit_hash,
    )
    return result, audit


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

    R2 S9 / final-verification: ``expected_evidence_error`` and
    ``expected_authority_violation`` are STRUCTURED expected-outcome
    flags.  The metrics computation reads these flags — it MUST NOT
    infer error categories from :attr:`name` (which would be a label
    leak).  Renaming a fixture must not change the metrics.

    Fixtures are constructed WITHOUT reading the wall-clock — the
    ``created_at`` fields on Proposals and Evidence use a fixed
    timestamp so the ``request_hash`` is reproducible.
    """

    name: str
    request: ReviewRequest
    expected_blocked_proposal_ids: frozenset[str]
    expected_conflicted_proposal_ids: frozenset[str] = frozenset()
    # R2 S9: structured expected-outcome flags — read by
    # compute_review_metrics INSTEAD of inferring from ``name``.
    expected_evidence_error: bool = False
    expected_authority_violation: bool = False
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


def _make_capability_binding(
    task_id: str,
    agent_id: str,
    capability: AgentCapability,
) -> ExecutionCapabilitySnapshot:
    """Build an :class:`ExecutionCapabilitySnapshot` with a correct
    ``binding_hash``."""
    binding_hash = stable_hash(
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_version": capability.version,
            "capability": capability.model_dump(mode="python"),
        }
    )
    return ExecutionCapabilitySnapshot(
        task_id=task_id,
        agent_id=agent_id,
        agent_version=capability.version,
        capability=capability,
        binding_hash=binding_hash,
    )


def _make_envelope(
    proposal: ActionProposal,
    *,
    run_id: str = "run-fixture",
    result_id: str = "result-fixture",
    task_id: str = "task-fixture",
    agent_id: str | None = None,
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    """Build a :class:`ReviewProposalEnvelope` with a correct ``origin_hash``.

    R2.1 P0-1: converts the :class:`ActionProposal` to a
    :class:`ReviewProposalSnapshot` before wrapping it in the envelope.
    """
    aid = agent_id or proposal.created_by_agent
    snapshot = ReviewProposalSnapshot.from_proposal(proposal)
    # R2.1 P0-1: origin_hash MUST match the envelope's validator which
    # uses to_action_proposal().model_dump(mode="python").
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
    """R2 P0-3: wrap raw Evidence in ReviewEvidenceSnapshot."""
    return [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in evidence
    ]


def _make_request(
    review_id: str,
    proposals: list[ActionProposal],
    evidence: list[Evidence],
    *,
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
    task_records: list[TaskRecordSummary] | None = None,
    proposal_envelopes: list[ReviewProposalEnvelope] | None = None,
    policy_context: PolicyContext | None = None,
) -> ReviewRequest:
    # R2.1 P0-5: REAL Evidence Dedup before wrapping in snapshots.
    evidence, evidence_dedup_audit = _dedup_evidence(evidence)
    # R2 P0-3: wrap evidence in ReviewEvidenceSnapshot
    evidence_snapshots = _wrap_evidence(evidence)
    # R2.1 P0-1: convert proposals to deep-frozen ReviewProposalSnapshot
    proposal_snapshots = [ReviewProposalSnapshot.from_proposal(p) for p in proposals]
    # R1: auto-build envelopes for every proposal if not supplied
    if proposal_envelopes is None:
        proposal_envelopes = [_make_envelope(p) for p in proposals]

    # R2.1 P0-4: build a fixture ExecutionRunIdentity (required).
    run_identity = ExecutionRunIdentity(
        run_id="run-fixture",
        tenant_id="tenant-fixture",
        plan_hash="plan-fixture-hash",
        registry_version="registry-fixture-v1",
        identity_hash=stable_hash(
            {
                "run_id": "run-fixture",
                "tenant_id": "tenant-fixture",
                "plan_hash": "plan-fixture-hash",
                "registry_version": "registry-fixture-v1",
            }
        ),
    )

    # R2.1 P0-4: build a fixture ResultOriginSnapshot for the
    # fixture result.  The Request validator requires a matching
    # result_origin for every envelope's result_id.
    fixture_proposal_hashes = tuple(
        sorted(
            (s.proposal_id, s.proposal_hash)
            for s in proposal_snapshots
            if s.proposal_hash
        )
    )
    fixture_evidence_hashes = tuple(
        sorted((ev.evidence.evidence_id, ev.snapshot_hash) for ev in evidence_snapshots)
    )
    result_origin_hash = stable_hash(
        {
            "run_id": "run-fixture",
            "tenant_id": "tenant-fixture",
            "result_id": "result-fixture",
            "task_id": "task-fixture",
            "agent_id": "fixture_agent",
            "agent_version": "1.0.0",
            "proposal_hashes": sorted(fixture_proposal_hashes),
            "evidence_hashes": sorted(fixture_evidence_hashes),
        }
    )
    result_origins = (
        ResultOriginSnapshot(
            run_id="run-fixture",
            tenant_id="tenant-fixture",
            result_id="result-fixture",
            task_id="task-fixture",
            agent_id="fixture_agent",
            agent_version="1.0.0",
            proposal_hashes=fixture_proposal_hashes,
            evidence_hashes=fixture_evidence_hashes,
            origin_hash=result_origin_hash,
        ),
    )

    return ReviewRequest(
        review_id=review_id,
        run_id="run-fixture",
        tenant_id="tenant-fixture",
        plan_hash="plan-fixture-hash",
        registry_version="registry-fixture-v1",
        proposals=tuple(proposal_snapshots),
        evidence=tuple(evidence_snapshots),
        task_records=tuple(
            task_records
            or [
                TaskRecordSummary(
                    task_id="task-fixture",
                    agent_id="fixture_agent",
                    status="completed",
                )
            ]
        ),
        trace=(
            TraceSummary(
                sequence=0,
                event_type="run_started",
                task_id=None,
                agent_id=None,
            ),
        ),
        capability_bindings=tuple(capability_bindings or []),
        proposal_envelopes=tuple(proposal_envelopes),
        result_origins=result_origins,
        policy_context=policy_context or default_policy_context(),
        run_identity=run_identity,
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        evidence_dedup_audit=evidence_dedup_audit,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
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
    cap_binding_read = _make_capability_binding(
        "task-fixture",
        "fixture_agent",
        _make_capability(
            "fixture_agent",
            authority=AgentAuthority.READ,
            allowed_tools=frozenset({"crm_reader.get_customers"}),
        ),
    )
    cap_binding_propose = _make_capability_binding(
        "task-fixture",
        "fixture_agent",
        _make_capability(
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
                capability_bindings=[cap_binding_read],
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
                capability_bindings=[cap_binding_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-missing-ev-001"}),
            expected_evidence_error=True,
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
                capability_bindings=[cap_binding_read],
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
                capability_bindings=[cap_binding_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-foreign-ev-001"}),
            expected_evidence_error=True,
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
                capability_bindings=[cap_binding_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-auth-violation-001"}),
            expected_authority_violation=True,
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
                capability_bindings=[cap_binding_read],
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
                capability_bindings=[cap_binding_propose],
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
                capability_bindings=[cap_binding_propose],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-high-risk-001"}),
            description="High-risk action that requires human approval.",
        )
    )

    # 9. Explicit policy deny — uses a PolicyContext rule that denies
    #    the action_type explicitly.
    # R2 P0-6: rules are strictly-typed PolicyRule, not raw dicts.
    deny_ctx = PolicyContext(
        policy_version="ma-05a-deny-fixture",
        rules=(
            PolicyRule(
                rule_id="deny-report-generate",
                rule_version="ma-05a-deny-fixture",
                priority=100,
                effect=PolicyDecision.DENIED,
                action_type="report.generate",
            ),
        ),
    )
    fixtures.append(
        ReviewFixture(
            name="policy_deny",
            request=_make_request(
                "review-policy-deny",
                proposals=[
                    _make_proposal(
                        "prop-policy-deny-001",
                        action_type="report.generate",
                        evidence_ids=["ev-policy-deny-001"],
                    )
                ],
                evidence=[_make_evidence("ev-policy-deny-001")],
                capability_bindings=[cap_binding_read],
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
                capability_bindings=[cap_binding_read],
            ),
            expected_blocked_proposal_ids=frozenset({"prop-dup-002"}),
            expected_conflicted_proposal_ids=frozenset(),
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
                capability_bindings=[cap_binding_propose],
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
                capability_bindings=[cap_binding_read],
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
    :attr:`ReviewFixture.expected_blocked_proposal_ids`,
    :attr:`ReviewFixture.expected_conflicted_proposal_ids`,
    :attr:`ReviewFixture.expected_evidence_error`, and
    :attr:`ReviewFixture.expected_authority_violation` to determine
    expected outcomes.  Renaming a fixture must not change the
    computed metrics (R2 S9: no label leakage).
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

            # Track expected evidence / authority violations by
            # STRUCTURED flags — NOT by fixture.name (R2 S9: no label
            # leakage).  Renaming a fixture must not change metrics.
            if fixture.expected_evidence_error:
                total_evidence_errors_expected += 1
            if fixture.expected_authority_violation:
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
                # Evidence error detection — gated by the STRUCTURED
                # expected_evidence_error flag, NOT fixture.name.
                if any(
                    f.finding_code.startswith("review.evidence.")
                    and f.severity in ("error", "critical")
                    for f in r.findings
                ):
                    if fixture.expected_evidence_error:
                        if r.proposal_id in fixture.expected_blocked_proposal_ids:
                            total_evidence_errors_detected += 1
                # Authority violation detection — gated by the
                # STRUCTURED expected_authority_violation flag.
                if any(
                    f.finding_code.startswith("review.authority.")
                    and f.severity in ("error", "critical")
                    for f in r.findings
                ):
                    if fixture.expected_authority_violation:
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
