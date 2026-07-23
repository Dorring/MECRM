"""Phase 5A Evidence Validation.

Pure-function Evidence reference validation.  No I/O, no network, no
side-effects.

Per Phase 5A Section 7.2 the Reviewer must not silently ignore
invalid Evidence.  Every invalid reference becomes a
:class:`ReviewFinding` on the affected Proposal's review.

Validation checks (in order):

1. **Existence** — every ``evidence_id`` referenced by a Proposal
   must exist in the Evidence index.
2. **Tenant consistency** — every referenced Evidence must belong to
   the same tenant as the Proposal.
3. **Source-agent consistency** — every referenced Evidence's
   ``source_agent`` must either match the Proposal's
   ``created_by_agent`` OR be present in the Capability Snapshots
   carried by the :class:`ReviewRequest`.
4. **Content-hash validity** — if Evidence has a ``content_hash`` it
   must be a non-empty hex string; a tampered / empty hash is
   rejected.
5. **Type compatibility** — the Evidence type must be compatible
   with the Proposal's ``action_type`` per
   :data:`_ACTION_EVIDENCE_REQUIREMENTS`.
6. **Duplicate references** — the same ``evidence_id`` appearing
   twice in the same Proposal's ``evidence_ids`` list is flagged.
7. **Dangling references** — Evidence present in the index but
   referenced by no Proposal is flagged as ``INFO`` (informational,
   not a rejection).
"""

from __future__ import annotations

from typing import Any

from multi_agent.contracts import ActionProposal, Evidence, EvidenceType
from multi_agent.review_contracts import (
    CODE_EVIDENCE_DANGLING,
    CODE_EVIDENCE_DUPLICATE,
    CODE_EVIDENCE_FOREIGN_TENANT,
    CODE_EVIDENCE_HASH_MISMATCH,
    CODE_EVIDENCE_MISSING,
    CODE_EVIDENCE_TYPE_MISMATCH,
    CapabilitySnapshot,
    ReviewFinding,
    ReviewFindingSeverity,
)


# ---------------------------------------------------------------------------
# Action → required Evidence type mapping.
# ---------------------------------------------------------------------------

# Each action_type maps to the set of EvidenceTypes that may legitimately
# support it.  An empty set means "any evidence type is accepted" — used
# for low-risk read-only actions.  A non-empty set means the Proposal
# must reference at least one Evidence of a compatible type.
_ACTION_EVIDENCE_REQUIREMENTS: dict[str, frozenset[EvidenceType]] = {
    "report.generate": frozenset(),  # any
    "summary.compile": frozenset(),  # any
    "metric.query": frozenset({EvidenceType.METRIC}),
    "crm.tag.update": frozenset(
        {
            EvidenceType.CUSTOMER,
            EvidenceType.CONTACT,
            EvidenceType.TICKET,
            EvidenceType.DEAL,
        }
    ),
    "crm.status.update": frozenset(
        {
            EvidenceType.CUSTOMER,
            EvidenceType.TICKET,
            EvidenceType.DEAL,
        }
    ),
    "crm.note.add": frozenset(
        {
            EvidenceType.CUSTOMER,
            EvidenceType.CONTACT,
            EvidenceType.TICKET,
            EvidenceType.DEAL,
        }
    ),
    "crm.owner.assign": frozenset({EvidenceType.CUSTOMER}),
    "crm.escalate": frozenset({EvidenceType.TICKET, EvidenceType.CUSTOMER}),
    "refund.issue": frozenset(
        {
            EvidenceType.CUSTOMER,
            EvidenceType.TICKET,
            EvidenceType.DEAL,
        }
    ),
    "contract.amend": frozenset({EvidenceType.DEAL, EvidenceType.CUSTOMER}),
    "notification.bulk_send": frozenset(
        {
            EvidenceType.CUSTOMER,
            EvidenceType.CONTACT,
        }
    ),
    "permission.change": frozenset(
        {
            EvidenceType.CUSTOMER,
            EvidenceType.AUDIT_EVENT,
        }
    ),
}


