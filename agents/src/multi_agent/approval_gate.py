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
    concurrent ``decide`` / ``consume_for_command`` calls
    (compare-and-set).

    R3 P0-13: the legacy ``consume`` / ``validate_and_consume`` methods
    have been REMOVED from the Protocol.  The ONLY consume path is
    :meth:`consume_for_command`, which validates tenant, run, proposal,
    ``approval_subject_hash``, approver role, expiry, and status ALL
    under the store lock before marking CONSUMED and binding the
    consumption to a ``command_family_id``.
    """

    async def create(self, request: ApprovalRequest) -> ApprovalRequest: ...

    async def get(self, approval_id: str) -> ApprovalRequest | None: ...

    async def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRequest: ...

    async def validate_decision(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ApprovalDecision:
        """P0-2: read-only validation of the approval decision.

        Validates tenant, run, proposal, ``approval_subject_hash``,
        approver role, expiry, and status — but does NOT consume the
        approval.  The approval remains available for consumption or
        rejection.
        """
        ...

    async def consume_for_command(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        command_family_id: str,
        execution_fingerprint: str,
        now: datetime,
    ) -> ApprovalConsumptionRecord:
        """P0-1 / P0-2 R3: consume the approval and bind it to a
        command family.

        The consumption record binds the approval to the exact
        ``command_family_id`` + ``execution_fingerprint`` (what the
        human approved, via ``approval_subject_hash``).  A replay of
        the same command family + fingerprint returns the original
        consumption (NOT a second illegal consume).  A different
        command family cannot reuse the consumption.
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

    Semantics (P0-1 R3 + P1-1):

    * ``create`` — stores a PENDING request.  Same ``approval_id`` +
      same ``approval_request_hash`` → idempotent return.  Same
      ``approval_id`` + different ``approval_request_hash`` →
      :class:`ApprovalConflictError` (P1-1).
    * ``decide`` — applies a terminal decision; only ONE terminal
      decision is allowed per approval.
    * ``consume_for_command`` — the ONLY atomic consume path
      (R3 P0-13): validates tenant, run, proposal,
      ``approval_subject_hash`` (the hash the human approved, NOT the
      final ``authorization_hash`` which includes the decision),
      approver role, expiry (vs ``now``), and status ALL under the
      store lock before marking CONSUMED and binding the consumption
      to a ``command_family_id``.  Any validation failure does NOT
      consume the approval.
    * ``consume`` / ``validate_and_consume`` — REMOVED (R3 P0-13).
      The legacy non-atomic consume paths are no longer available;
      the executor MUST use ``consume_for_command``.
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

    async def validate_decision(
        self,
        approval_id: str,
        *,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ApprovalDecision:
        """P0-1 R3: read-only validation — does NOT consume the approval.

        R3 fix: the binding check uses ``approval_subject_hash`` (what
        the human approved) instead of the final ``authorization_hash``
        (which includes the approval decision and could be forged
        post-decision).  The Approval Request and Decision both bind to
        ``approval_subject_hash``; the final ``authorization_hash`` is
        only checked for internal consistency (decision == request).
        """
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

            # --- Identity checks (fail-closed, no consume) ---
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
            # P0-1 R3: the request and decision bind to
            # ``approval_subject_hash`` (what the human approved), NOT
            # the final ``authorization_hash`` (which changes after the
            # decision is bound).  This is the core R3 fix: a forged
            # post-decision authorization that re-derives a new
            # ``authorization_hash`` cannot reuse the approval because
            # the subject hash is stable (base + approval_id).
            subject_hash = authorization.approval_subject_hash
            if subject_hash is None:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization has no "
                    f"approval_subject_hash (approval_id missing)"
                )
            if request.authorization_hash != subject_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: request.authorization_hash "
                    f"does not match approval_subject_hash"
                )
            if decision.authorization_hash != subject_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.authorization_hash "
                    f"does not match approval_subject_hash"
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
        command_family_id: str,
        execution_fingerprint: str,
        now: datetime,
    ) -> ApprovalConsumptionRecord:
        """P0-1 / P0-2 R3: consume the approval bound to a command family.

        R3 fixes:

        * The binding check uses ``approval_subject_hash`` (what the
          human approved) instead of the final ``authorization_hash``.
        * The consumption record carries ``command_family_id`` (not a
          single ``command_id``) so safe retries within the same family
          can reuse the consumption.
        * Replay logic: same ``command_family_id`` + same
          ``execution_fingerprint`` → return the ORIGINAL consumption
          (NOT a second consume).  Different ``command_family_id`` →
          cannot reuse (fail-closed).

        First validates (read-only), then atomically marks consumed and
        creates a ConsumptionRecord bound to the command family.
        """
        async with self._lock:
            # P0-1 R3: replay logic keyed on command_family_id.
            existing = self._consumptions.get(approval_id)
            if existing is not None:
                if (
                    existing.command_family_id == command_family_id
                    and existing.execution_fingerprint == execution_fingerprint
                ):
                    # Same command family + fingerprint replay — return
                    # the original consumption (NOT a second consume).
                    return existing
                # Different command family — cannot reuse the approval.
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: already consumed by command "
                    f"family {existing.command_family_id!r}, cannot reuse "
                    f"for {command_family_id!r}"
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
            # P0-1 R3: the request and decision BOTH bind to
            # ``approval_subject_hash`` (what the human approved), NOT
            # the final ``authorization_hash`` (which changes after the
            # decision is bound via _bind_approval).  This is the core
            # R3 fix: a forged post-decision authorization that
            # re-derives a new authorization_hash cannot reuse the
            # approval because the subject hash is stable
            # (base_authorization_hash + approval_id).
            subject_hash = authorization.approval_subject_hash
            if subject_hash is None:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: authorization has no "
                    f"approval_subject_hash (approval_id missing)"
                )
            if request.authorization_hash != subject_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: request.authorization_hash "
                    f"does not match approval_subject_hash"
                )
            if decision.authorization_hash != subject_hash:
                raise ApprovalValidationError(
                    f"approval {approval_id!r}: decision.authorization_hash "
                    f"does not match approval_subject_hash"
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

            # P0-1 R3: atomically consume + create consumption record
            # bound to command_family_id via approval_subject_hash.
            self._consumed.add(approval_id)
            consumption = ApprovalConsumptionRecord(
                approval_id=approval_id,
                decision_hash=decision.decision_hash,
                approval_subject_hash=subject_hash,
                authorization_hash=authorization.authorization_hash,
                command_family_id=command_family_id,
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
        * P0-1 R3: ``request.authorization_hash`` AND
          ``decision.authorization_hash`` BOTH match
          ``authorization.approval_subject_hash`` (what the human
          approved), NOT the final ``authorization.authorization_hash``
          (which changes after the decision is bound).
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
        # Identity checks FIRST (before hash checks) so a mismatched
        # proposal / tenant / authorization_id is reported clearly
        # rather than being masked by a subject-hash mismatch.
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
        # P0-1 R3: the request and decision bind to
        # ``approval_subject_hash`` (what the human approved), NOT the
        # final ``authorization_hash``.
        subject_hash = authorization.approval_subject_hash
        if subject_hash is None:
            raise ApprovalValidationError(
                "authorization has no approval_subject_hash (approval_id missing)",
            )
        if request.authorization_hash != subject_hash:
            raise ApprovalValidationError(
                "request.authorization_hash does not match approval_subject_hash",
            )
        if decision.authorization_hash != subject_hash:
            raise ApprovalValidationError(
                "decision.authorization_hash does not match approval_subject_hash",
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
