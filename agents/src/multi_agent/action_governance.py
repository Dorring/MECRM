"""Phase 5A R2 — Unified Action Governance Specification.

R2 S3: a single, frozen, hash-stable source of truth for every
action-type's governance rules.  Before R2 the same rule was
duplicated across :mod:`multi_agent.reviewer` (``_ACTION_RISK``,
``_ACTION_AUTHORITY_FLOOR``, ``_ACTION_TO_TOOL``),
:mod:`multi_agent.evidence_review` (``_ACTION_EVIDENCE_REQUIREMENTS``),
:mod:`multi_agent.policy` (``_REVIEWABLE_ACTION_CATEGORIES``,
``_EXECUTE_ONLY_CATEGORIES``, ``_AUTHORITY_FLOOR``,
``_ALWAYS_NEEDS_APPROVAL``), and :mod:`multi_agent.conflict_resolution`
(``_ACTIVATE_ACTIONS`` etc.).  Each duplicate could drift independently
and silently change the approval bar.

This module exposes:

* :data:`ACTION_GOVERNANCE_SPEC_VERSION` — bumped whenever any spec
  entry changes.
* :data:`ACTION_GOVERNANCE_SPEC_HASH` — SHA-256 over the canonical
  spec registry, so a Reviewer can detect a spec drift between the
  Request boundary and the live registry.
* :class:`ActionGovernanceSpec` — frozen, hashable per-action rule.
* :data:`ACTION_GOVERNANCE_REGISTRY` — the canonical registry mapping
  ``action_type`` → :class:`ActionGovernanceSpec`.
* :func:`get_action_governance_spec` — accessor with explicit
  "unknown action" semantics.

Every Reviewer / Policy / Evidence / Conflict module MUST read from
this registry — local lookup tables are forbidden (R2 S14).
"""

from __future__ import annotations

from pydantic import ConfigDict, field_validator

from multi_agent.contracts import AgentAuthority, EvidenceType, StrictContract
from multi_agent.review_contracts import ReviewRiskLevel
from multi_agent.serialization import stable_hash


# ---------------------------------------------------------------------------
# Spec version — bumped on every registry change.
# ---------------------------------------------------------------------------

ACTION_GOVERNANCE_SPEC_VERSION = "ma-05a.action-governance.1.0"


# ---------------------------------------------------------------------------
# Frozen per-action governance contract.
# ---------------------------------------------------------------------------