def _hex_hash_valid(value: str | None) -> bool:
    """Return True iff *value* is a non-empty hex string."""
    if not value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return len(value) > 0


def _make_finding(
    *,
    code: str,
    severity: ReviewFindingSeverity,
    message: str,
    proposal: ActionProposal,
    evidence_ids: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> ReviewFinding:
    return ReviewFinding(
        finding_code=code,
        severity=severity,
        message=message,
        proposal_id=proposal.proposal_id,
        task_id=None,
        agent_id=proposal.created_by_agent,
        evidence_ids=evidence_ids or [],
        policy_source="evidence_review@ma-05a",
        details=details or {},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_review_evidence_hash(evidence: Evidence) -> str:
    """Single source of truth for Evidence content integrity.

    Hashes all canonical Evidence fields EXCEPT the self-referential
    ``content_hash`` field, so a tampered Evidence that keeps the old
    declared hash is detected.
    """
    from multi_agent.serialization import stable_hash

    payload = evidence.model_dump(mode="python")
    payload.pop("content_hash", None)
    return stable_hash(payload)


def build_evidence_index(
    evidence: list[Evidence],
) -> tuple[dict[str, Evidence], set[str]]:
    """Return a deterministic ``evidence_id → Evidence`` mapping and a
    set of excluded evidence_ids.

    If the input contains duplicate evidence_ids with the SAME content
    (compared via :func:`compute_review_evidence_hash`), one copy is
    kept (benign deduplication).  If duplicates have DIFFERENT content,
    ALL copies of that id are excluded from the index (fail closed) and
    the id is added to the excluded set.  The caller is responsible for
    surfacing the content-mismatch duplicate as a :class:`ReviewFinding`
    via :func:`detect_duplicate_evidence`.
    """
    # Group by evidence_id to detect duplicates with different content.
    seen: dict[str, list[Evidence]] = {}
    for ev in evidence:
        seen.setdefault(ev.evidence_id, []).append(ev)

    index: dict[str, Evidence] = {}
    excluded: set[str] = set()
    for ev_id, group in seen.items():
        if len(group) == 1:
            index[ev_id] = group[0]
        else:
            # Multiple entries with the same evidence_id — check
            # whether their review hashes all match.
            hashes = {compute_review_evidence_hash(ev) for ev in group}
            if len(hashes) == 1:
                # Same content — benign duplicate, keep one.
                index[ev_id] = group[0]
            else:
                # Different content — fail closed: exclude all copies.
                excluded.add(ev_id)
    return index, excluded


def detect_duplicate_evidence(
    evidence: list[Evidence],
) -> list[ReviewFinding]:
    """Return findings for evidence_ids that appear more than once
    with different content.

    Same evidence_id + same content is a benign duplicate (already
    deduplicated by Phase 4 merge).  Same evidence_id + different
    content (compared via :func:`compute_review_evidence_hash`) is a
    content_mismatch — all copies are flagged.
    """
    findings: list[ReviewFinding] = []
    seen: dict[str, list[Evidence]] = {}
    for ev in evidence:
        seen.setdefault(ev.evidence_id, []).append(ev)

    for ev_id in sorted(seen):
        group = seen[ev_id]
        if len(group) <= 1:
            continue
        hashes = {compute_review_evidence_hash(ev) for ev in group}
        if len(hashes) > 1:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_EVIDENCE_DUPLICATE,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Evidence {ev_id!r} has {len(hashes)} distinct "
                        f"content hashes; all copies excluded"
                    ),
                    proposal_id="(evidence-index)",
                    evidence_ids=[ev_id],
                    policy_source="evidence_review@ma-05a",
                    details={
                        "evidence_id": ev_id,
                        "copy_count": len(group),
                        "distinct_hashes": len(hashes),
                    },
                )
            )
    return findings


