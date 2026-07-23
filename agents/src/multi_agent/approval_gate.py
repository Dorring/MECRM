"""Phase 5B — Human Approval Gate.

The :class:`ApprovalGate` decides whether a Proposal needs human
approval, validates an approver's decision, and enforces the
single-consume rule.  The :class:`ApprovalStore` is the durable
boundary (Protocol); :class:`InMemoryApprovalStore` is the
compare-and-set test double.

Key invariants (Phase 5B Section 8):

* A high-risk / approval-required Proposal NEVER executes without a
  valid APPROVED decision (``APPROVAL_REQUIRED``).
* An approver MUST hold at least one of ``required_approver_roles``.
* An APPROVED decision can be consumed exactly ONCE — a second
  consume attempt raises ``APPROVAL_ALREADY_CONSUMED``.
* REJECTED / EXPIRED / REVOKED decisions can NEVER be consumed.
* The decision's ``authorization_hash`` MUST match the
  :class:`ExecutionAuthorization` being executed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timezone
from typing import Protocol, runtime_checkable

from multi_agent.action_governance import ActionGovernanceSpec
from multi_agent.approval_contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
)
from multi_agent.execution_authorization import ExecutionAuthorization
from multi_agent.execution_error_codes import (
    ApprovalRequiredError,
    ApprovalValidationError,
)
from multi_agent.review_contracts import ProposalReview, ReviewRiskLevel


# ---------------------------------------------------------------------------
# ApprovalRequirement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequirement:
    """Resolved approval requirement for one Proposal.

    ``required`` — the Proposal cannot execute without an APPROVED
    decision.
    ``approval_id`` — the id of the :class:`ApprovalRequest` that
    must be approved (None when ``required`` is False).
    ``reason`` — human-readable explanation (audit log).
    """

    required: bool
    approval_id: str | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# ApprovalStore Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ApprovalStore(Protocol):
    """Async approval store boundary.

    All methods are async so a future DB-backed adapter can poll
    without changing call sites.  Implementations MUST be safe under
    concurrent ``decide`` / ``consume`` calls (compare-and-set).
    """

    async def create(self, request: ApprovalRequest) -> ApprovalRequest: ...

    async def get(self, approval_id: str) -> ApprovalRequest | None: ...

    async def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRequest: ...

    async def consume(
        self,
        approval_id: str,
        authorization_hash: str,
    ) -> ApprovalDecision: ...


# ---------------------------------------------------------------------------
# InMemoryApprovalStore
# ---------------------------------------------------------------------------


class InMemoryApprovalStore:
    """Async, compare-and-set, in-memory approval store.

    Concurrency-safe via a single :class:`asyncio.Lock`.

    Semantics:

    * ``create`` — stores a PENDING request; idempotent on
      ``approval_id`` (returns the existing request if present).
    * ``decide`` — applies a terminal decision; only ONE terminal
      decision is allowed per approval (subsequent attempts raise
      :class:`ApprovalValidationError`).
    * ``consume`` — marks an APPROVED decision as CONSUMED; can be
      called exactly ONCE.  REJECTED / EXPIRED / REVOKED decisions
      can NEVER be consumed.
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalDecision] = {}
        self._consumed: set[str] = set()
        self._lock = asyncio.Lock()

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._lock:
            existing = self._requests.get(request.approval_id)
            if existing is not None:
                return existing
            self._requests[request.approval_id] = request
            return request

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        async with self._lock:
            return self._requests.get(approval_id)

    async def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRequest:
        async with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} not found",
                )
            if approval_id in self._decisions:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} already has a terminal decision",
                )
            if decision.approval_id != approval_id:
                raise ApprovalValidationError(
                    f"decision.approval_id {decision.approval_id!r} != {approval_id!r}",
                )
            if decision.approval_request_hash != request.approval_request_hash:
                raise ApprovalValidationError(
                    "decision.approval_request_hash mismatch",
                )
            self._decisions[approval_id] = decision
            return request

    async def consume(
        self,
        approval_id: str,
        authorization_hash: str,
    ) -> ApprovalDecision:
        async with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} not found",
                )
            if approval_id in self._consumed:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} already consumed",
                )
            decision = self._decisions.get(approval_id)
            if decision is None:
                raise ApprovalRequiredError(
                    f"approval {approval_id!r} has no decision yet",
                )
            if decision.status == ApprovalStatus.REJECTED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} was REJECTED",
                )
            if decision.status == ApprovalStatus.EXPIRED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} EXPIRED",
                )
            if decision.status == ApprovalStatus.REVOKED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} was REVOKED",
                )
            if decision.status != ApprovalStatus.APPROVED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} status is "
                    f"{decision.status.value!r}, cannot consume",
                )
            if decision.authorization_hash != authorization_hash:
                raise ApprovalValidationError(
                    "decision.authorization_hash does not match the "
                    "execution authorization",
                )
            self._consumed.add(approval_id)
            return decision


