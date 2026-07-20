"""Parallel agent result merge — Phase 2 R2.

Merge is order-independent: inputs are first canonicalised (by result_id sort)
and conflicts are detected via content hash rather than silently keeping the
first occurrence.
"""

from __future__ import annotations

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
    """Merge N parallel AgentResults into a single MergedState.

    All Results, their Evidence, and their Proposals must belong to
    *expected_tenant_id*.  Foreign data is recorded as a conflict and
    excluded from the merged output.
    """
    if not results:
        return MergedState(
            merged_at=datetime.now(timezone.utc),
        )

    sorted_results = sorted(results, key=lambda r: r.result_id)
    conflicts: list[MergeConflict] = []
    deduped_results: list[AgentResult] = []
    merged_evidence: list[Evidence] = []
    merged_proposals: list[ActionProposal] = []

    seen_result_ids: dict[str, str] = {}  # result_id → content_hash
    seen_evidence_ids: dict[str, str] = {}  # evidence_id → content_hash
    seen_proposal_ids: dict[str, str] = {}  # proposal_id → content_hash
    seen_proposal_hashes: set[str] = set()

    for result in sorted_results:
        # -- Tenant checks ---------------------------------------------------
        if result.tenant_id != expected_tenant_id:
            conflicts.append(
                MergeConflict(
                    conflict_type="foreign_tenant",
                    detail=f"Result {result.result_id!r} tenant {result.tenant_id!r} != expected {expected_tenant_id!r}",
                    conflicting_ids=[result.result_id],
                )
            )
            continue

        # -- Result dedup ---------------------------------------------------
        rhash = content_hash(result.model_dump(mode="json"))
        if result.result_id in seen_result_ids:
            existing_hash = seen_result_ids[result.result_id]
            if rhash == existing_hash:
                # Same ID, same content → idempotent duplicate, skip
                continue
            else:
                conflicts.append(
                    MergeConflict(
                        conflict_type="content_mismatch",
                        detail=f"Result {result.result_id!r} has different content",
                        conflicting_ids=[result.result_id],
                    )
                )
                continue
        seen_result_ids[result.result_id] = rhash
        deduped_results.append(result)

        # -- Evidence merge -------------------------------------------------
        for ev in result.evidence:
            if ev.tenant_id != expected_tenant_id:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=f"Evidence {ev.evidence_id!r} tenant {ev.tenant_id!r} != expected {expected_tenant_id!r}",
                        conflicting_ids=[ev.evidence_id],
                    )
                )
                continue

            ehash = content_hash(ev.model_dump(mode="json"))
            if ev.evidence_id in seen_evidence_ids:
                existing = seen_evidence_ids[ev.evidence_id]
                if ehash != existing:
                    conflicts.append(
                        MergeConflict(
                            conflict_type="content_mismatch",
                            detail=f"Evidence {ev.evidence_id!r} has conflicting content",
                            conflicting_ids=[ev.evidence_id],
                        )
                    )
                # Same content or different: skip duplicate evidence_id
                continue
            seen_evidence_ids[ev.evidence_id] = ehash
            merged_evidence.append(ev)

        # -- Proposal merge -------------------------------------------------
        for proposal in result.action_proposals:
            if proposal.tenant_id != expected_tenant_id:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=f"Proposal {proposal.proposal_id!r} tenant {proposal.tenant_id!r} != expected {expected_tenant_id!r}",
                        conflicting_ids=[proposal.proposal_id],
                    )
                )
                continue

            if proposal.created_by_agent != result.agent_id:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_agent",
                        detail=f"Proposal {proposal.proposal_id!r} created_by {proposal.created_by_agent!r} != result agent {result.agent_id!r}",
                        conflicting_ids=[proposal.proposal_id, result.result_id],
                    )
                )
                continue

            phash = content_hash(proposal.model_dump(mode="json"))
            if proposal.proposal_id in seen_proposal_ids:
                existing = seen_proposal_ids[proposal.proposal_id]
                if phash != existing:
                    conflicts.append(
                        MergeConflict(
                            conflict_type="content_mismatch",
                            detail=f"Proposal {proposal.proposal_id!r} has conflicting content",
                            conflicting_ids=[proposal.proposal_id],
                        )
                    )
                continue

            if proposal.proposal_hash in seen_proposal_hashes:
                continue
            seen_proposal_ids[proposal.proposal_id] = phash
            seen_proposal_hashes.add(proposal.proposal_hash)
            merged_proposals.append(proposal)

    return MergedState(
        results=deduped_results,
        merged_evidence=merged_evidence,
        merged_proposals=merged_proposals,
        conflicts=conflicts,
        merged_at=datetime.now(timezone.utc),
    )