def validate_evidence_for_proposal(
    proposal: ActionProposal,
    evidence_index: dict[str, Evidence],
    capability_snapshots: dict[str, CapabilitySnapshot],
    *,
    tenant_id: str,
    excluded_evidence_ids: set[str] | None = None,
) -> list[ReviewFinding]:
    """Validate every Evidence reference on *proposal*.

    Returns a list of :class:`ReviewFinding` entries — empty list
    means all references are valid.  Never raises (the Reviewer
    surfaces findings rather than throwing for business-level
    evidence problems).

    ``excluded_evidence_ids`` contains evidence_ids that were excluded
    from the index due to content mismatch (tamper detection).  Every
    Proposal referencing an excluded id gets a blocking
    ``CODE_EVIDENCE_HASH_MISMATCH`` ERROR finding.
    """
    findings: list[ReviewFinding] = []
    excluded = excluded_evidence_ids or set()

    # 1. Duplicate references within the same Proposal
    seen_in_proposal: dict[str, int] = {}
    for ev_id in proposal.evidence_ids:
        seen_in_proposal[ev_id] = seen_in_proposal.get(ev_id, 0) + 1
    duplicates = sorted(
        {ev_id for ev_id, count in seen_in_proposal.items() if count > 1}
    )
    if duplicates:
        findings.append(
            _make_finding(
                code=CODE_EVIDENCE_DUPLICATE,
                severity=ReviewFindingSeverity.WARNING,
                message=(
                    f"Proposal {proposal.proposal_id!r} references "
                    f"duplicate evidence_ids: {duplicates!r}"
                ),
                proposal=proposal,
                evidence_ids=duplicates,
                details={"duplicate_evidence_ids": duplicates},
            )
        )

    # 2. Per-reference checks
    required_types = _ACTION_EVIDENCE_REQUIREMENTS.get(proposal.action_type)
    # If action_type is unknown, required_types is None — we do NOT
    # type-check, but the Policy evaluator will reject the action
    # separately via CODE_ACTION_UNKNOWN_TYPE.

    matched_evidence: list[Evidence] = []
    for ev_id in proposal.evidence_ids:
        # 2a. Excluded due to content mismatch (tamper detection)
        if ev_id in excluded:
            findings.append(
                _make_finding(
                    code=CODE_EVIDENCE_HASH_MISMATCH,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Proposal {proposal.proposal_id!r} references "
                        f"evidence {ev_id!r} which was excluded due to "
                        f"content mismatch (tamper detected)"
                    ),
                    proposal=proposal,
                    evidence_ids=[ev_id],
                    details={
                        "evidence_id": ev_id,
                        "reason": "content_mismatch_excluded",
                    },
                )
            )
            continue

        ev = evidence_index.get(ev_id)
        if ev is None:
            findings.append(
                _make_finding(
                    code=CODE_EVIDENCE_MISSING,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Proposal {proposal.proposal_id!r} references "
                        f"missing evidence {ev_id!r}"
                    ),
                    proposal=proposal,
                    evidence_ids=[ev_id],
                    details={"missing_evidence_id": ev_id},
                )
            )
            continue

        # 2a. Tenant consistency
        if ev.tenant_id != tenant_id:
            findings.append(
                _make_finding(
                    code=CODE_EVIDENCE_FOREIGN_TENANT,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Evidence {ev_id!r} tenant {ev.tenant_id!r} != "
                        f"expected {tenant_id!r}"
                    ),
                    proposal=proposal,
                    evidence_ids=[ev_id],
                    details={
                        "evidence_id": ev_id,
                        "evidence_tenant": ev.tenant_id,
                        "expected_tenant": tenant_id,
                    },
                )
            )
            continue

        # 2b. Source-agent consistency
        # Evidence.source_agent must be either the Proposal's
        # created_by_agent OR a registered agent in the Capability
        # Snapshots.  This prevents a Proposal from borrowing Evidence
        # produced by an unrelated agent.
        if (
            ev.source_agent != proposal.created_by_agent
            and ev.source_agent not in capability_snapshots
        ):
            findings.append(
                _make_finding(
                    code=CODE_EVIDENCE_DANGLING,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Evidence {ev_id!r} source_agent "
                        f"{ev.source_agent!r} is neither the Proposal's "
                        f"created_by_agent {proposal.created_by_agent!r} "
                        f"nor a registered agent in the Capability Snapshots"
                    ),
                    proposal=proposal,
                    evidence_ids=[ev_id],
                    details={
                        "evidence_id": ev_id,
                        "source_agent": ev.source_agent,
                        "proposal_agent": proposal.created_by_agent,
                    },
                )
            )
            continue

        # 2c. Content-hash validity
        if ev.content_hash is not None and not _hex_hash_valid(ev.content_hash):
            findings.append(
                _make_finding(
                    code=CODE_EVIDENCE_HASH_MISMATCH,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Evidence {ev_id!r} has an invalid content_hash "
                        f"{ev.content_hash!r}"
                    ),
                    proposal=proposal,
                    evidence_ids=[ev_id],
                    details={
                        "evidence_id": ev_id,
                        "content_hash": ev.content_hash,
                    },
                )
            )
            continue

        # 2d. Type compatibility
        if required_types and required_types:
            # Non-empty set — the Evidence type must be in the set.
            if ev.evidence_type not in required_types:
                findings.append(
                    _make_finding(
                        code=CODE_EVIDENCE_TYPE_MISMATCH,
                        severity=ReviewFindingSeverity.ERROR,
                        message=(
                            f"Evidence {ev_id!r} type "
                            f"{ev.evidence_type.value!r} is not compatible "
                            f"with action {proposal.action_type!r}; "
                            f"required one of {sorted(t.value for t in required_types)!r}"
                        ),
                        proposal=proposal,
                        evidence_ids=[ev_id],
                        details={
                            "evidence_id": ev_id,
                            "evidence_type": ev.evidence_type.value,
                            "action_type": proposal.action_type,
                            "required_types": sorted(t.value for t in required_types),
                        },
                    )
                )
                continue

        matched_evidence.append(ev)

    # 3. High-risk proposals must reference at least one valid Evidence
    # (after all invalid references have been filtered out).
    from multi_agent.contracts import ActionRiskLevel

    if proposal.risk_level == ActionRiskLevel.HIGH and not matched_evidence:
        findings.append(
            _make_finding(
                code=CODE_EVIDENCE_MISSING,
                severity=ReviewFindingSeverity.ERROR,
                message=(
                    f"High-risk Proposal {proposal.proposal_id!r} has no "
                    f"valid Evidence references after validation"
                ),
                proposal=proposal,
                evidence_ids=list(proposal.evidence_ids),
                details={
                    "risk_level": proposal.risk_level.value,
                    "referenced_count": len(proposal.evidence_ids),
                    "valid_count": len(matched_evidence),
                },
            )
        )

    return findings


