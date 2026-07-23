"""Phase 5B — Human Approval Gate contracts.

Frozen, hash-stable contracts for the human approval loop that gates
execution of high-risk / approval-required Proposals.

Design rules
------------

* Every contract inherits :class:`StrictContract` with
  ``extra="forbid"`` and ``frozen=True``.
* ``ApprovalStatus`` is a closed :class:`StrEnum` so audit consumers
  can switch on stable string values.
* :class:`Clock` is a :class:`typing.Protocol` — no production code
  may call :func:`datetime.now` directly; the injected clock is the
  single source of wall-clock time so tests stay deterministic.
* Hash computation goes through :func:`stable_hash` so the hashes are
  stable across processes and ``PYTHONHASHSEED`` values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hmac import compare_digest
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.contracts import StrictContract
from multi_agent.execution_error_codes import ExecutionError
from multi_agent.review_contracts import ReviewRiskLevel
from multi_agent.serialization import stable_hash


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ApprovalStatus(StrEnum):
    """Lifecycle of a single approval request.

    ``NOT_REQUIRED`` — the Proposal does not need human approval
    (risk too low or governance spec ``always_needs_approval=False``).
    ``PENDING`` — request created, awaiting a decision.
    ``APPROVED`` / ``REJECTED`` — terminal decisions.
    ``EXPIRED`` — the deadline passed before a decision was made.
    ``REVOKED`` — an approver or admin cancelled a previously-granted
    approval; it can never be re-granted.
    ``CONSUMED`` — an APPROVED decision has been bound to exactly one
    ExecutionAuthorization; it can never be re-consumed.
    """

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


# Terminal statuses — once reached, the approval can no longer transition.
_TERMINAL_APPROVAL_STATUSES = frozenset(
    {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.REVOKED,
        ApprovalStatus.CONSUMED,
    }
)


def is_terminal_approval_status(status: ApprovalStatus) -> bool:
    """Return ``True`` if *status* cannot transition further."""
    return status in _TERMINAL_APPROVAL_STATUSES


# ---------------------------------------------------------------------------
# Clock — single source of wall-clock time (Phase 5B Section 5).
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Injectable wall-clock source.

    Production code MUST NOT call :func:`datetime.now` directly — it
    must read ``clock.now()`` so tests can supply a deterministic
    frozen clock and the approval expiry logic is reproducible.
    """

    def now(self) -> datetime: ...


class SystemClock:
    """Default :class:`Clock` backed by :func:`datetime.now` (UTC)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """Deterministic clock for tests — always returns the same instant."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        self._instant = instant.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._instant


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------