class ActionGovernanceSpec(StrictContract):
    """Frozen, hashable governance rule for one ``action_type``.

    Every field that affects the Reviewer's decision for an action
    lives here so there is exactly ONE definition per action_type
    across the entire Phase 5A codebase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: str
    reviewable: bool
    canonical_risk: ReviewRiskLevel
    minimum_authority: AgentAuthority
    required_tool: str | None = None
    required_evidence_types: frozenset[EvidenceType] = frozenset()
    idempotency_required: bool = False
    always_needs_approval: bool = False
    conflict_family: str | None = None
    parameter_schema_id: str = "default"

    @field_validator("action_type")
    @classmethod
    def _action_type_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("action_type must not be blank")
        return v


# ---------------------------------------------------------------------------
# Canonical registry — the single source of truth.
# ---------------------------------------------------------------------------


def _build_registry() -> dict[str, ActionGovernanceSpec]:
    """Build the canonical action governance registry.

    Order does not matter — the registry is keyed by ``action_type``
    and the spec hash is computed over a canonical (sorted) form.
    """
    specs: list[ActionGovernanceSpec] = [
        # --- Read-only / report proposals ----------------------------------
        ActionGovernanceSpec(
            action_type="report.generate",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            required_evidence_types=frozenset(),
            idempotency_required=False,
            always_needs_approval=False,
            conflict_family=None,
        ),
        ActionGovernanceSpec(
            action_type="summary.compile",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            required_evidence_types=frozenset(),
            idempotency_required=False,
            always_needs_approval=False,
            conflict_family=None,
        ),
        ActionGovernanceSpec(
            action_type="metric.query",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            required_evidence_types=frozenset({EvidenceType.METRIC}),
            idempotency_required=False,
            always_needs_approval=False,
            conflict_family=None,
        ),
        # --- CRM propose (no execute) --------------------------------------
        ActionGovernanceSpec(
            action_type="crm.tag.update",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.MEDIUM,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {
                    EvidenceType.CUSTOMER,
                    EvidenceType.CONTACT,
                    EvidenceType.TICKET,
                    EvidenceType.DEAL,
                }
            ),
            idempotency_required=True,
            always_needs_approval=False,
            conflict_family="crm_field_update",
        ),
        ActionGovernanceSpec(
            action_type="crm.status.update",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.MEDIUM,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {
                    EvidenceType.CUSTOMER,
                    EvidenceType.TICKET,
                    EvidenceType.DEAL,
                }
            ),
            idempotency_required=True,
            always_needs_approval=False,
            conflict_family="crm_status_activate",
        ),
        ActionGovernanceSpec(
            action_type="crm.note.add",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.MEDIUM,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {
                    EvidenceType.CUSTOMER,
                    EvidenceType.CONTACT,
                    EvidenceType.TICKET,
                    EvidenceType.DEAL,
                }
            ),
            idempotency_required=True,
            always_needs_approval=False,
            conflict_family="crm_create",
        ),
        ActionGovernanceSpec(
            action_type="crm.owner.assign",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.HIGH,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset({EvidenceType.CUSTOMER}),
            idempotency_required=True,
            always_needs_approval=True,
            conflict_family="crm_owner_reassign",
        ),
        ActionGovernanceSpec(
            action_type="crm.escalate",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.HIGH,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {EvidenceType.TICKET, EvidenceType.CUSTOMER}
            ),
            idempotency_required=True,
            always_needs_approval=True,
            conflict_family="crm_status_activate",
        ),
        # --- Recovery actions (high-risk, always needs_approval) ----------
        ActionGovernanceSpec(
            action_type="refund.issue",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.CRITICAL,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {
                    EvidenceType.CUSTOMER,
                    EvidenceType.TICKET,
                    EvidenceType.DEAL,
                }
            ),
            idempotency_required=True,
            always_needs_approval=True,
            conflict_family=None,
        ),
        ActionGovernanceSpec(
            action_type="contract.amend",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.CRITICAL,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {EvidenceType.DEAL, EvidenceType.CUSTOMER}
            ),
            idempotency_required=True,
            always_needs_approval=True,
            conflict_family=None,
        ),
        ActionGovernanceSpec(
            action_type="notification.bulk_send",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.HIGH,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="crm_writer.propose",
            required_evidence_types=frozenset(
                {EvidenceType.CUSTOMER, EvidenceType.CONTACT}
            ),
            idempotency_required=True,
            always_needs_approval=True,
            conflict_family="notification_mutex",
        ),
        ActionGovernanceSpec(
            action_type="permission.change",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.CRITICAL,
            minimum_authority=AgentAuthority.PROPOSE,
            required_tool="governance.approve",
            required_evidence_types=frozenset(
                {EvidenceType.CUSTOMER, EvidenceType.AUDIT_EVENT}
            ),
            idempotency_required=True,
            always_needs_approval=True,
            conflict_family="crm_status_activate",
        ),
    ]
    return {s.action_type: s for s in specs}


ACTION_GOVERNANCE_REGISTRY: dict[str, ActionGovernanceSpec] = _build_registry()


# ---------------------------------------------------------------------------
# Spec hash — stable across processes (canonical, sorted).
# ---------------------------------------------------------------------------


def _compute_spec_hash() -> str:
    """Return a stable SHA-256 over the canonical spec registry."""
    # Sort by action_type so the hash is order-invariant.
    payload = [
        ACTION_GOVERNANCE_REGISTRY[k].model_dump(mode="python")
        for k in sorted(ACTION_GOVERNANCE_REGISTRY)
    ]
    return stable_hash(payload)


ACTION_GOVERNANCE_SPEC_HASH: str = _compute_spec_hash()


# ---------------------------------------------------------------------------
# Accessor
# ---------------------------------------------------------------------------


def get_action_governance_spec(action_type: str) -> ActionGovernanceSpec | None:
    """Return the :class:`ActionGovernanceSpec` for *action_type*, or
    ``None`` if the action is not registered.

    Callers MUST treat ``None`` as "unknown action" and fail-closed
    (e.g. emit ``CODE_ACTION_UNKNOWN_TYPE``) — never as "low-risk
    default".
    """
    return ACTION_GOVERNANCE_REGISTRY.get(action_type)


__all__ = [
    "ACTION_GOVERNANCE_REGISTRY",
    "ACTION_GOVERNANCE_SPEC_HASH",
    "ACTION_GOVERNANCE_SPEC_VERSION",
    "ActionGovernanceSpec",
    "get_action_governance_spec",
]