def detect_dangling_evidence(
    proposals: list[ActionProposal],
    evidence_index: dict[str, Evidence],
) -> list[ReviewFinding]:
    """Return INFO findings for Evidence in the index that is not
    referenced by any Proposal.

    This is informational — a Phase 4 run may legitimately produce
    Evidence that no Proposal ends up citing.  The finding is surfaced
    for audit completeness.
    """
    referenced: set[str] = set()
    for p in proposals:
        referenced.update(p.evidence_ids)

    dangling = sorted(set(evidence_index.keys()) - referenced)
    findings: list[ReviewFinding] = []
    for ev_id in dangling:
        findings.append(
            ReviewFinding(
                finding_code=CODE_EVIDENCE_DANGLING,
                severity=ReviewFindingSeverity.INFO,
                message=(
                    f"Evidence {ev_id!r} is present in the index but "
                    f"not referenced by any Proposal"
                ),
                proposal_id="(evidence-index)",
                evidence_ids=[ev_id],
                policy_source="evidence_review@ma-05a",
                details={"evidence_id": ev_id},
            )
        )
    return findings


__all__ = [
    "build_evidence_index",
    "compute_review_evidence_hash",
    "detect_dangling_evidence",
    "detect_duplicate_evidence",
    "validate_evidence_for_proposal",
]
