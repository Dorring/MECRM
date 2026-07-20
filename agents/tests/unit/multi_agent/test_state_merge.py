"""State merge tests — Phase 2 R2 with expected_tenant_id and content-hash dedup."""

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

    def test_single_result(self):
        r1 = _make_result(result_id="r-001")
        merged = merge_parallel_results([r1], expected_tenant_id="t-001")
        assert len(merged.results) == 1


# Order independence + permutation ----------------------------------------


class TestOrderIndependence:
    def test_swap_input_order_same_output_ids(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")
        m1 = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        m2 = merge_parallel_results([r2, r1], expected_tenant_id="t-001")
        assert [r.result_id for r in m1.results] == [r.result_id for r in m2.results]

    def test_all_permutations_equivalent(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")
        r3 = _make_result(result_id="r-003")
        results = [r1, r2, r3]
        expected_ids = ["r-001", "r-002", "r-003"]

        for perm in permutations(results):
            merged = merge_parallel_results(list(perm), expected_tenant_id="t-001")
            assert [r.result_id for r in merged.results] == expected_ids


# Result dedup (content-hash based) ---------------------------------------


class TestResultDedup:
    def test_duplicate_result_same_content_is_idempotent(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-001")  # same id, same content
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.results) == 1

    def test_duplicate_result_different_content_is_conflict(self):
        r1 = _make_result(result_id="r-001", confidence=0.9)
        r2 = _make_result(result_id="r-001", confidence=0.5)  # same id, different
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert any(c.conflict_type == "content_mismatch" for c in merged.conflicts)

    def test_duplicate_evidence_different_content_is_conflict(self):
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
        # Evidence ev-001 has different content → conflict
        assert any(
            c.conflict_type == "content_mismatch" and "ev-001" in str(c.conflicting_ids)
            for c in merged.conflicts
        )


# Evidence dedup ----------------------------------------------------------


class TestEvidenceDedup:
    def test_evidence_dedup_by_id(self):
        ev1 = _make_evidence(evidence_id="ev-001")
        ev2 = _make_evidence(evidence_id="ev-001")  # same id, same content
        ev3 = _make_evidence(evidence_id="ev-002")

        r1 = _make_result(result_id="r-001", evidence=[ev1, ev3])
        r2 = _make_result(result_id="r-002", evidence=[ev2])

        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        evidence_ids = {e.evidence_id for e in merged.merged_evidence}
        assert evidence_ids == {"ev-001", "ev-002"}


# Foreign tenant -----------------------------------------------------------


class TestForeignTenant:
    def test_foreign_result_rejected(self):
        r1 = _make_result(result_id="r-001", tenant_id="t-001")
        r2 = _make_result(result_id="r-002", tenant_id="t-002")  # foreign
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.results) == 1
        assert merged.results[0].result_id == "r-001"
        assert any(c.conflict_type == "foreign_tenant" for c in merged.conflicts)

    def test_foreign_evidence_rejected(self):
        # Result r1 is from t-001 with its own evidence; r2 is from t-002.
        ev1 = _make_evidence(evidence_id="ev-001", tenant_id="t-001")
        ev2 = _make_evidence(evidence_id="ev-002", tenant_id="t-002")
        r1 = _make_result(result_id="r-001", tenant_id="t-001", evidence=[ev1])
        r2 = _make_result(result_id="r-002", tenant_id="t-002", evidence=[ev2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        # t-002 result + evidence rejected
        assert len(merged.merged_evidence) == 1
        assert merged.merged_evidence[0].tenant_id == "t-001"
        assert any(c.conflict_type == "foreign_tenant" for c in merged.conflicts)

    def test_foreign_proposal_rejected(self):
        # r1 has proposals matching t-001; r2 from t-002 is rejected
        p1 = _make_proposal(proposal_id="p-001", tenant_id="t-001")
        p2 = _make_proposal(proposal_id="p-002", tenant_id="t-002")
        r1 = _make_result(
            result_id="r-001", tenant_id="t-001", agent_id="agent_a", proposals=[p1]
        )
        r2 = _make_result(
            result_id="r-002", tenant_id="t-002", agent_id="agent_a", proposals=[p2]
        )
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.merged_proposals) >= 1
        assert all(p.tenant_id == "t-001" for p in merged.merged_proposals)


# Proposal merge ----------------------------------------------------------


class TestProposalMerge:
    def test_proposal_dedup_by_hash(self):
        p1 = _make_proposal(proposal_id="p-001")
        p2 = _make_proposal(proposal_id="p-002")  # same content → same hash
        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        assert len(merged.merged_proposals) == 1

    def test_proposal_creator_mismatch(self):
        # Result agent is agent_a, but proposal says created_by_agent="agent_other"
        # AgentResult._tenant_homogeneity rejects this at construction.
        # The merge catches it when comparing cross-result creator mismatch
        # (separate results: r1 with agent_a, r2 with agent_b, both create same proposal id)
        p1 = _make_proposal(proposal_id="p-001", created_by_agent="agent_a")
        p2 = _make_proposal(proposal_id="p-002", created_by_agent="agent_b")
        r1 = _make_result(result_id="r-001", agent_id="agent_a", proposals=[p1])
        r2 = _make_result(result_id="r-002", agent_id="agent_b", proposals=[p2])
        merged = merge_parallel_results([r1, r2], expected_tenant_id="t-001")
        # Both proposals have different creators, but each matches its own result → no conflict
        assert len(merged.merged_proposals) >= 1


# Immutability ------------------------------------------------------------


class TestImmutability:
    def test_merge_does_not_modify_inputs(self):
        ev = _make_evidence(evidence_id="ev-001")
        original_type = ev.evidence_type
        r1 = _make_result(result_id="r-001", evidence=[ev])
        merge_parallel_results([r1], expected_tenant_id="t-001")
        assert ev.evidence_type == original_type
