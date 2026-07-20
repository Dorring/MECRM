"""Parallel agent result merge — Phase 2 R3.

Two-phase merge: group by ID, compare content hashes.
Same ID + single content hash → keep one.
Same ID + multiple content hashes → ALL excluded (content_mismatch).
No first/last preference.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from pydantic import Field

from multi_agent.contracts import (
    ActionProposal,
    AgentResult,
    Evidence,
    StrictContract,
)
from multi_agent.serialization import content_hash


# ---------------------------------------------------------------------------
# Merge output models
# ---------------------------------------------------------------------------


class MergeConflict(StrictContract):
    conflict_type: str
    detail: str = ""
    conflicting_ids: list[str] = []


class MergedState(StrictContract):
    results: list[AgentResult] = []
    merged_evidence: list[Evidence] = []
    merged_proposals: list[ActionProposal] = []
    conflicts: list[MergeConflict] = []
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
            if p.tenant_id == expected_tenant_id:
                proposal_groups[p.proposal_id].append(p)
            else:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=f"Proposal {p.proposal_id!r} tenant {p.tenant_id!r} != expected {expected_tenant_id!r}",
                        conflicting_ids=[p.proposal_id],
                    )
                )

    # -- Phase 2: resolve each group -----------------------------------------
    deduped_results: list[AgentResult] = []
    for result_id, group in sorted(result_groups.items(), key=lambda x: x[0]):
        hashes = {content_hash(r.model_dump(mode="json")) for r in group}
        if len(hashes) == 1:
            deduped_results.append(group[0])
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=f"Result {result_id!r} has {len(hashes)} distinct content hashes; all excluded",
                    conflicting_ids=[result_id],
                )
            )

    merged_evidence: list[Evidence] = []
    for ev_id, group in sorted(evidence_groups.items(), key=lambda x: x[0]):
        hashes = {content_hash(ev.model_dump(mode="json")) for ev in group}
        if len(hashes) == 1:
            merged_evidence.append(group[0])
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=f"Evidence {ev_id!r} has {len(hashes)} distinct content hashes; all excluded",
                    conflicting_ids=[ev_id],
                )
            )

    merged_proposals: list[ActionProposal] = []
    seen_hashes: set[str] = set()
    for prop_id, group in sorted(proposal_groups.items(), key=lambda x: x[0]):
        hashes = {p.proposal_hash for p in group}
        if len(hashes) == 1:
            p = group[0]
            if p.proposal_hash not in seen_hashes:
                seen_hashes.add(p.proposal_hash)
                merged_proposals.append(p)
        else:
            conflicts.append(
                MergeConflict(
                    conflict_type="content_mismatch",
                    detail=f"Proposal {prop_id!r} has {len(hashes)} distinct content hashes; all excluded",
                    conflicting_ids=[prop_id],
                )
            )

    return MergedState(
        results=deduped_results,
        merged_evidence=merged_evidence,
        merged_proposals=merged_proposals,
        conflicts=conflicts,
        merged_at=datetime.now(timezone.utc),
    )
