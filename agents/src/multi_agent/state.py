"""Parallel agent result merge.

Two-phase merge: group by ID, compare content hashes.
Same ID + single content hash → keep one.
Same ID + multiple content hashes → ALL excluded (content_mismatch).
No first/last preference.

Invalid proposals (integrity failure, content mismatch, foreign tenant,
missing evidence) are tracked in a single ``excluded_proposal_ids`` set
and removed from BOTH ``merged_proposals`` AND every
``results[*].action_proposals`` so that no executable path can reach a
known-bad proposal.
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
    # Populated by every exclusion path and used to scrub results at the end.
    excluded_proposal_ids: set[str] = set()

    # -- Filter foreign tenants ----------------------------------------------
    local_results: list[AgentResult] = []
    for r in sorted_results:
        if r.tenant_id != expected_tenant_id:
            conflicts.append(
                MergeConflict(
                    conflict_type="foreign_tenant",
                    detail=f"Result {r.result_id!r} tenant {r.tenant_id!r} != expected {expected_tenant_id!r}",
                    conflicting_ids=[r.result_id],
                )
            )
        else:
            local_results.append(r)

    # -- Phase 1: group by ID ------------------------------------------------
    result_groups: dict[str, list[AgentResult]] = defaultdict(list)
    evidence_groups: dict[str, list[Evidence]] = defaultdict(list)
    proposal_groups: dict[str, list[ActionProposal]] = defaultdict(list)

    for r in local_results:
        result_groups[r.result_id].append(r)
        for ev in r.evidence:
            if ev.tenant_id == expected_tenant_id:
                evidence_groups[ev.evidence_id].append(ev)
            else:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=f"Evidence {ev.evidence_id!r} tenant {ev.tenant_id!r} != expected {expected_tenant_id!r}",
                        conflicting_ids=[ev.evidence_id],
                    )
                )
        for p in r.action_proposals:
            if p.tenant_id != expected_tenant_id:
                excluded_proposal_ids.add(p.proposal_id)
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=f"Proposal {p.proposal_id!r} tenant {p.tenant_id!r} != expected {expected_tenant_id!r}",
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
                        detail=f"Proposal {p.proposal_id!r} failed integrity check; excluded",
                        conflicting_ids=[p.proposal_id],
                    )
                )
                continue
            proposal_groups[p.proposal_id].append(p)

    # -- Phase 2: resolve each group -----------------------------------------
    deduped_results: list[AgentResult] = []
    for result_id, r_group in sorted(result_groups.items(), key=lambda x: x[0]):
        r_hashes = {content_hash(r.model_dump(mode="json")) for r in r_group}
        if len(r_hashes) == 1:
            deduped_results.append(r_group[0])
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=f"Result {result_id!r} has {len(r_hashes)} distinct content hashes; all excluded",
                    conflicting_ids=[result_id],
                )
            )

    merged_evidence: list[Evidence] = []
    for ev_id, ev_group in sorted(evidence_groups.items(), key=lambda x: x[0]):
        ev_hashes = {content_hash(ev.model_dump(mode="json")) for ev in ev_group}
        if len(ev_hashes) == 1:
            merged_evidence.append(ev_group[0])
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=f"Evidence {ev_id!r} has {len(ev_hashes)} distinct content hashes; all excluded",
                    conflicting_ids=[ev_id],
                )
            )

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
                    detail=f"Proposal {prop_id!r} has {len(p_hashes)} distinct content hashes; all excluded",
                    conflicting_ids=[prop_id],
                )
            )

    # -- Evidence reference integrity ----------------------------------------
    # Proposals that reference evidence IDs which are not present in the
    # final merged_evidence set (e.g. because the evidence was excluded by
    # a content_mismatch conflict) must also be excluded.  This runs after
    # all evidence and proposal conflict resolution so that the available
    # evidence set reflects the surviving objects only.
    available_evidence_ids = {ev.evidence_id for ev in merged_evidence}
    final_proposals: list[ActionProposal] = []
    for p in merged_proposals:
        missing = sorted(set(p.evidence_ids) - available_evidence_ids)
        if missing:
            excluded_proposal_ids.add(p.proposal_id)
            conflicts.append(
                MergeConflict(
                    conflict_type="proposal_missing_evidence",
                    detail=(
                        f"Proposal {p.proposal_id!r} references "
                        f"missing evidence {missing!r}"
                    ),
                    conflicting_ids=[p.proposal_id, *missing],
                )
            )
            continue
        final_proposals.append(p)
    merged_proposals = final_proposals

    # -- Scrub excluded proposals from deduped_results ----------------------
    # Invalid proposals must not be reachable from any executable path in
    # MergedState.  We remove them from each surviving result's
    # action_proposals using model_copy(update=...) which does NOT re-run
    # validators, so the already-validated AgentResult stays intact.
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
