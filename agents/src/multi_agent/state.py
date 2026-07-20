"""Parallel agent result merge.

When a Supervisor fans out to N specialists, their results must be merged
into a single coherent view.  The merge rules guarantee:

- **Order independence**: sorting by result_id before merge yields the same
  output regardless of input order.
- **Foreign tenant rejection**: evidence with a tenant_id that differs from
  the enclosing AgentResult's tenant_id is rejected.
- **Deduplication by ID**: duplicate result_ids, evidence_ids, and
  proposal_hashes are collapsed.
- **Immutability**: input objects are never mutated.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from multi_agent.contracts import ActionProposal, AgentResult, Evidence


# ---------------------------------------------------------------------------
# Merge output models
# ---------------------------------------------------------------------------


class MergeConflict(BaseModel):
    """Records a conflict detected during merge."""

    conflict_type: str
    detail: str
    conflicting_ids: list[str] = Field(default_factory=list)


class MergedState(BaseModel):
    """The output of merging N parallel AgentResults."""

    results: list[AgentResult] = Field(default_factory=list)
    merged_evidence: list[Evidence] = Field(default_factory=list)
    merged_proposals: list[ActionProposal] = Field(default_factory=list)
    conflicts: list[MergeConflict] = Field(default_factory=list)
    merged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Merge function
# ---------------------------------------------------------------------------


def merge_parallel_results(results: list[AgentResult]) -> MergedState:
    """Merge parallel AgentResults into a single MergedState.

    The merge is **order-independent**: inputs are sorted by ``result_id``
    before processing so the caller need not guarantee any particular input
    order.
    """
    if not results:
        return MergedState(
            results=[],
            merged_evidence=[],
            merged_proposals=[],
            conflicts=[],
            merged_at=datetime.now(timezone.utc),
        )

    # Sort for deterministic, order-independent output
    sorted_results = sorted(results, key=lambda r: r.result_id)
    tenant_id = sorted_results[0].tenant_id

    conflicts: list[MergeConflict] = []
    seen_result_ids: set[str] = set()
    seen_evidence_ids: set[str] = set()
    seen_proposal_hashes: set[str] = set()
    seen_proposal_ids: dict[str, ActionProposal] = {}

    deduped_results: list[AgentResult] = []
    merged_evidence: list[Evidence] = []
    merged_proposals: list[ActionProposal] = []

    for result in sorted_results:
        # -- Result deduplication ---------------------------------------------
        if result.result_id in seen_result_ids:
            conflicts.append(
                MergeConflict(
                    conflict_type="duplicate_result",
                    detail=f"Result {result.result_id!r} appears more than once; keeping first",
                    conflicting_ids=[result.result_id],
                )
            )
            continue
        seen_result_ids.add(result.result_id)
        deduped_results.append(result)

        # -- Evidence merge ---------------------------------------------------
        for evidence in result.evidence:
            # Foreign tenant check
            if evidence.tenant_id != tenant_id:
                conflicts.append(
                    MergeConflict(
                        conflict_type="foreign_tenant",
                        detail=(
                            f"Evidence {evidence.evidence_id!r} has tenant_id "
                            f"{evidence.tenant_id!r} but result {result.result_id!r} "
                            f"belongs to tenant {tenant_id!r}"
                        ),
                        conflicting_ids=[evidence.evidence_id, result.result_id],
                    )
                )
                # Reject the foreign evidence — do NOT add to merged_evidence
                continue

            # Dedup by evidence_id
            if evidence.evidence_id in seen_evidence_ids:
                continue
            seen_evidence_ids.add(evidence.evidence_id)
            merged_evidence.append(evidence)

        # -- Proposal merge ---------------------------------------------------
        for proposal in result.action_proposals:
            # Dedup by proposal_hash
            if proposal.proposal_hash in seen_proposal_hashes:
                continue
            seen_proposal_hashes.add(proposal.proposal_hash)

            # Check for same proposal_id with different content (conflict)
            if proposal.proposal_id in seen_proposal_ids:
                existing = seen_proposal_ids[proposal.proposal_id]
                if existing.proposal_hash != proposal.proposal_hash:
                    conflicts.append(
                        MergeConflict(
                            conflict_type="content_mismatch",
                            detail=(
                                f"Proposal {proposal.proposal_id!r} appears with "
                                f"different hashes: {existing.proposal_hash!r} vs "
                                f"{proposal.proposal_hash!r}"
                            ),
                            conflicting_ids=[
                                proposal.proposal_id,
                            ],
                        )
                    )
                    continue
            seen_proposal_ids[proposal.proposal_id] = proposal
            merged_proposals.append(proposal)

    return MergedState(
        results=deduped_results,
        merged_evidence=merged_evidence,
        merged_proposals=merged_proposals,
        conflicts=conflicts,
        merged_at=datetime.now(timezone.utc),
    )
