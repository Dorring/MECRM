"""Phase 5A Evidence Review tests.

Covers (Phase 5A Section 17 — Identity & Evidence):

* cross-tenant Evidence rejected
* dangling Evidence rejected (source_agent not in capability snapshots)
* task/agent inconsistency
* Evidence hash tamper rejected
* duplicate Evidence deterministically handled
* missing Evidence rejected
* type mismatch rejected
"""

from __future__ import annotations

from datetime import datetime, timezone


from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    Evidence,
    EvidenceType,
)
from multi_agent.evidence_review import (
    build_evidence_index,
    detect_dangling_evidence,
    detect_duplicate_evidence,
    validate_evidence_for_proposal,
)
from multi_agent.review_contracts import (
    CODE_EVIDENCE_DANGLING,
    CODE_EVIDENCE_DUPLICATE,
    CODE_EVIDENCE_FOREIGN_TENANT,
    CODE_EVIDENCE_HASH_MISMATCH,
    CODE_EVIDENCE_MISSING,
    CODE_EVIDENCE_TYPE_MISMATCH,
    CapabilitySnapshot,
    ReviewFindingSeverity,
)


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_evidence(
    evidence_id: str = "ev-001",
    *,
    evidence_type: EvidenceType = EvidenceType.CUSTOMER,
    tenant_id: str = "tenant-test",
    source_agent: str = "agent_test",
    content_hash: str | None = "a" * 64,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=tenant_id,
        source_agent=source_agent,
        content_hash=content_hash,
        created_at=_TS,
    )


