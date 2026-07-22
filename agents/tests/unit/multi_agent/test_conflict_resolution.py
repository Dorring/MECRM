"""Phase 5A Conflict Resolution tests.

Covers (Phase 5A Section 17 — Conflict):

* exact duplicate → deduped with audit
* same resource + different value → conflict
* mutex action → conflict
* input order does not affect result
* idempotency-key mismatch → conflict
* owner reassign conflict
"""

from __future__ import annotations

from datetime import datetime, timezone


from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
)
from multi_agent.conflict_resolution import (
    compute_canonical_key,
    detect_conflicts,
    detect_duplicates,
    detect_idempotency_key_conflicts,
)
from multi_agent.review_contracts import (
    CODE_CONFLICT_FIELD_VALUE,
    CODE_CONFLICT_OWNER_REASSIGN,
    CODE_DUPLICATE_DEDUPED,
)


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_proposal(
    proposal_id: str,
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict | None = None,
    idempotency_key: str = "idem-key-0001",
    tenant_id: str = "tenant-test",
    created_by_agent: str = "agent_test",
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity=target_entity,
        target_id=target_id,
        payload=payload or {},
        risk_level=risk_level,
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


# ---------------------------------------------------------------------------
# compute_canonical_key
# ---------------------------------------------------------------------------


class TestCanonicalKey:
    def test_same_content_same_key(self):
        p1 = _make_proposal("p1")
        p2 = _make_proposal("p2")  # different id, same content
        assert compute_canonical_key(p1) == compute_canonical_key(p2)

    def test_different_action_different_key(self):
        p1 = _make_proposal("p1", action_type="report.generate")
        p2 = _make_proposal("p2", action_type="summary.compile")
        assert compute_canonical_key(p1) != compute_canonical_key(p2)

    def test_different_payload_different_key(self):
        p1 = _make_proposal("p1", payload={"k": "v1"})
        p2 = _make_proposal("p2", payload={"k": "v2"})
        assert compute_canonical_key(p1) != compute_canonical_key(p2)

    def test_different_target_different_key(self):
        p1 = _make_proposal("p1", target_id="cust-001")
        p2 = _make_proposal("p2", target_id="cust-002")
        assert compute_canonical_key(p1) != compute_canonical_key(p2)

    def test_key_excludes_proposal_id(self):
        """Canonical key must NOT depend on proposal_id."""
        p1 = _make_proposal("p1", target_id="cust-001")
        p2 = _make_proposal("p2", target_id="cust-001")
        assert compute_canonical_key(p1) == compute_canonical_key(p2)


# ---------------------------------------------------------------------------
# detect_duplicates
# ---------------------------------------------------------------------------


class TestDetectDuplicates:
    def test_exact_duplicate_deduped(self):
        p1 = _make_proposal("p1", idempotency_key="key-0001")
        p2 = _make_proposal("p2", idempotency_key="key-0001")
        result = detect_duplicates([p1, p2])
        assert "p1" in result.deduped_proposal_ids
        assert "p2" in result.excluded_proposal_ids
        assert len(result.findings) == 1
        assert result.findings[0].finding_code == CODE_DUPLICATE_DEDUPED

    def test_different_idempotency_key_not_deduped(self):
        p1 = _make_proposal("p1", idempotency_key="key-0001")
        p2 = _make_proposal("p2", idempotency_key="key-0002")
        result = detect_duplicates([p1, p2])
        assert "p1" in result.deduped_proposal_ids
        assert "p2" in result.deduped_proposal_ids
        assert result.excluded_proposal_ids == set()
        assert result.findings == []

    def test_primary_is_lexicographically_smallest(self):
        p1 = _make_proposal("prop-002", idempotency_key="key-0001")
        p2 = _make_proposal("prop-001", idempotency_key="key-0001")
        result = detect_duplicates([p1, p2])
        assert "prop-001" in result.deduped_proposal_ids
        assert "prop-002" in result.excluded_proposal_ids

    def test_input_order_does_not_affect_primary(self):
        p1 = _make_proposal("prop-002", idempotency_key="key-0001")
        p2 = _make_proposal("prop-001", idempotency_key="key-0001")
        r1 = detect_duplicates([p1, p2])
        r2 = detect_duplicates([p2, p1])
        assert r1.deduped_proposal_ids == r2.deduped_proposal_ids
        assert r1.excluded_proposal_ids == r2.excluded_proposal_ids


# ---------------------------------------------------------------------------
# detect_conflicts
# ---------------------------------------------------------------------------


class TestDetectConflicts:
    def test_same_resource_different_value_conflict(self):
        p1 = _make_proposal(
            "p1",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            idempotency_key="key-0002",
        )
        result = detect_conflicts([p1, p2])
        assert "p1" in result.conflicted_proposal_ids
        assert "p2" in result.conflicted_proposal_ids
        assert any(f.finding_code == CODE_CONFLICT_FIELD_VALUE for f in result.findings)

    def test_same_resource_same_value_no_conflict(self):
        p1 = _make_proposal(
            "p1",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="key-0002",
        )
        result = detect_conflicts([p1, p2])
        assert result.conflicted_proposal_ids == set()

    def test_different_resources_no_conflict(self):
        p1 = _make_proposal(
            "p1",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-002",
            payload={"tag": "vip"},
            idempotency_key="key-0002",
        )
        result = detect_conflicts([p1, p2])
        assert result.conflicted_proposal_ids == set()

    def test_owner_reassign_conflict(self):
        p1 = _make_proposal(
            "p1",
            action_type="crm.owner.assign",
            target_entity="customer",
            target_id="cust-001",
            payload={"owner_id": "owner-001"},
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="crm.owner.assign",
            target_entity="customer",
            target_id="cust-001",
            payload={"owner_id": "owner-002"},
            idempotency_key="key-0002",
        )
        result = detect_conflicts([p1, p2])
        assert result.conflicted_proposal_ids == {"p1", "p2"}
        assert any(
            f.finding_code == CODE_CONFLICT_OWNER_REASSIGN for f in result.findings
        )

    def test_input_order_does_not_affect_conflicts(self):
        p1 = _make_proposal(
            "p1",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            idempotency_key="key-0002",
        )
        r1 = detect_conflicts([p1, p2])
        r2 = detect_conflicts([p2, p1])
        assert r1.conflicted_proposal_ids == r2.conflicted_proposal_ids
        # Findings should also be the same set
        assert {f.proposal_id for f in r1.findings} == {
            f.proposal_id for f in r2.findings
        }

    def test_excluded_proposals_skipped(self):
        """Duplicates passed in excluded_proposal_ids are not conflict
        participants."""
        p1 = _make_proposal(
            "p1",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "vip"},
            idempotency_key="key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            idempotency_key="key-0002",
        )
        # Exclude p2 — no conflict should be detected
        result = detect_conflicts([p1, p2], excluded_proposal_ids={"p2"})
        assert result.conflicted_proposal_ids == set()


# ---------------------------------------------------------------------------
# detect_idempotency_key_conflicts
# ---------------------------------------------------------------------------


class TestIdempotencyKeyConflicts:
    def test_same_key_different_canonical_conflict(self):
        p1 = _make_proposal(
            "p1",
            action_type="report.generate",
            idempotency_key="shared-key-0001",
        )
        p2 = _make_proposal(
            "p2",
            action_type="summary.compile",  # different action → different canonical
            idempotency_key="shared-key-0001",
        )
        pairs = detect_idempotency_key_conflicts([p1, p2])
        assert len(pairs) == 1
        assert ("p1", "p2") in pairs

    def test_same_key_same_canonical_no_conflict(self):
        p1 = _make_proposal("p1", idempotency_key="shared-key-0001")
        p2 = _make_proposal("p2", idempotency_key="shared-key-0001")
        # Same canonical key + same idempotency → duplicate, not conflict
        pairs = detect_idempotency_key_conflicts([p1, p2])
        assert pairs == []

    def test_different_keys_no_conflict(self):
        p1 = _make_proposal("p1", idempotency_key="key-0001")
        p2 = _make_proposal("p2", idempotency_key="key-0002")
        pairs = detect_idempotency_key_conflicts([p1, p2])
        assert pairs == []