# ---------------------------------------------------------------------------
# ApprovalGate
# ---------------------------------------------------------------------------


class ApprovalGate:
    """Resolves and validates human approval requirements.

    Stateless — all durable state lives in the :class:`ApprovalStore`.
    """

    def resolve_approval_requirement(
        self,
        proposal_review: ProposalReview,
        authorization: ExecutionAuthorization,
        governance_spec: ActionGovernanceSpec,
    ) -> ApprovalRequirement:
        """Decide whether *proposal_review* needs human approval.

        Rules (Phase 5B Section 8):

        * If the review status is not APPROVED and not NEEDS_APPROVAL,
          approval is NOT required (the Proposal is not executable at
          all — the executor will skip it).
        * If ``governance_spec.always_needs_approval`` is True, OR the
          review status is NEEDS_APPROVAL, OR the canonical risk is
          HIGH / CRITICAL, approval IS required.
        * Otherwise approval is NOT required.
        """
        if governance_spec.always_needs_approval:
            return ApprovalRequirement(
                required=True,
                approval_id=authorization.approval_id,
                reason=(
                    f"governance spec for {governance_spec.action_type!r} "
                    f"always requires approval"
                ),
            )
        if proposal_review.status.value == "needs_approval":
            return ApprovalRequirement(
                required=True,
                approval_id=authorization.approval_id,
                reason="review status is needs_approval",
            )
        if governance_spec.canonical_risk in (
            ReviewRiskLevel.HIGH,
            ReviewRiskLevel.CRITICAL,
        ):
            return ApprovalRequirement(
                required=True,
                approval_id=authorization.approval_id,
                reason=(
                    f"canonical risk {governance_spec.canonical_risk.value!r} "
                    f"requires approval"
                ),
            )
        return ApprovalRequirement(required=False, reason="no approval required")

    def validate_decision(
        self,
        decision: ApprovalDecision,
        request: ApprovalRequest,
        authorization: ExecutionAuthorization,
    ) -> None:
        """Validate that *decision* authorises *authorization*.

        Fail-closed checks (Phase 5B Section 8):

        * ``decision.approval_id`` matches ``request.approval_id``.
        * ``decision.approval_request_hash`` matches
          ``request.approval_request_hash``.
        * ``decision.authorization_hash`` matches
          ``authorization.authorization_hash``.
        * ``request.proposal_id`` matches ``authorization.proposal_id``.
        * ``request.authorization_id`` matches
          ``authorization.authorization_id``.
        * ``request.tenant_id`` matches ``authorization.tenant_id``.
        * The decision status is APPROVED.
        * ``request.expires_at`` has not passed (vs ``decided_at``).
        * The approver holds at least one of
          ``request.required_approver_roles``.
        """
        if decision.approval_id != request.approval_id:
            raise ApprovalValidationError(
                f"decision.approval_id {decision.approval_id!r} != "
                f"request {request.approval_id!r}",
            )
        if decision.approval_request_hash != request.approval_request_hash:
            raise ApprovalValidationError(
                "decision.approval_request_hash does not match request",
            )
        if decision.authorization_hash != authorization.authorization_hash:
            raise ApprovalValidationError(
                "decision.authorization_hash does not match authorization",
            )
        if request.proposal_id != authorization.proposal_id:
            raise ApprovalValidationError(
                f"request.proposal_id {request.proposal_id!r} != "
                f"authorization {authorization.proposal_id!r}",
            )
        if request.authorization_id != authorization.authorization_id:
            raise ApprovalValidationError(
                f"request.authorization_id {request.authorization_id!r} != "
                f"authorization {authorization.authorization_id!r}",
            )
        if request.tenant_id != authorization.tenant_id:
            raise ApprovalValidationError(
                f"request.tenant_id {request.tenant_id!r} != authorization "
                f"{authorization.tenant_id!r}",
            )
        if decision.status != ApprovalStatus.APPROVED:
            raise ApprovalValidationError(
                f"decision status is {decision.status.value!r}, not approved",
            )
        if request.expires_at is not None:
            decided_at = decision.decided_at
            if decided_at.tzinfo is None:
                decided_at = decided_at.replace(tzinfo=timezone.utc)
            if decided_at > request.expires_at:
                raise ApprovalValidationError(
                    f"decision decided_at {decided_at.isoformat()} is after "
                    f"expires_at {request.expires_at.isoformat()}",
                )
        # Approver role check — at least one required role must be held.
        if not request.required_approver_roles:
            raise ApprovalValidationError(
                "request.required_approver_roles is empty",
            )
        held = set(decision.approver_roles)
        required = set(request.required_approver_roles)
        if not (held & required):
            raise ApprovalValidationError(
                f"approver roles {sorted(held)!r} do not include any required "
                f"role {sorted(required)!r}",
            )


__all__ = [
    "ApprovalGate",
    "ApprovalRequirement",
    "ApprovalStore",
    "InMemoryApprovalStore",
]
