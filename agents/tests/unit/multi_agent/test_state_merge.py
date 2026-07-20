"""State merge tests — Phase 2 R3 with two-phase order-independent merge."""

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
        tenant_id=tenant_id,
        status=status,
        confidence=0.95,
        duration_ms=100.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        token_usage=TokenUsage(),
        completed_at=_utc_now(),
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


def _make_evidence(evidence_id: str = "ev-001", tenant_id: str = "t-001") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TOOL_RESULT,
        tenant_id=tenant_id,
        source_agent="test_agent",
        created_at=_utc_now(),
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
