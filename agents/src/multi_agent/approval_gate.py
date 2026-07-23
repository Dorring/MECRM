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
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from multi_agent.action_governance import ActionGovernanceSpec
from multi_agent.approval_contracts import (
    ApprovalConflictError,
    ApprovalConsumptionRecord,
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

    P0-3: ``validate_and_consume`` is the ONLY atomic consume path —
    it validates tenant, run, proposal, authorization hash, request
    hash, approver role, expiry, and status ALL under the store lock
    before marking CONSUMED.  The separate ``consume`` method is
    DEPRECATED and MUST NOT be used by the executor.
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

    async def validate_and_consume(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ApprovalDecision: ...

    async def validate_decision(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ApprovalDecision:
        """P0-2: read-only validation of the approval decision.

        Validates tenant, run, proposal, authorization_hash, approver role,
        expiry, and status — but does NOT consume the approval.  The
        approval remains available for consumption or rejection.
        """
        ...

    async def consume_for_command(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        command_id: str,
        execution_fingerprint: str,
        now: datetime,
    ) -> ApprovalConsumptionRecord:
        """P0-2: consume the approval and bind it to a specific command.

        The consumption record binds the approval to the exact command_id
        and execution_fingerprint.  A replay of the same command can read
        the original consumption (not a second illegal consume).  A different
        command cannot reuse the consumption.
        """
        ...

    async def get_consumption(
        self,
        approval_id: str,
    ) -> ApprovalConsumptionRecord | None:
        """Return the consumption record for *approval_id*, or None."""
        ...

    async def get_decision(self, approval_id: str) -> ApprovalDecision | None: ...


# ---------------------------------------------------------------------------
# InMemoryApprovalStore
# ---------------------------------------------------------------------------


class InMemoryApprovalStore:
    """Async, compare-and-set, in-memory approval store.

    Concurrency-safe via a single :class:`asyncio.Lock`.

    Semantics (P0-3 + P1-1):

    * ``create`` — stores a PENDING request.  Same ``approval_id`` +
      same ``approval_request_hash`` → idempotent return.  Same
      ``approval_id`` + different ``approval_request_hash`` →
      :class:`ApprovalConflictError` (P1-1).
    * ``decide`` — applies a terminal decision; only ONE terminal
      decision is allowed per approval.
    * ``validate_and_consume`` — the ONLY atomic consume path (P0-3):
      validates tenant, run, proposal, authorization_hash,
      approval_request_hash, approver role, expiry (vs ``now``), and
      status ALL under the store lock before marking CONSUMED.  Any
      validation failure does NOT consume the approval.
    * ``consume`` — DEPRECATED; delegates to ``validate_and_consume``
      with minimal checks (kept for backwards compatibility with
      tests that pre-construct decisions).
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalDecision] = {}
        self._consumed: set[str] = set()
        self._consumptions: dict[str, ApprovalConsumptionRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._lock:
            existing = self._requests.get(request.approval_id)
            if existing is not None:
                # P1-1: same ID + same hash → idempotent; different hash → conflict.
                if existing.approval_request_hash != request.approval_request_hash:
                    raise ApprovalConflictError(
                        f"approval {request.approval_id!r} already exists with "
                        f"a different approval_request_hash",
                    )
                return existing
            self._requests[request.approval_id] = request
            return request

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        async with self._lock:
            req = self._requests.get(approval_id)
            if req is not None:
                req.verify_integrity()
            return req

    async def get_decision(self, approval_id: str) -> ApprovalDecision | None:
        async with self._lock:
            decision = self._decisions.get(approval_id)
            if decision is not None:
                decision.verify_integrity()
            return decision

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
        """DEPRECATED — use ``validate_and_consume`` instead (P0-3).

        Kept for backwards compatibility; performs the old non-atomic
        consume.  The executor MUST use ``validate_and_consume``.
        """
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

    async def validate_and_consume(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ApprovalDecision:
        """P0-3: atomically validate and consume an approval.

        ALL checks run under the store lock.  Any failure does NOT
        consume the approval — it remains available for correction
        and a subsequent valid consume.

        Checks (in order):
        1. Request exists.
        2. Decision exists (APPROVED).
        3. Not already consumed.
        4. Tenant matches authorization.
        5. Run matches authorization.
        6. Proposal matches authorization.
        7. Authorization ID matches request.
        8. Authorization hash matches decision.
        9. Approval request hash matches decision.
        10. Decision status is APPROVED.
        11. ``now`` <= ``request.expires_at`` (P0-3: uses execution
            clock, NOT ``decision.decided_at``).
        12. Approver holds at least one required role.
        """
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

            # --- Identity checks (fail-closed, no consume) ---
            if request.tenant_id != authorization.tenant_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: tenant_id "
                    f"{request.tenant_id!r} != authorization "
                    f"{authorization.tenant_id!r}",
                )
            if request.run_id != authorization.run_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: run_id mismatch",
                )
            if request.proposal_id != authorization.proposal_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: proposal_id "
                    f"{request.proposal_id!r} != authorization "
                    f"{authorization.proposal_id!r}",
                )
            if request.authorization_id != authorization.authorization_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization_id mismatch",
                )
            if request.authorization_hash != authorization.authorization_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: request.authorization_hash "
                    f"does not match authorization",
                )
            if decision.authorization_hash != authorization.authorization_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.authorization_hash "
                    f"does not match authorization",
                )
            if decision.approval_request_hash != request.approval_request_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.approval_request_hash "
                    f"does not match request",
                )
            if decision.approval_id != approval_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.approval_id mismatch",
                )
            if decision.status != ApprovalStatus.APPROVED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision status is "
                    f"{decision.status.value!r}, not APPROVED",
                )

            # P0-3: expiry check uses the EXECUTION clock (``now``),
            # NOT ``decision.decided_at``.  An approval decided before
            # expiry but consumed after expiry is INVALID.
            if request.expires_at is not None:
                now_utc = now
                if now_utc.tzinfo is None:
                    now_utc = now_utc.replace(tzinfo=timezone.utc)
                now_utc = now_utc.astimezone(timezone.utc)
                if now_utc > request.expires_at:
                    raise ApprovalValidationError(
                        f"approval {approval_id!r}: expired at execution time "
                        f"(now={now_utc.isoformat()}, "
                        f"expires_at={request.expires_at.isoformat()})",
                    )

            # Approver role check.
            if not request.required_approver_roles:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: required_approver_roles is empty",
                )
            held = set(decision.approver_roles)
            required = set(request.required_approver_roles)
            if not (held & required):
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: approver roles "
                    f"{sorted(held)!r} do not include any required role "
                    f"{sorted(required)!r}",
                )

            # All checks passed → atomically mark CONSUMED.
            self._consumed.add(approval_id)
            return decision

    async def validate_decision(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ApprovalDecision:
        """P0-2: read-only validation — does NOT consume the approval."""
        async with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                raise ApprovalValidationError(f"approval {approval_id!r} not found")
            if approval_id in self._consumed:
                raise ApprovalValidationError(
                    f"approval {approval_id!r} already consumed"
                )
            decision = self._decisions.get(approval_id)
            if decision is None:
                raise ApprovalRequiredError(
                    f"approval {approval_id!r} has no decision yet"
                )

            # All the same identity checks as validate_and_consume
            if request.tenant_id != authorization.tenant_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: tenant_id mismatch"
                )
            if request.run_id != authorization.run_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: run_id mismatch"
                )
            if request.proposal_id != authorization.proposal_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: proposal_id mismatch"
                )
            if request.authorization_id != authorization.authorization_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization_id mismatch"
                )
            if request.authorization_hash != authorization.authorization_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization_hash mismatch"
                )
            if decision.authorization_hash != authorization.authorization_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.authorization_hash mismatch"
                )
            if decision.approval_request_hash != request.approval_request_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.approval_request_hash mismatch"
                )
            if decision.approval_id != approval_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.approval_id mismatch"
                )
            if decision.status != ApprovalStatus.APPROVED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision status is "
                    f"{decision.status.value!r}, not APPROVED"
                )

            # P0-3: expiry check using execution clock
            if request.expires_at is not None:
                now_utc = now
                if now_utc.tzinfo is None:
                    now_utc = now_utc.replace(tzinfo=timezone.utc)
                now_utc = now_utc.astimezone(timezone.utc)
                if now_utc > request.expires_at:
                    raise ApprovalValidationError(
                        f"approval {approval_id!r}: expired at execution time"
                    )

            # P1-2: time semantics — decided_at must be >= requested_at and <= now
            decided_at = decision.decided_at
            if decided_at.tzinfo is None:
                decided_at = decided_at.replace(tzinfo=timezone.utc)
            decided_at = decided_at.astimezone(timezone.utc)
            requested_at = request.requested_at
            if requested_at.tzinfo is None:
                requested_at = requested_at.replace(tzinfo=timezone.utc)
            requested_at = requested_at.astimezone(timezone.utc)
            now_utc = now
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            now_utc = now_utc.astimezone(timezone.utc)
            if decided_at < requested_at:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decided_at {decided_at.isoformat()} "
                    f"is before requested_at {requested_at.isoformat()}"
                )
            if decided_at > now_utc:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decided_at {decided_at.isoformat()} "
                    f"is in the future (now={now_utc.isoformat()})"
                )

            # Approver role check
            if not request.required_approver_roles:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: required_approver_roles is empty"
                )
            held = set(decision.approver_roles)
            required = set(request.required_approver_roles)
            if not (held & required):
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: approver roles do not "
                    f"include any required role"
                )

            # Verify integrity of request and decision before returning
            request.verify_integrity()
            decision.verify_integrity()

            return decision

    async def consume_for_command(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        command_id: str,
        execution_fingerprint: str,
        now: datetime,
    ) -> ApprovalConsumptionRecord:
        """P0-2: consume the approval bound to a specific command.

        First validates (read-only), then atomically marks consumed and
        creates a ConsumptionRecord bound to the command.

        If the same command replays, the original ConsumptionRecord is
        returned (not a second consume).  A different command_id cannot
        reuse the consumption.
        """
        async with self._lock:
            # Check for existing consumption (replay of same command)
            existing = self._consumptions.get(approval_id)
            if existing is not None:
                if (
                    existing.command_id == command_id
                    and existing.execution_fingerprint == execution_fingerprint
                ):
                    # Same command replay — return original consumption
                    return existing
                # Different command — cannot reuse
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: already consumed by command "
                    f"{existing.command_id!r}, cannot reuse for {command_id!r}"
                )

            # Validate (inline, not calling validate_decision to avoid
            # double-locking since we already hold the lock)
            request = self._requests.get(approval_id)
            if request is None:
                raise ApprovalValidationError(f"approval {approval_id!r} not found")
            decision = self._decisions.get(approval_id)
            if decision is None:
                raise ApprovalRequiredError(
                    f"approval {approval_id!r} has no decision yet"
                )
            if decision.status != ApprovalStatus.APPROVED:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision status is "
                    f"{decision.status.value!r}, not APPROVED"
                )
            if request.tenant_id != authorization.tenant_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: tenant_id mismatch"
                )
            if request.run_id != authorization.run_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: run_id mismatch"
                )
            if request.proposal_id != authorization.proposal_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: proposal_id mismatch"
                )
            if request.authorization_id != authorization.authorization_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization_id mismatch"
                )
            if request.authorization_hash != authorization.authorization_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization_hash mismatch"
                )
            if decision.authorization_hash != authorization.authorization_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.authorization_hash mismatch"
                )
            if decision.approval_request_hash != request.approval_request_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.approval_request_hash mismatch"
                )
            if decision.approval_id != approval_id:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.approval_id mismatch"
                )

            # Expiry check
            if request.expires_at is not None:
                now_utc = now
                if now_utc.tzinfo is None:
                    now_utc = now_utc.replace(tzinfo=timezone.utc)
                now_utc = now_utc.astimezone(timezone.utc)
                if now_utc > request.expires_at:
                    raise ApprovalValidationError(
                        f"approval {approval_id!r}: expired at execution time"
                    )

            # P1-2: time semantics
            decided_at = decision.decided_at
            if decided_at.tzinfo is None:
                decided_at = decided_at.replace(tzinfo=timezone.utc)
            decided_at = decided_at.astimezone(timezone.utc)
            requested_at = request.requested_at
            if requested_at.tzinfo is None:
                requested_at = requested_at.replace(tzinfo=timezone.utc)
            requested_at = requested_at.astimezone(timezone.utc)
            now_utc = now
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            now_utc = now_utc.astimezone(timezone.utc)
            if decided_at < requested_at:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decided_at before requested_at"
                )
            if decided_at > now_utc:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decided_at is in the future"
                )

            # Approver role check
            if not request.required_approver_roles:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: required_approver_roles is empty"
                )
            held = set(decision.approver_roles)
            required = set(request.required_approver_roles)
            if not (held & required):
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: approver roles do not "
                    f"include any required role"
                )

            # Verify integrity
            request.verify_integrity()
            decision.verify_integrity()

            # Atomically consume + create consumption record
            self._consumed.add(approval_id)
            consumption = ApprovalConsumptionRecord(
                approval_id=approval_id,
                decision_hash=decision.decision_hash,
                authorization_hash=authorization.authorization_hash,
                command_id=command_id,
                execution_fingerprint=execution_fingerprint,
            )
            self._consumptions[approval_id] = consumption
            return consumption

    async def get_consumption(
        self, approval_id: str
    ) -> ApprovalConsumptionRecord | None:
        async with self._lock:
            return self._consumptions.get(approval_id)


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
    "ApprovalConsumptionRecord",
    "ApprovalGate",
    "ApprovalRequirement",
    "ApprovalStore",
    "InMemoryApprovalStore",
]