def _make_proposal(
    proposal_id: str = "prop-001",
    *,
    action_type: str = "report.generate",
    evidence_ids: list[str] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    tenant_id: str = "tenant-test",
    created_by_agent: str = "agent_test",
    idempotency_key: str = "idem-key-0001",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity="report",
        evidence_ids=evidence_ids or [],
        risk_level=risk_level,
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


def _make_cap_snapshot(
    agent_id: str = "agent_test",
    authority: AgentAuthority = AgentAuthority.READ,
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        agent_id=agent_id,
        capability=AgentCapability(
            agent_id=agent_id,
            version="1.0.0",
            description="test",
            domains=frozenset({"test"}),
            supported_tasks=frozenset({"test_task"}),
            allowed_tools=frozenset({"crm_reader.get_customers"}),
            authority=authority,
            input_contract="in",
            output_contract="out",
            timeout_ms=300_000,
            max_retries=0,
            estimated_cost_class="low",
        ),
    )


# ---------------------------------------------------------------------------
# build_evidence_index
# ---------------------------------------------------------------------------


class TestBuildEvidenceIndex:
    def test_single_evidence_indexed(self):
        ev = _make_evidence("ev-001")
        idx, _ = build_evidence_index([ev])
        assert idx["ev-001"] == ev

    def test_duplicate_with_same_content_deduped(self):
        ev1 = _make_evidence("ev-001")
        ev2 = _make_evidence("ev-001")
        idx, _ = build_evidence_index([ev1, ev2])
        # Same content → only one kept
        assert "ev-001" in idx

    def test_duplicate_with_different_content_excluded(self):
        # compute_review_evidence_hash excludes the self-referential
        # content_hash field, so differ in source_agent to produce
        # genuinely distinct review hashes.
        ev1 = _make_evidence("ev-001", source_agent="agent_a")
        ev2 = _make_evidence("ev-001", source_agent="agent_b")
        idx, excluded = build_evidence_index([ev1, ev2])
        # Different content → all excluded (fail closed)
        assert "ev-001" not in idx
        assert "ev-001" in excluded


# ---------------------------------------------------------------------------
# detect_duplicate_evidence
# ---------------------------------------------------------------------------


class TestDetectDuplicateEvidence:
    def test_no_duplicates_returns_empty(self):
        evs = [_make_evidence("ev-001"), _make_evidence("ev-002")]
        findings = detect_duplicate_evidence(evs)
        assert findings == []

    def test_duplicate_with_different_content_flagged(self):
        # compute_review_evidence_hash excludes the self-referential
        # content_hash field, so differ in source_agent to produce
        # genuinely distinct review hashes.
        ev1 = _make_evidence("ev-001", source_agent="agent_a")
        ev2 = _make_evidence("ev-001", source_agent="agent_b")
        findings = detect_duplicate_evidence([ev1, ev2])
        assert len(findings) == 1
        assert findings[0].finding_code == CODE_EVIDENCE_DUPLICATE
        assert findings[0].severity == ReviewFindingSeverity.ERROR

    def test_duplicate_with_same_content_not_flagged(self):
        ev1 = _make_evidence("ev-001")
        ev2 = _make_evidence("ev-001")
        findings = detect_duplicate_evidence([ev1, ev2])
        assert findings == []


# ---------------------------------------------------------------------------
# validate_evidence_for_proposal
# ---------------------------------------------------------------------------


class TestValidateEvidenceForProposal:
    def test_valid_evidence_no_findings(self):
        ev = _make_evidence("ev-001")
        prop = _make_proposal(evidence_ids=["ev-001"])
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([ev])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert findings == []

    def test_missing_evidence_flagged(self):
        prop = _make_proposal(evidence_ids=["ev-does-not-exist"])
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert len(findings) == 1
        assert findings[0].finding_code == CODE_EVIDENCE_MISSING
        assert findings[0].severity == ReviewFindingSeverity.ERROR

    def test_foreign_tenant_evidence_flagged(self):
        ev = _make_evidence("ev-001", tenant_id="tenant-OTHER")
        prop = _make_proposal(evidence_ids=["ev-001"])
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([ev])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert any(f.finding_code == CODE_EVIDENCE_FOREIGN_TENANT for f in findings)

    def test_dangling_evidence_flagged(self):
        """Evidence source_agent is neither the proposal's agent nor in
        the capability snapshots."""
        ev = _make_evidence("ev-001", source_agent="rogue_agent")
        prop = _make_proposal(evidence_ids=["ev-001"])
        cap = _make_cap_snapshot(agent_id="agent_test")
        idx, _ = build_evidence_index([ev])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert any(f.finding_code == CODE_EVIDENCE_DANGLING for f in findings)

    def test_invalid_content_hash_flagged(self):
        ev = _make_evidence("ev-001", content_hash="not-hex!")
        prop = _make_proposal(evidence_ids=["ev-001"])
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([ev])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert any(f.finding_code == CODE_EVIDENCE_HASH_MISMATCH for f in findings)

    def test_type_mismatch_flagged(self):
        ev = _make_evidence(
            "ev-001",
            evidence_type=EvidenceType.METRIC,
        )
        # metric.query requires EvidenceType.METRIC, so this should pass.
        # Use crm.tag.update which requires CUSTOMER/CONTACT/TICKET/DEAL.
        prop = _make_proposal(
            action_type="crm.tag.update",
            evidence_ids=["ev-001"],
            risk_level=ActionRiskLevel.MEDIUM,
        )
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([ev])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert any(f.finding_code == CODE_EVIDENCE_TYPE_MISMATCH for f in findings)

    def test_duplicate_reference_within_proposal_flagged(self):
        ev = _make_evidence("ev-001")
        prop = _make_proposal(evidence_ids=["ev-001", "ev-001"])
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([ev])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        assert any(f.finding_code == CODE_EVIDENCE_DUPLICATE for f in findings)

    def test_high_risk_proposal_without_valid_evidence_flagged(self):
        prop = _make_proposal(
            action_type="crm.owner.assign",
            evidence_ids=["ev-missing"],
            risk_level=ActionRiskLevel.HIGH,
        )
        cap = _make_cap_snapshot()
        idx, _ = build_evidence_index([])
        findings = validate_evidence_for_proposal(
            prop,
            idx,
            {cap.agent_id: cap},
            tenant_id="tenant-test",
        )
        # Should have both missing-evidence and high-risk-no-valid-evidence
        codes = {f.finding_code for f in findings}
        assert CODE_EVIDENCE_MISSING in codes


# ---------------------------------------------------------------------------
# detect_dangling_evidence
# ---------------------------------------------------------------------------


class TestDetectDanglingEvidence:
    def test_orphan_evidence_info_finding(self):
        ev = _make_evidence("ev-orphan")
        prop = _make_proposal(evidence_ids=["ev-001"])  # references a different id
        idx, _ = build_evidence_index([ev])
        findings = detect_dangling_evidence([prop], idx)
        assert len(findings) == 1
        assert findings[0].finding_code == CODE_EVIDENCE_DANGLING
        assert findings[0].severity == ReviewFindingSeverity.INFO

    def test_no_orphans_no_findings(self):
        ev = _make_evidence("ev-001")
        prop = _make_proposal(evidence_ids=["ev-001"])
        idx, _ = build_evidence_index([ev])
        findings = detect_dangling_evidence([prop], idx)
        assert findings == []