class ApprovalRequest(StrictContract):
    """Frozen, hash-stable request for a human approval decision.

    Binds a single approval to a specific :class:`ExecutionAuthorization`
    (via ``authorization_hash``) and to the originating Review
    (via ``review_request_hash`` / ``review_result_hash``).  The
    approver MUST hold one of ``required_approver_roles`` for the
    decision to be valid.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    authorization_id: str
    tenant_id: str
    run_id: str
    proposal_id: str
    review_request_hash: str
    review_result_hash: str
    authorization_hash: str
    risk_level: ReviewRiskLevel
    action_type: str
    action_summary: str
    required_approver_roles: tuple[str, ...] = ()
    requested_by: str
    requested_at: datetime
    expires_at: datetime | None = None
    approval_request_hash: str = ""

    @field_validator(
        "approval_id",
        "authorization_id",
        "tenant_id",
        "run_id",
        "proposal_id",
        "review_request_hash",
        "review_result_hash",
        "authorization_hash",
        "action_type",
        "action_summary",
        "requested_by",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ApprovalRequest identity fields must not be blank")
        return v

    @field_validator("required_approver_roles")
    @classmethod
    def _freeze_roles(cls, v: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            cleaned = tuple(sorted({str(r).strip() for r in v if str(r).strip()}))
            if not cleaned:
                raise ValueError(
                    "ApprovalRequest.required_approver_roles must not be empty"
                )
            return cleaned
        raise TypeError("required_approver_roles must be a list or tuple")

    @field_validator("requested_at")
    @classmethod
    def _requested_at_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ApprovalRequest.requested_at must be timezone-aware")
        return v.astimezone(timezone.utc)

    @field_validator("expires_at")
    @classmethod
    def _expires_at_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            raise ValueError("ApprovalRequest.expires_at must be timezone-aware")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _verify_approval_request_hash(self) -> ApprovalRequest:
        expected = self.compute_hash()
        if not self.approval_request_hash:
            object.__setattr__(self, "approval_request_hash", expected)
        elif not compare_digest(self.approval_request_hash, expected):
            raise ValueError(
                f"ApprovalRequest {self.approval_id!r}: stored approval_request_hash "
                f"{self.approval_request_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    def compute_hash(self) -> str:
        """Stable SHA-256 over the canonical request content.

        Excludes ``approval_request_hash`` (self-referential).
        """
        return stable_hash(self, exclude={"approval_request_hash"})

    def verify_integrity(self) -> None:
        """Recompute and compare ``approval_request_hash``."""
        if not compare_digest(self.approval_request_hash, self.compute_hash()):
            raise ValueError(
                f"ApprovalRequest {self.approval_id!r}: approval_request_hash "
                f"does not match recomputed content"
            )

    def is_expired(self, now: datetime) -> bool:
        """Return ``True`` if ``expires_at`` is set and *now* is past it."""
        if self.expires_at is None:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) > self.expires_at


# ---------------------------------------------------------------------------
# ApprovalDecision
# ---------------------------------------------------------------------------


class ApprovalDecision(StrictContract):
    """A human approver's terminal decision on an :class:`ApprovalRequest`.

    ``status`` is one of APPROVED / REJECTED / EXPIRED / REVOKED /
    CONSUMED.  ``decision_hash`` covers every field so a tampered
    decision is detected at the boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    status: ApprovalStatus
    approver_id: str
    approver_roles: tuple[str, ...] = ()
    decision_reason: str
    decided_at: datetime
    approval_request_hash: str
    authorization_hash: str
    decision_hash: str = ""

    @field_validator(
        "approval_id",
        "approver_id",
        "decision_reason",
        "approval_request_hash",
        "authorization_hash",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ApprovalDecision identity fields must not be blank")
        return v

    @field_validator("approver_roles")
    @classmethod
    def _freeze_approver_roles(cls, v: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(sorted({str(r).strip() for r in v if str(r).strip()}))
        raise TypeError("approver_roles must be a list or tuple")

    @field_validator("decided_at")
    @classmethod
    def _decided_at_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ApprovalDecision.decided_at must be timezone-aware")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _verify_decision_hash(self) -> ApprovalDecision:
        expected = self.compute_hash()
        if not self.decision_hash:
            object.__setattr__(self, "decision_hash", expected)
        elif not compare_digest(self.decision_hash, expected):
            raise ValueError(
                f"ApprovalDecision {self.approval_id!r}: stored decision_hash "
                f"{self.decision_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    def compute_hash(self) -> str:
        """Stable SHA-256 over the canonical decision content.

        Excludes ``decision_hash`` (self-referential).
        """
        return stable_hash(self, exclude={"decision_hash"})

    def verify_integrity(self) -> None:
        """Recompute and compare ``decision_hash``."""
        if not compare_digest(self.decision_hash, self.compute_hash()):
            raise ValueError(
                f"ApprovalDecision {self.approval_id!r}: decision_hash does not "
                f"match recomputed content"
            )


# ---------------------------------------------------------------------------
# ApprovalConflictError — same approval_id with a different request hash (P1-1).
# ---------------------------------------------------------------------------


class ApprovalConflictError(ExecutionError):
    """Same ``approval_id`` was used with a different ``approval_request_hash``.

    Raised by :meth:`ApprovalStore.create` when an existing request with
    the same ``approval_id`` has a different ``approval_request_hash``
    (P1-1: same ID + different hash → conflict, NOT silent return).
    """

    error_code = "approval_conflict"


__all__ = [
    "ApprovalConflictError",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStatus",
    "Clock",
    "FrozenClock",
    "SystemClock",
    "is_terminal_approval_status",
]
