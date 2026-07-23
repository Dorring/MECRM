"""Parallel agent result merge.

Two-phase merge: group by ID, compare content hashes.
Same ID + single content hash -> keep one.
Same ID + multiple content hashes -> ALL excluded (content_mismatch).
No first/last preference.

Processing order (R9):

1. Filter foreign-tenant results.
2. Group by result_id; resolve result dedup / content_mismatch.
3. Collect Evidence and ActionProposal ONLY from surviving results.
4. Resolve evidence dedup / content_mismatch.
5. Resolve proposal integrity, content_mismatch, foreign tenant.
6. Verify evidence references; exclude dangling proposals.
7. Filter merged_proposals by excluded_proposal_ids (Fail-Closed).
8. Scrub excluded proposals from results[*].action_proposals.
9. Construct MergedState.

``excluded_proposal_ids`` is a proposal-ID-level Fail-Closed set: if ANY
copy of a proposal_id is judged invalid, ALL copies of that id are
removed from both ``merged_proposals`` and every
``results[*].action_proposals``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from pydantic import ConfigDict, Field, ValidationError

from multi_agent.contracts import (
    ActionProposal,
    AgentResult,
    Evidence,
    StrictContract,
)
from multi_agent.errors import ProposalHashMismatchError
from multi_agent.serialization import content_hash

# ---------------------------------------------------------------------------
# Merge output models
# ---------------------------------------------------------------------------


class MergeConflict(StrictContract):
    conflict_type: str
    detail: str = ""
    conflicting_ids: list[str] = Field(default_factory=list)


class MergedState(StrictContract):
    model_config = ConfigDict(extra="forbid", revalidate_instances="never")

    results: list[AgentResult] = Field(default_factory=list)
    merged_evidence: list[Evidence] = Field(default_factory=list)
    merged_proposals: list[ActionProposal] = Field(default_factory=list)
    conflicts: list[MergeConflict] = Field(default_factory=list)
    merged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Merge function
# ---------------------------------------------------------------------------


def merge_parallel_results(
    results: list[AgentResult],
    *,
    expected_tenant_id: str,
) -> MergedState:
    """Merge N parallel AgentResults.  Order-independent two-phase algorithm."""

    if not results:
        return MergedState(merged_at=datetime.now(timezone.utc))

    sorted_results = sorted(results, key=lambda r: r.result_id)
    conflicts: list[MergeConflict] = []
    # Unified set of proposal IDs that must not survive into MergedState.
    # Populated by every exclusion path and used to scrub both
    # merged_proposals and results[*].action_proposals at the end.
    excluded_proposal_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Step 1: filter foreign-tenant results
    # ------------------------------------------------------------------
    local_results: list[AgentResult] = []
    for r in sorted_results:
        if r.tenant_id != expected_tenant_id:
            conflicts.append(
                MergeConflict(
                    conflict_type="foreign_tenant",
                    detail=(
                        f"Result {r.result_id!r} tenant {r.tenant_id!r}"
                        f" != expected {expected_tenant_id!r}"
                    ),
                    conflicting_ids=[r.result_id],
                )
            )
        else:
            local_results.append(r)

    # ------------------------------------------------------------------
    # Step 2: group by result_id and resolve dedup / content_mismatch
    # ------------------------------------------------------------------
    result_groups: dict[str, list[AgentResult]] = defaultdict(list)
    for r in local_results:
        result_groups[r.result_id].append(r)

    deduped_results: list[AgentResult] = []
    for result_id, r_group in sorted(result_groups.items(), key=lambda x: x[0]):
        r_hashes = {content_hash(r.model_dump(mode="json")) for r in r_group}
        if len(r_hashes) == 1:
            deduped_results.append(r_group[0])
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=(
                        f"Result {result_id!r} has {len(r_hashes)} distinct"
                        f" content hashes; all excluded"
                    ),
                    conflicting_ids=[result_id],
                )
            )

    # ------------------------------------------------------------------
    # Step 3: collect Evidence and Proposals ONLY from surviving results
    # ------------------------------------------------------------------
    evidence_groups: dict[str, list[Evidence]] = defaultdict(list)
    proposal_groups: dict[str, list[ActionProposal]] = defaultdict(list)

    for r in deduped_results:
        for ev in r.evidence:
            if ev.tenant_id == expected_tenant_id:
                evidence_groups[ev.evidence_id].append(ev)
            else:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=(
                            f"Evidence {ev.evidence_id!r} tenant"
                            f" {ev.tenant_id!r} != expected"
                            f" {expected_tenant_id!r}"
                        ),
                        conflicting_ids=[ev.evidence_id],
                    )
                )
        for p in r.action_proposals:
            if p.tenant_id != expected_tenant_id:
                excluded_proposal_ids.add(p.proposal_id)
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=(
                            f"Proposal {p.proposal_id!r} tenant"
                            f" {p.tenant_id!r} != expected"
                            f" {expected_tenant_id!r}"
                        ),
                        conflicting_ids=[p.proposal_id],
                    )
                )
                continue
            try:
                p.verify_integrity()
            except (
                ProposalHashMismatchError,
                ValidationError,
                ValueError,
                TypeError,
            ):
                excluded_proposal_ids.add(p.proposal_id)
                conflicts.append(
                    MergeConflict(
                        conflict_type="proposal_integrity_failure",
                        detail=(
                            f"Proposal {p.proposal_id!r} failed integrity"
                            f" check; excluded"
                        ),
                        conflicting_ids=[p.proposal_id],
                    )
                )
                continue
            proposal_groups[p.proposal_id].append(p)

    # ------------------------------------------------------------------
    # Step 4: resolve evidence dedup / content_mismatch
    # ------------------------------------------------------------------
    merged_evidence: list[Evidence] = []
    for ev_id, ev_group in sorted(evidence_groups.items(), key=lambda x: x[0]):
        ev_hashes = {content_hash(ev.model_dump(mode="json")) for ev in ev_group}
        if len(ev_hashes) == 1:
            merged_evidence.append(ev_group[0])
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=(
                        f"Evidence {ev_id!r} has {len(ev_hashes)} distinct"
                        f" content hashes; all excluded"
                    ),
                    conflicting_ids=[ev_id],
                )
            )

    # ------------------------------------------------------------------
    # Step 5: resolve proposal dedup / content_mismatch
    # ------------------------------------------------------------------
    merged_proposals: list[ActionProposal] = []
    seen_hashes: set[str] = set()
    for prop_id, p_group in sorted(proposal_groups.items(), key=lambda x: x[0]):
        p_hashes = {p.proposal_hash for p in p_group}
        if len(p_hashes) == 1:
            p = p_group[0]
            if p.proposal_hash not in seen_hashes:
                seen_hashes.add(p.proposal_hash)
                merged_proposals.append(p)
        else:
            excluded_proposal_ids.add(prop_id)
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=(
                        f"Proposal {prop_id!r} has {len(p_hashes)} distinct"
                        f" content hashes; all excluded"
                    ),
                    conflicting_ids=[prop_id],
                )
            )

    # ------------------------------------------------------------------
    # Step 6: verify evidence references — exclude dangling proposals
    # ------------------------------------------------------------------
    available_evidence_ids = {ev.evidence_id for ev in merged_evidence}
    ref_checked_proposals: list[ActionProposal] = []
    for p in merged_proposals:
        missing = sorted(set(p.evidence_ids) - available_evidence_ids)
        if missing:
            excluded_proposal_ids.add(p.proposal_id)
            conflicts.append(
                MergeConflict(
                    conflict_type="proposal_missing_evidence",
                    detail=(
                        f"Proposal {p.proposal_id!r} references"
                        f" missing evidence {missing!r}"
                    ),
                    conflicting_ids=[p.proposal_id, *missing],
                )
            )
            continue
        ref_checked_proposals.append(p)

    # ------------------------------------------------------------------
    # Step 7: Fail-Closed filter — any excluded proposal_id is removed
    # from merged_proposals even if another copy passed earlier checks.
    # ------------------------------------------------------------------
    merged_proposals = [
        p for p in ref_checked_proposals if p.proposal_id not in excluded_proposal_ids
    ]

    # ------------------------------------------------------------------
    # Step 8: scrub excluded proposals from results[*].action_proposals
    # ------------------------------------------------------------------
    if excluded_proposal_ids:
        cleaned_results: list[AgentResult] = []
        for r in deduped_results:
            if any(p.proposal_id in excluded_proposal_ids for p in r.action_proposals):
                kept = [
                    p
                    for p in r.action_proposals
                    if p.proposal_id not in excluded_proposal_ids
                ]
                cleaned_results.append(r.model_copy(update={"action_proposals": kept}))
            else:
                cleaned_results.append(r)
        deduped_results = cleaned_results

    return MergedState(
        results=deduped_results,
        merged_evidence=merged_evidence,
        merged_proposals=merged_proposals,
        conflicts=conflicts,
        merged_at=datetime.now(timezone.utc),
    )
