"""Phase 5B — Stable execution error codes and exception hierarchy.

All execution-path errors use these stable string codes (not free-form
strings) and typed exception classes.  This module is the single
source of truth for Phase 5B error semantics.

Design rules
------------

* Error codes are stable string constants — they enter receipts,
  trace events, and audit logs.  Renaming a code is a breaking change.
* Exception classes carry the ``error_code`` attribute so consumers
  can switch on it without parsing messages.
* Unknown-outcome errors use ``ExecutionUnknownOutcomeError`` and
  MUST NOT be retried automatically (Phase 5B Section 17).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Stable error codes — Phase 5B Section 26
# ---------------------------------------------------------------------------

EXECUTION_NOT_AUTHORIZED = "execution_not_authorized"
APPROVAL_REQUIRED = "approval_required"
APPROVAL_REJECTED = "approval_rejected"
APPROVAL_EXPIRED = "approval_expired"
APPROVAL_REVOKED = "approval_revoked"
APPROVAL_ALREADY_CONSUMED = "approval_already_consumed"
AUTHORIZATION_INTEGRITY_FAILED = "authorization_integrity_failed"
REVIEW_BINDING_MISMATCH = "review_binding_mismatch"
GOVERNANCE_SPEC_MISMATCH = "governance_spec_mismatch"
ADAPTER_NOT_FOUND = "adapter_not_found"
ADAPTER_VERSION_MISMATCH = "adapter_version_mismatch"
ACTION_NOT_SUPPORTED = "action_not_supported"
KILL_SWITCH_ACTIVE = "kill_switch_active"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
EXECUTION_ALREADY_IN_PROGRESS = "execution_already_in_progress"
EXECUTION_OUTCOME_UNKNOWN = "execution_outcome_unknown"
EXECUTION_TIMEOUT = "execution_timeout"
EXECUTION_CANCELLED = "execution_cancelled"
INVALID_ADAPTER_OUTCOME = "invalid_adapter_outcome"
INVALID_EXECUTION_RECEIPT = "invalid_execution_receipt"
TENANT_MISMATCH = "tenant_mismatch"
EXECUTION_DEADLINE_EXCEEDED = "execution_deadline_exceeded"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ExecutionError(Exception):
    """Base class for all Phase 5B execution errors.

    Every subclass carries a stable ``error_code`` string so audit
    consumers can switch on it without parsing messages.
    """

    error_code: str = "execution_error"

    def __init__(self, message: str, *, proposal_id: str | None = None) -> None:
        super().__init__(message)
        self.proposal_id = proposal_id


class ExecutionAuthorizationError(ExecutionError):
    """Authorization is missing, invalid, or fails integrity check."""

    error_code = AUTHORIZATION_INTEGRITY_FAILED


class ApprovalRequiredError(ExecutionError):
    """Proposal needs human approval before execution can proceed."""

    error_code = APPROVAL_REQUIRED


class ApprovalValidationError(ExecutionError):
    """Approval decision is invalid (wrong role, expired, revoked,
    already consumed, or hash mismatch)."""

    error_code = APPROVAL_REJECTED


class AdapterBindingError(ExecutionError):
    """Adapter is not found, version mismatch, or action not supported."""

    error_code = ADAPTER_NOT_FOUND


class IdempotencyConflictError(ExecutionError):
    """Same idempotency key with a different execution fingerprint."""

    error_code = IDEMPOTENCY_CONFLICT


class ExecutionAlreadyInProgressError(ExecutionError):
    """Same idempotency key is already IN_PROGRESS."""

    error_code = EXECUTION_ALREADY_IN_PROGRESS


class ExecutionTimeoutError(ExecutionError):
    """Adapter call exceeded the per-action timeout or batch deadline."""

    error_code = EXECUTION_TIMEOUT


class ExecutionUnknownOutcomeError(ExecutionError):
    """Adapter outcome could not be confirmed (timeout, cancellation,
    or connection loss before a definitive result).

    Phase 5B Section 17: UNKNOWN outcomes MUST NOT be automatically
    retried.  The idempotency record is marked UNKNOWN and requires
    human intervention.
    """

    error_code = EXECUTION_OUTCOME_UNKNOWN


class ExecutionReceiptError(ExecutionError):
    """Receipt fails integrity or cross-binding verification."""

    error_code = INVALID_EXECUTION_RECEIPT


class ExecutionIntegrityError(ExecutionError):
    """Review result, governance spec, or tenant identity mismatch."""

    error_code = REVIEW_BINDING_MISMATCH


class KillSwitchExecutionBlockedError(ExecutionError):
    """Kill Switch is active for the tenant, action type, adapter, or
    globally.  No Adapter call is permitted."""

    error_code = KILL_SWITCH_ACTIVE


# Re-export for convenience
__all__ = [
    # Error codes
    "EXECUTION_NOT_AUTHORIZED",
    "APPROVAL_REQUIRED",
    "APPROVAL_REJECTED",
    "APPROVAL_EXPIRED",
    "APPROVAL_REVOKED",
    "APPROVAL_ALREADY_CONSUMED",
    "AUTHORIZATION_INTEGRITY_FAILED",
    "REVIEW_BINDING_MISMATCH",
    "GOVERNANCE_SPEC_MISMATCH",
    "ADAPTER_NOT_FOUND",
    "ADAPTER_VERSION_MISMATCH",
    "ACTION_NOT_SUPPORTED",
    "KILL_SWITCH_ACTIVE",
    "IDEMPOTENCY_CONFLICT",
    "EXECUTION_ALREADY_IN_PROGRESS",
    "EXECUTION_OUTCOME_UNKNOWN",
    "EXECUTION_TIMEOUT",
    "EXECUTION_CANCELLED",
    "INVALID_ADAPTER_OUTCOME",
    "INVALID_EXECUTION_RECEIPT",
    "TENANT_MISMATCH",
    "EXECUTION_DEADLINE_EXCEEDED",
    # Exception classes
    "ExecutionError",
    "ExecutionAuthorizationError",
    "ApprovalRequiredError",
    "ApprovalValidationError",
    "AdapterBindingError",
    "IdempotencyConflictError",
    "ExecutionAlreadyInProgressError",
    "ExecutionTimeoutError",
    "ExecutionUnknownOutcomeError",
    "ExecutionReceiptError",
    "ExecutionIntegrityError",
    "KillSwitchExecutionBlockedError",
]
