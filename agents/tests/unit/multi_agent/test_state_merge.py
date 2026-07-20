"""State merge tests — two-phase order-independent merge."""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import permutations

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentResult,
    Evidence,
    EvidenceType,
    TokenUsage,
)
from multi_agent.state import merge_parallel_results

# Helpers ----------------------------------------------------------------

# Fixed timestamp so that two objects with the same logical content
# produce identical content hashes regardless of wall-clock drift.
_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return _FIXED_TS


def _make_result(
    result_id: str = "r-001",
    task_id: str = "task-001",
    agent_id: str = "agent_a",
    tenant_id: str = "t-001",
    status: str = "completed",
    evidence: list[Evidence] | None = None,
    proposals: list[ActionProposal] | None = None,
    **overrides,
) -> AgentResult:
    defaults: dict = dict(
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        tenant_id=tenant_id,
        status=status,
        confidence=0.95,
        duration_ms=100.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        token_usage=TokenUsage(),
        completed_at=_FIXED_TS,
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


def _make_evidence(evidence_id: str = "ev-001", tenant_id: str = "t-001") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TOOL_RESULT,
        tenant_id=tenant_id,
        source_agent="test_agent",
        created_at=_FIXED_TS,
    )


def _make_proposal(
    proposal_id: str = "p-001", tenant_id: str = "t-001", **overrides
) -> ActionProposal:
    fields: dict = dict(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent="agent_a",
        action_type="create",
        target_entity="ticket",
        priority="medium",
        risk_level=ActionRiskLevel.MEDIUM,
        evidence_ids=[],
        requires_approval=True,
        idempotency_key=f"ik-{proposal_id}",
    )
    fields.update(overrides)
    return ActionProposal.create(**fields)


# Basic merge -------------------------------------------------------------


class TestBasicMerge:
    def test_two_results_merge(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.results) == 2

    def test_empty_list(self):
        merged = merge_parallel_results([], expected_tenant_id="t-001")
        assert merged.results == []


# Order independence ------------------------------------------------------


