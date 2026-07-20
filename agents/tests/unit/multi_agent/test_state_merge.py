"""State merge tests — parallel AgentResult merging.

All tests run under AI_MODE=deterministic; no Ollama, no API keys.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from multi_agent.contracts import (
    ActionProposal,
    AgentResult,
    Evidence,
    TokenUsage,
    _compute_proposal_hash,
)
from multi_agent.state import merge_parallel_results

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_evidence(
    evidence_id: str = "ev-001",
    tenant_id: str = "t-001",
    evidence_type: str = "opa_policy",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=tenant_id,
        source_agent="test_agent",
        created_at=_utc_now(),
    )


def _make_proposal(
    proposal_id: str = "p-001",
    tenant_id: str = "t-001",
    priority: str = "medium",
    **overrides,
) -> ActionProposal:
    now = _utc_now()
    hash_kwargs = dict(
        tenant_id=tenant_id,
        created_by_agent="agent_a",
        action_type="create",
        target_entity="ticket",
        target_id=None,
        payload={"x": 1},
        priority=priority,
        justification="test",
        evidence_ids=[],
        requires_approval=True,
        idempotency_key=f"ik-{proposal_id}",
    )
    # Update hash_kwargs with overrides that affect the hash
    for k in hash_kwargs:
        if k in overrides:
            hash_kwargs[k] = overrides[k]

    fields: dict = dict(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent="agent_a",
        action_type="create",
        target_entity="ticket",
        payload={"x": 1},
        priority=priority,
        justification="test",
        idempotency_key=f"ik-{proposal_id}",
        created_at=now,
    )
    fields.update(overrides)
    fields["proposal_hash"] = _compute_proposal_hash(**hash_kwargs)  # type: ignore[arg-type]
    return ActionProposal(**fields)


# ---------------------------------------------------------------------------
# Basic merge
# ---------------------------------------------------------------------------


class TestBasicMerge:
    def test_two_results_merge(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")

        merged = merge_parallel_results([r1, r2])
        assert len(merged.results) == 2

    def test_single_result(self):
        r1 = _make_result(result_id="r-001")
        merged = merge_parallel_results([r1])
        assert len(merged.results) == 1
        assert merged.results[0].result_id == "r-001"

    def test_empty_list(self):
        merged = merge_parallel_results([])
        assert merged.results == []
        assert merged.merged_evidence == []
        assert merged.merged_proposals == []
        assert merged.conflicts == []


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------


class TestOrderIndependence:
    def test_swap_input_order_same_output(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")

        m1 = merge_parallel_results([r1, r2])
        m2 = merge_parallel_results([r2, r1])

        # Results must be in sorted order in both cases
        assert [r.result_id for r in m1.results] == ["r-001", "r-002"]
        assert [r.result_id for r in m2.results] == ["r-001", "r-002"]

    def test_triple_any_order(self):
        r1 = _make_result(result_id="r-001")
        r2 = _make_result(result_id="r-002")
        r3 = _make_result(result_id="r-003")

        ids = ["r-001", "r-002", "r-003"]
        for ordering in ([r1, r2, r3], [r3, r2, r1], [r2, r1, r3]):
            merged = merge_parallel_results(list(ordering))
            assert [r.result_id for r in merged.results] == ids


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_result_id_keeps_first(self):
        r1 = _make_result(result_id="r-001", agent_id="agent_a")
        r2 = _make_result(result_id="r-001", agent_id="agent_b")  # same id, different agent

        merged = merge_parallel_results([r1, r2])
        assert len(merged.results) == 1
        assert merged.results[0].agent_id == "agent_a"  # first wins
        assert any(c.conflict_type == "duplicate_result" for c in merged.conflicts)

    def test_evidence_dedup_by_id(self):
        ev1 = _make_evidence(evidence_id="ev-001")
        ev2 = _make_evidence(evidence_id="ev-001")  # same id
        ev3 = _make_evidence(evidence_id="ev-002")

        r1 = _make_result(result_id="r-001", evidence=[ev1, ev3])
        r2 = _make_result(result_id="r-002", evidence=[ev2])

        merged = merge_parallel_results([r1, r2])
        assert len(merged.merged_evidence) == 2
        evidence_ids = {e.evidence_id for e in merged.merged_evidence}
        assert evidence_ids == {"ev-001", "ev-002"}

    def test_proposal_dedup_by_hash(self):
        # Same semantic content → same hash (same idempotency_key)
        p1 = _make_proposal(proposal_id="p-001", idempotency_key="ik-same")
        p2 = _make_proposal(proposal_id="p-002", idempotency_key="ik-same")

        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])

        merged = merge_parallel_results([r1, r2])
        # Same hash → only one proposal survives
        assert len(merged.merged_proposals) == 1


# ---------------------------------------------------------------------------
# Content conflicts
# ---------------------------------------------------------------------------


class TestContentConflicts:
    def test_different_proposal_same_id_conflict(self):
        p1 = _make_proposal(proposal_id="p-001", payload={"a": 1})
        p2 = _make_proposal(
            proposal_id="p-001",
            payload={"b": 2},
            # Force different hash by changing action_type
            action_type="delete",
            proposal_hash=_compute_proposal_hash(
                tenant_id="t-001",
                created_by_agent="agent_a",
                action_type="delete",
                target_entity="ticket",
                target_id=None,
                payload={"b": 2},
                priority="medium",
                justification="test",
                evidence_ids=[],
                requires_approval=True,
                idempotency_key="ik-p-001",
            ),
        )

        r1 = _make_result(result_id="r-001", proposals=[p1])
        r2 = _make_result(result_id="r-002", proposals=[p2])

        merged = merge_parallel_results([r1, r2])
        assert any(c.conflict_type == "content_mismatch" for c in merged.conflicts)


# ---------------------------------------------------------------------------
# Foreign tenant evidence
# ---------------------------------------------------------------------------


class TestForeignTenantEvidence:
    def test_foreign_tenant_evidence_rejected(self):
        ev_good = _make_evidence(evidence_id="ev-001", tenant_id="t-001")
        ev_bad = _make_evidence(evidence_id="ev-002", tenant_id="t-002")

        r1 = _make_result(result_id="r-001", tenant_id="t-001", evidence=[ev_good])
        r2 = _make_result(result_id="r-002", tenant_id="t-001", evidence=[ev_bad])

        merged = merge_parallel_results([r1, r2])
        # Only ev-001 from tenant t-001 should be kept
        evidence_ids = {e.evidence_id for e in merged.merged_evidence}
        assert "ev-001" in evidence_ids
        assert "ev-002" not in evidence_ids
        assert any(c.conflict_type == "foreign_tenant" for c in merged.conflicts)

    def test_foreign_tenant_evidence_creates_conflict(self):
        ev_bad = _make_evidence(evidence_id="ev-foreign", tenant_id="other-tenant")

        r1 = _make_result(result_id="r-001", tenant_id="t-001", evidence=[ev_bad])
        merged = merge_parallel_results([r1])
        assert len(merged.conflicts) >= 1
        assert any("foreign_tenant" in c.conflict_type for c in merged.conflicts)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_merge_does_not_modify_inputs(self):
        ev = _make_evidence(evidence_id="ev-001")
        original_evidence = copy.deepcopy(ev)
        r1 = _make_result(result_id="r-001", evidence=[ev])

        merge_parallel_results([r1])

        # Original evidence should not be modified
        assert ev.evidence_id == original_evidence.evidence_id
        assert ev.tenant_id == original_evidence.tenant_id

    def test_results_not_mutated(self):
        r1 = _make_result(result_id="r-001")
        original_id = r1.result_id

        merge_parallel_results([r1])
        assert r1.result_id == original_id
