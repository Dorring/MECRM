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

from types import MappingProxyType

from pydantic import ConfigDict, field_validator

from multi_agent.contracts import AgentAuthority, EvidenceType, StrictContract
from multi_agent.review_contracts import ReviewRiskLevel
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Spec version — bumped on every registry change.
# ---------------------------------------------------------------------------

ACTION_GOVERNANCE_SPEC_VERSION = "ma-05a.action-governance.2.0"


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

    # Phase 5B R3 — execution safety fields (P0-5 / P0-9 / P0-3).
    # These enter the spec hash so a change is detectable at the
    # Review / Executor boundary.
    execution_retry_allowed: bool = False
    max_execution_retries: int = 0
    retryable_error_codes: frozenset[str] = frozenset()
    required_approver_roles: tuple[str, ...] = ()
    minimum_approver_count: int = 1
    approval_ttl_seconds: int = 86400
    resource_type: str | None = None
    resource_id_fields: tuple[str, ...] = ()

    @field_validator("action_type")
    @classmethod
    def _action_type_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("action_type must not be blank")
        return v

    @field_validator("required_approver_roles")
    @classmethod
    def _freeze_approver_roles(cls, v: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(sorted({str(r).strip() for r in v if str(r).strip()}))
        raise TypeError("required_approver_roles must be a list or tuple")

    @field_validator("max_execution_retries")
    @classmethod
    def _non_negative_retries(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_execution_retries must be >= 0")
        return v

    @field_validator("minimum_approver_count")
    @classmethod
    def _non_negative_count(cls, v: int) -> int:
        if v < 1:
            raise ValueError("minimum_approver_count must be >= 1")
        return v

    @field_validator("approval_ttl_seconds")
    @classmethod
    def _positive_ttl(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("approval_ttl_seconds must be > 0")
        return v


# ---------------------------------------------------------------------------
# Canonical registry — the single source of truth.
# ---------------------------------------------------------------------------


def _build_registry() -> dict[str, ActionGovernanceSpec]:
    """Build the canonical action governance registry.

    Order does not matter — the registry is keyed by ``action_type``
    and the spec hash is computed over a canonical (sorted) form.

    R3: every approval / retry / resource field is explicit per action
    type so the executor never falls back to hardcoded defaults.
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
            required_approver_roles=("manager",),
            approval_ttl_seconds=86400,
            resource_type=None,
            resource_id_fields=(),
            execution_retry_allowed=True,
            max_execution_retries=1,
            retryable_error_codes=frozenset({"transient_failure"}),
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
            required_approver_roles=("manager",),
            approval_ttl_seconds=86400,
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
            required_approver_roles=("manager",),
            approval_ttl_seconds=86400,
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
            required_approver_roles=("manager",),
            approval_ttl_seconds=43200,
            resource_type="customer",
            resource_id_fields=("customer_id", "target_id"),
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
            required_approver_roles=("manager",),
            approval_ttl_seconds=43200,
            resource_type="customer",
            resource_id_fields=("customer_id", "target_id"),
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
            required_approver_roles=("manager",),
            approval_ttl_seconds=43200,
            resource_type="customer",
            resource_id_fields=("customer_id", "target_id"),
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
            required_approver_roles=("admin", "owner_manager"),
            minimum_approver_count=1,
            approval_ttl_seconds=21600,
            resource_type="customer",
            resource_id_fields=("customer_id", "target_id"),
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
            required_approver_roles=("admin", "support_lead"),
            approval_ttl_seconds=21600,
            resource_type="customer",
            resource_id_fields=("customer_id", "target_id"),
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
            required_approver_roles=("finance_admin", "admin"),
            approval_ttl_seconds=7200,
            resource_type="deal",
            resource_id_fields=("deal_id", "target_id"),
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
            required_approver_roles=("legal_admin", "admin"),
            approval_ttl_seconds=7200,
            resource_type="deal",
            resource_id_fields=("deal_id", "target_id"),
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
            required_approver_roles=("admin", "marketing_lead"),
            approval_ttl_seconds=21600,
            resource_type="notification",
            resource_id_fields=("campaign_id",),
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
            required_approver_roles=("security_admin",),
            minimum_approver_count=1,
            approval_ttl_seconds=7200,
            resource_type="permission",
            resource_id_fields=("target_id",),
        ),
    ]
    return {s.action_type: s for s in specs}


# R2.1 P0-7: the registry is an IMMUTABLE private mapping.  External
# code receives a read-only :class:`types.MappingProxyType` wrapper —
# attempts to mutate it (``ACTION_GOVERNANCE_REGISTRY["x"] = y``) raise
# ``TypeError`` at runtime.  The underlying ``dict`` is private
# (``_ACTION_GOVERNANCE_REGISTRY``) so it cannot be imported and
# mutated directly.
_ACTION_GOVERNANCE_REGISTRY: dict[str, ActionGovernanceSpec] = _build_registry()

#: Public read-only view of the canonical registry.  External code
#: MUST use this or :func:`get_action_governance_spec` — never the
#: private ``_ACTION_GOVERNANCE_REGISTRY``.
ACTION_GOVERNANCE_REGISTRY: MappingProxyType[str, ActionGovernanceSpec] = (
    MappingProxyType(_ACTION_GOVERNANCE_REGISTRY)
)


# ---------------------------------------------------------------------------
# Spec hash — stable across processes (canonical, sorted).
# ---------------------------------------------------------------------------


def _compute_spec_hash_from(
    registry: MappingProxyType[str, ActionGovernanceSpec]
    | dict[str, ActionGovernanceSpec],
) -> str:
    """Return a stable SHA-256 over the canonical spec registry."""
    # Sort by action_type so the hash is order-invariant.
    payload = [registry[k].model_dump(mode="python") for k in sorted(registry)]
    return stable_hash(payload)


def _compute_spec_hash() -> str:
    """Return a stable SHA-256 over the canonical spec registry."""
    return _compute_spec_hash_from(_ACTION_GOVERNANCE_REGISTRY)


ACTION_GOVERNANCE_SPEC_HASH: str = _compute_spec_hash()


def compute_live_governance_spec_hash() -> str:
    """R2.1 P0-7: recompute the governance spec hash from the LIVE
    registry at call time.

    The Reviewer calls this on every Review to detect tampering: if
    external code has replaced ``_ACTION_GOVERNANCE_REGISTRY`` (which
    is private and should never happen) or if the module constant
    ``ACTION_GOVERNANCE_SPEC_HASH`` has been patched, the live hash
    will differ from the constant, and the Reviewer fails-closed.
    """
    return _compute_spec_hash_from(_ACTION_GOVERNANCE_REGISTRY)


def verify_governance_spec_integrity(*, expected_hash: str | None = None) -> None:
    """R2.1 P0-7: verify the live registry hash matches the module
    constant (and optionally a caller-supplied expected hash).

    Called by the Reviewer at the start of every Review so a tampered
    registry is detected before any Proposal is evaluated.

    Raises :class:`RuntimeError` on mismatch.
    """
    live = compute_live_governance_spec_hash()
    if live != ACTION_GOVERNANCE_SPEC_HASH:
        raise RuntimeError(
            "ACTION_GOVERNANCE_REGISTRY has been tampered with: live spec "
            f"hash {live[:12]!r} != module constant "
            f"{ACTION_GOVERNANCE_SPEC_HASH[:12]!r}"
        )
    if expected_hash is not None and live != expected_hash:
        raise RuntimeError(
            f"Live governance spec hash {live[:12]!r} != expected "
            f"{expected_hash[:12]!r}"
        )


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
    return _ACTION_GOVERNANCE_REGISTRY.get(action_type)


__all__ = [
    "ACTION_GOVERNANCE_REGISTRY",
    "ACTION_GOVERNANCE_SPEC_HASH",
    "ACTION_GOVERNANCE_SPEC_VERSION",
    "ActionGovernanceSpec",
    "compute_live_governance_spec_hash",
    "get_action_governance_spec",
    "verify_governance_spec_integrity",
]