class TestOrderIndependence:
    def test_all_permutations_equivalent(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")
        r3 = _make_result(result_id="r-003")
        results = [r1, r2, r3]
        expected_ids = ["r-001", "r-002", "r-003"]

        for perm in permutations(results):
            merged = merge_parallel_results(list(perm), expected_tenant_id="t-001")
            assert [r.result_id for r in merged.results] == expected_ids


# Result dedup ------------------------------------------------------------


class TestResultDedup:
    def test_duplicate_result_same_content_idempotent(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-001")  # same id, same content
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.results) == 1

    def test_duplicate_result_different_content_conflict(self):
        r1 = _make_result(result_id="r-001", confidence=0.9)
        r2 = _make_result(result_id="r-001", confidence=0.5)  # same id, diff content
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        # BOTH excluded
        assert len(merged.results) == 0
        assert any(c.conflict_type == "content_mismatch" for c in merged.conflicts)

    def test_conflicting_result_excluded_in_both_orders(self):
        r1 = _make_result(result_id="r-001", confidence=0.9)
        r2 = _make_result(result_id="r-001", confidence=0.5)
        m1 = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        m2 = merge_parallel_results([r2, r1], expected_tenant_id="t-001")
        # Both orders exclude the conflicting result
        assert len(m1.results) == 0
        assert len(m2.results) == 0

    def test_conflicting_merge_all_permutations_equivalent(self):
        """With same-ID results of different content, all permutations exclude all."""
        # Create 2 results with same ID but different content
        # Only need 2 since the third has a different ID
        r_a = _make_result(result_id="r-001", confidence=0.9)
        r_b = _make_result(result_id="r-001", confidence=0.5)
        r_ok = _make_result(result_id="r-002")

        for perm in permutations([r_a, r_b, r_ok]):
            merged = merge_parallel_results(list(perm), expected_tenant_id="t-001")
            # r-001 excluded (conflict), r-002 kept
            assert [r.result_id for r in merged.results] == ["r-002"]
            assert any(c.conflict_type == "content_mismatch" for c in merged.conflicts)


# Evidence dedup ----------------------------------------------------------


class TestEvidenceDedup:
    def test_evidence_dedup_same_content(self):
        ev1 = _make_evidence(evidence_id="ev-001")
        ev2 = _make_evidence(evidence_id="ev-001")  # same
        ev3 = _make_evidence(evidence_id="ev-002")
        r1 = _make_result(result_id="r-001", evidence=[ev1, ev3])
        r2 = _make_result(result_id="r-002", evidence=[ev2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert {e.evidence_id for e in merged.merged_evidence} == {"ev-001", "ev-002"}

    def test_duplicate_evidence_different_content_conflict(self):
        ev1 = _make_evidence(evidence_id="ev-001")
        ev2 = Evidence(
            evidence_id="ev-001",
            evidence_type=EvidenceType.CUSTOMER,
            tenant_id="t-001",
            source_agent="a",
            created_at=_utc_now(),
        )
        r1 = _make_result(result_id="r-001", evidence=[ev1])
        r2 = _make_result(result_id="r-002", evidence=[ev2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        # Both excluded
        assert not any(e.evidence_id == "ev-001" for e in merged.merged_evidence)
        assert any(c.conflict_type == "content_mismatch" for c in merged.conflicts)

    def test_conflicting_evidence_excluded_in_both_orders(self):
        ev1 = _make_evidence(evidence_id="ev-001")
        ev2 = Evidence(
            evidence_id="ev-001",
            evidence_type=EvidenceType.CUSTOMER,
            tenant_id="t-001",
            source_agent="a",
            created_at=_utc_now(),
        )
        r1 = _make_result(result_id="r-001", evidence=[ev1])
        r2 = _make_result(result_id="r-002", evidence=[ev2])
        m1 = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        m2 = merge_parallel_results([r2, r1], expected_tenant_id="t-001")
        assert not any(e.evidence_id == "ev-001" for e in m1.merged_evidence)
        assert not any(e.evidence_id == "ev-001" for e in m2.merged_evidence)


# Proposal merge ----------------------------------------------------------


class TestProposalMerge:
    def test_proposal_dedup_by_hash(self):
        p1 = _make_proposal(proposal_id="p-001")
        p2 = _make_proposal(proposal_id="p-002")  # same content → same hash
        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.merged_proposals) == 1

    def test_conflicting_proposal_excluded_in_both_orders(self):
        p1 = _make_proposal(proposal_id="p-001", payload={"a": 1})
        p2 = _make_proposal(proposal_id="p-001", payload={"b": 2})
        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])
        m1 = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        m2 = merge_parallel_results([r2, r1], expected_tenant_id="t-001")
        assert all(p.proposal_id != "p-001" for p in m1.merged_proposals)
        assert all(p.proposal_id != "p-001" for p in m2.merged_proposals)
        assert any(c.conflict_type == "content_mismatch" for c in m1.conflicts)


# Foreign tenant -----------------------------------------------------------


class TestForeignTenant:
    def test_foreign_result_rejected(self):
        r1 = _make_result(result_id="r-001", tenant_id="t-001")
        r2 = _make_result(result_id="r-002", tenant_id="t-002")
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.results) == 1
        assert merged.results[0].result_id == "r-001"

    def test_foreign_evidence_rejected(self):
        ev1 = _make_evidence(evidence_id="ev-001", tenant_id="t-001")
        ev2 = _make_evidence(evidence_id="ev-002", tenant_id="t-002")
        r1 = _make_result(result_id="r-001", tenant_id="t-001", evidence=[ev1])
        r2 = _make_result(result_id="r-002", tenant_id="t-002", evidence=[ev2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.merged_evidence) == 1
        assert merged.merged_evidence[0].tenant_id == "t-001"


# Immutability ------------------------------------------------------------


class TestImmutability:
    def test_merge_does_not_modify_inputs(self):
        ev = _make_evidence(evidence_id="ev-001")
        original_type = ev.evidence_type
        r1 = _make_result(result_id="r-001", evidence=[ev])
        merge_parallel_results([r1], expected_tenant_id="t-001")
        assert ev.evidence_type == original_type


# ============================================================================
# R4: Merge re-validates proposal integrity
# ============================================================================


class TestMergeProposalIntegrity:
    def test_mutated_proposal_after_result_rejected_by_merge(self):
        """Proposal mutated after AgentResult construction is caught by merge."""
        p = _make_proposal(proposal_id="p-mut", payload={"amount": 100})
        r = _make_result(result_id="r-001", proposals=[p])
        p.verify_integrity()  # valid now
        r.action_proposals[0].payload["amount"] = 999999  # type: ignore[index]
        merged = merge_parallel_results([r], expected_tenant_id="t-001")
        assert len(merged.merged_proposals) == 0
        assert any(
            c.conflict_type == "proposal_integrity_failure" for c in merged.conflicts
        )

    def test_integrity_failure_proposal_excluded(self):
        """Proposal failing integrity is excluded from merge output."""
        p = _make_proposal(proposal_id="p-bad", payload={"amount": 100})
        r = _make_result(result_id="r-001", proposals=[p])
        r.action_proposals[0].payload["amount"] = 999999  # type: ignore[index]
        merged = merge_parallel_results([r], expected_tenant_id="t-001")
        assert len(merged.merged_proposals) == 0

    def test_good_proposal_passes_merge(self):
        """Intact proposal passes merge normally."""
        p = _make_proposal(proposal_id="p-good")
        r = _make_result(result_id="r-001", proposals=[p])
        merged = merge_parallel_results([r], expected_tenant_id="t-001")
        assert len(merged.merged_proposals) == 1


# ============================================================================
# R7: Merge excludes proposals that reference missing evidence
# ============================================================================


class TestMergeEvidenceReferenceIntegrity:
    """Merge must verify that every proposal's evidence_ids reference
    evidence that actually survives into merged_evidence.  Proposals with
    dangling references are excluded and recorded as
    ``proposal_missing_evidence`` conflicts."""

    def test_merge_excludes_proposal_after_evidence_removed(self):
        """Construct a valid AgentResult (proposal + evidence), then clear
        the evidence list AFTER construction.  Merge must exclude the
        proposal because its evidence_id is no longer present."""
        ev = _make_evidence(evidence_id="ev-1")
        p = _make_proposal(
            proposal_id="p-1",
            evidence_ids=["ev-1"],
            risk_level=ActionRiskLevel.HIGH,
        )
        r = _make_result(result_id="r-001", evidence=[ev], proposals=[p])
        # Mutate: remove evidence after construction
        r.evidence.clear()

        merged = merge_parallel_results([r], expected_tenant_id="t-001")

        assert len(merged.merged_proposals) == 0
        assert any(
            c.conflict_type == "proposal_missing_evidence" for c in merged.conflicts
        )

    def test_merge_records_proposal_missing_evidence(self):
        """The conflict record must include both the proposal id and the
        missing evidence id in ``conflicting_ids``."""
        ev = _make_evidence(evidence_id="ev-missing-target")
        p = _make_proposal(
            proposal_id="p-orphan",
            evidence_ids=["ev-missing-target"],
            risk_level=ActionRiskLevel.HIGH,
        )
        r = _make_result(result_id="r-001", evidence=[ev], proposals=[p])
        r.evidence.clear()

        merged = merge_parallel_results([r], expected_tenant_id="t-001")

        conflict = next(
            c
            for c in merged.conflicts
            if c.conflict_type == "proposal_missing_evidence"
        )
        assert "p-orphan" in conflict.conflicting_ids
        assert "ev-missing-target" in conflict.conflicting_ids
        assert "ev-missing-target" in conflict.detail

    def test_merge_excludes_proposal_when_evidence_conflicts(self):
        """When evidence is excluded due to a content_mismatch conflict,
        any proposal referencing that evidence id must also be excluded."""
        ev_a = _make_evidence(evidence_id="ev-conflict")
        ev_b = Evidence(
            evidence_id="ev-conflict",
            evidence_type=EvidenceType.CUSTOMER,  # different content
            tenant_id="t-001",
            source_agent="a",
            created_at=_utc_now(),
        )
        p = _make_proposal(
            proposal_id="p-dep",
            evidence_ids=["ev-conflict"],
            risk_level=ActionRiskLevel.HIGH,
        )
        r1 = _make_result(result_id="r-001", evidence=[ev_a], proposals=[p])
        r2 = _make_result(result_id="r-002", evidence=[ev_b])

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # Evidence ev-conflict excluded due to content_mismatch
        assert not any(e.evidence_id == "ev-conflict" for e in merged.merged_evidence)
        assert any(
            c.conflict_type == "content_mismatch" and "ev-conflict" in c.conflicting_ids
            for c in merged.conflicts
        )
        # Proposal p-dep excluded because its evidence was excluded
        assert not any(prop.proposal_id == "p-dep" for prop in merged.merged_proposals)
        assert any(
            c.conflict_type == "proposal_missing_evidence"
            and "p-dep" in c.conflicting_ids
            for c in merged.conflicts
        )


# ============================================================================
# R8: Excluded proposals must not survive in results[*].action_proposals
# ============================================================================


class TestExcludedProposalScrubbedFromResults:
    """An invalid proposal must not be reachable from any executable path
    in MergedState — neither ``merged_proposals`` nor
    ``results[*].action_proposals``."""

    def test_integrity_failure_removed_from_result_actions(self):
        """A proposal whose hash is tampered AFTER construction is caught
        by ``verify_integrity()`` at merge time and removed from both
        ``merged_proposals`` and the owning result's ``action_proposals``."""
        p = _make_proposal(proposal_id="p-tampered")
        r = _make_result(result_id="r-001", proposals=[p])

        # Tamper the hash after construction — merge must detect this
        object.__setattr__(p, "proposal_hash", "deadbeef")

        merged = merge_parallel_results([r], expected_tenant_id="t-001")

        assert len(merged.merged_proposals) == 0
        assert any(
            c.conflict_type == "proposal_integrity_failure"
            and "p-tampered" in c.conflicting_ids
            for c in merged.conflicts
        )
        # Critical: the bad proposal must NOT survive inside results
        for result in merged.results:
            assert not any(
                prop.proposal_id == "p-tampered" for prop in result.action_proposals
            )

    def test_conflicting_proposal_removed_from_result_actions(self):
        """When the same proposal_id appears with different content hashes,
        ALL copies are excluded from both ``merged_proposals`` and every
        result's ``action_proposals``."""
        p1 = _make_proposal(
            proposal_id="p-conflict",
            payload={"amount": 100},
        )
        p2 = _make_proposal(
            proposal_id="p-conflict",
            payload={"amount": 999},
        )
        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        assert not any(
            prop.proposal_id == "p-conflict" for prop in merged.merged_proposals
        )
        assert any(
            c.conflict_type == "content_mismatch" and "p-conflict" in c.conflicting_ids
            for c in merged.conflicts
        )
        for result in merged.results:
            assert not any(
                prop.proposal_id == "p-conflict" for prop in result.action_proposals
            )

    def test_missing_evidence_proposal_removed_from_result_actions(self):
        """A proposal referencing missing evidence is excluded from both
        ``merged_proposals`` and its owning result's ``action_proposals``."""
        ev = _make_evidence(evidence_id="ev-gone")
        p = _make_proposal(
            proposal_id="p-orphan",
            evidence_ids=["ev-gone"],
            risk_level=ActionRiskLevel.HIGH,
        )
        r = _make_result(result_id="r-001", evidence=[ev], proposals=[p])
        # Remove the evidence AFTER construction so the proposal dangles
        r.evidence.clear()

        merged = merge_parallel_results([r], expected_tenant_id="t-001")

        assert not any(
            prop.proposal_id == "p-orphan" for prop in merged.merged_proposals
        )
        assert any(
            c.conflict_type == "proposal_missing_evidence"
            and "p-orphan" in c.conflicting_ids
            for c in merged.conflicts
        )
        for result in merged.results:
            assert not any(
                prop.proposal_id == "p-orphan" for prop in result.action_proposals
            )

    def test_no_excluded_proposal_reachable_from_merged_state(self):
        """End-to-end: construct a result with one good and one bad proposal,
        then assert the bad one is unreachable everywhere in MergedState."""
        good_p = _make_proposal(proposal_id="p-good")
        bad_p = _make_proposal(
            proposal_id="p-bad",
            payload={"x": 1},
        )
        r = _make_result(result_id="r-001", proposals=[good_p, bad_p])
        # Tamper the bad proposal's hash
        object.__setattr__(bad_p, "proposal_hash", "invalid")

        merged = merge_parallel_results([r], expected_tenant_id="t-001")

        # Good survives in both places
        assert any(prop.proposal_id == "p-good" for prop in merged.merged_proposals)
        # Bad is gone from both places
        assert not any(prop.proposal_id == "p-bad" for prop in merged.merged_proposals)
        for result in merged.results:
            ids = {prop.proposal_id for prop in result.action_proposals}
            assert "p-good" in ids
            assert "p-bad" not in ids


# ============================================================================
# R9: Proposal-ID-level Fail-Closed — any bad copy excludes ALL copies
# ============================================================================


class TestProposalIdFailClosed:
    """If ANY copy of a proposal_id is judged invalid, ALL copies of that
    id must be removed from merged_proposals and results[*].action_proposals."""

    def test_valid_and_tampered_same_proposal_id_excludes_all(self):
        """One valid + one tampered proposal with the SAME proposal_id:
        the tampered copy adds the id to excluded_proposal_ids, and the
        valid copy must also be excluded (Fail-Closed)."""
        p_good = _make_proposal(proposal_id="p-same")
        p_bad = _make_proposal(proposal_id="p-same")

        r1 = _make_result(result_id="r-001", proposals=[p_good])
        r2 = _make_result(result_id="r-002", proposals=[p_bad])
        # Tamper the bad copy's hash AFTER both result and proposal are
        # constructed, so AgentResult validation doesn't catch it — merge
        # must detect it via verify_integrity().
        object.__setattr__(p_bad, "proposal_hash", "deadbeef")

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # No copy of p-same should survive anywhere
        assert not any(p.proposal_id == "p-same" for p in merged.merged_proposals)
        for result in merged.results:
            assert not any(p.proposal_id == "p-same" for p in result.action_proposals)
        # Integrity failure was recorded
        assert any(
            c.conflict_type == "proposal_integrity_failure"
            and "p-same" in c.conflicting_ids
            for c in merged.conflicts
        )

    def test_foreign_and_local_same_proposal_id_excludes_all(self):
        """One foreign-tenant proposal + one local proposal with the SAME
        proposal_id, both inside LOCAL results: the foreign proposal adds
        the id to excluded_proposal_ids, and the local copy must also be
        excluded (Fail-Closed).

        Because AgentResult validates tenant homogeneity at construction,
        we tamper the proposal's tenant_id AFTER construction so the
        result is still buildable."""
        p_foreign = _make_proposal(proposal_id="p-same", tenant_id="t-001")
        p_local = _make_proposal(proposal_id="p-same", tenant_id="t-001")

        r1 = _make_result(
            result_id="r-001",
            tenant_id="t-001",
            proposals=[p_foreign],
        )
        r2 = _make_result(
            result_id="r-002",
            tenant_id="t-001",
            proposals=[p_local],
        )
        # Tamper p_foreign's tenant_id AFTER construction so the result
        # was buildable, but merge will catch the foreign proposal.
        object.__setattr__(p_foreign, "tenant_id", "t-other")

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # p-same must not survive anywhere — Fail-Closed
        assert not any(p.proposal_id == "p-same" for p in merged.merged_proposals)
        for result in merged.results:
            assert not any(p.proposal_id == "p-same" for p in result.action_proposals)

    def test_excluded_proposal_id_absent_from_all_merged_views(self):
        """End-to-end: an excluded proposal_id must not appear in
        merged_proposals, results[*].action_proposals, or any nested
        structure reachable from MergedState."""
        p1 = _make_proposal(proposal_id="p-dup", payload={"v": 1})
        p2 = _make_proposal(proposal_id="p-dup", payload={"v": 2})
        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # p-dup had content_mismatch → excluded
        assert not any(p.proposal_id == "p-dup" for p in merged.merged_proposals)
        for result in merged.results:
            assert not any(p.proposal_id == "p-dup" for p in result.action_proposals)


# ============================================================================
# R9: Conflicting Result's children must not survive
# ============================================================================


class TestConflictingResultChildrenExcluded:
    """When a Result is excluded due to content_mismatch, its Evidence
    and Proposals must NOT participate in the merge."""

    def test_conflicting_result_proposals_are_excluded(self):
        """Two results with same result_id but different content:
        both are excluded, and their proposals must not appear in
        merged_proposals."""
        p_a = _make_proposal(proposal_id="p-from-a")
        p_b = _make_proposal(proposal_id="p-from-b")
        r1 = _make_result(
            result_id="r-conflict",
            proposals=[p_a],
            confidence=0.9,
        )
        r2 = _make_result(
            result_id="r-conflict",
            proposals=[p_b],
            confidence=0.5,
        )

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # Results excluded
        assert len(merged.results) == 0
        assert any(
            c.conflict_type == "content_mismatch" and "r-conflict" in c.conflicting_ids
            for c in merged.conflicts
        )
        # Proposals from the conflicting results must NOT survive
        proposal_ids = {p.proposal_id for p in merged.merged_proposals}
        assert "p-from-a" not in proposal_ids
        assert "p-from-b" not in proposal_ids

    def test_conflicting_result_evidence_is_excluded(self):
        """Two results with same result_id but different content:
        their evidence must not appear in merged_evidence."""
        ev_a = _make_evidence(evidence_id="ev-from-a")
        ev_b = _make_evidence(evidence_id="ev-from-b")
        r1 = _make_result(
            result_id="r-conflict",
            evidence=[ev_a],
            confidence=0.9,
        )
        r2 = _make_result(
            result_id="r-conflict",
            evidence=[ev_b],
            confidence=0.5,
        )

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        assert len(merged.results) == 0
        evidence_ids = {e.evidence_id for e in merged.merged_evidence}
        assert "ev-from-a" not in evidence_ids
        assert "ev-from-b" not in evidence_ids

    def test_conflicting_result_children_unreachable_from_merged_state(self):
        """End-to-end: conflicting result's children must not be reachable
        from any path in MergedState."""
        ev = _make_evidence(evidence_id="ev-orphan")
        p = _make_proposal(
            proposal_id="p-orphan",
            evidence_ids=["ev-orphan"],
            risk_level=ActionRiskLevel.HIGH,
        )
        r1 = _make_result(
            result_id="r-conflict",
            evidence=[ev],
            proposals=[p],
            confidence=0.9,
        )
        r2 = _make_result(
            result_id="r-conflict",
            confidence=0.5,
        )

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # Nothing from r1 should survive
        assert len(merged.results) == 0
        assert len(merged.merged_evidence) == 0
        assert len(merged.merged_proposals) == 0

    def test_duplicate_identical_result_children_merged_once(self):
        """Two identical results (same id + same content): result is
        deduped to 1, and its children are merged once (not duplicated)."""
        ev = _make_evidence(evidence_id="ev-shared")
        p = _make_proposal(proposal_id="p-shared")
        r1 = _make_result(
            result_id="r-dup",
            evidence=[ev],
            proposals=[p],
        )
        r2 = _make_result(
            result_id="r-dup",
            evidence=[ev],
            proposals=[p],
        )

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")

        # Result deduped to 1
        assert len(merged.results) == 1
        # Evidence merged once (not duplicated)
        assert len(merged.merged_evidence) == 1
        assert merged.merged_evidence[0].evidence_id == "ev-shared"
        # Proposal merged once
        assert len(merged.merged_proposals) == 1
        assert merged.merged_proposals[0].proposal_id == "p-shared"
