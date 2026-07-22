"""Phase 4 Supervisor Runtime errors.

All errors inherit :class:`multi_agent.errors.MultiAgentError` so the
existing error hierarchy stays consistent and callers can catch the
base class to handle any multi-agent failure.
"""

from __future__ import annotations

from multi_agent.errors import MultiAgentError


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class SupervisorError(MultiAgentError):
    """Base for all Phase 4 Supervisor Runtime errors."""


# ---------------------------------------------------------------------------
# Retry / Invocation
# ---------------------------------------------------------------------------


class RetryableAgentError(SupervisorError):
    """Raised by a Handler when the failure is transient and the task
    may be retried within the remaining retry budget.

    Distinct from ``AgentError.retryable=True`` (which lives on the
    :class:`AgentResult` and is inspected after a *successful* Handler
    return).  ``RetryableAgentError`` is raised *before* a result is
    produced and triggers the Retry loop directly.
    """


class NonRetryableAgentError(SupervisorError):
    """R3 P0-2: Raised by a Handler when the failure is a definite
    business-domain error that cannot be retried, but is still an
    *expected* Agent Domain Error rather than a programming error.

    The task is marked ``failed`` with ``error_code=non_retryable_error``
    and the run continues — siblings are NOT cancelled (contrast with
    an unknown ``RuntimeError`` / ``TypeError`` which propagates to
    the Scheduler's structured-concurrency boundary).

    Rationale: R2 used a broad ``except Exception`` catch-all that
    downgraded every unknown error (including programming bugs) to a
    plain task failure.  R3 splits the boundary — Handlers that want
    a non-retryable task failure must raise this explicit type so
    generic programming errors are no longer silently swallowed.
    """


# ---------------------------------------------------------------------------
# Result validation
# ---------------------------------------------------------------------------


class InvalidAgentResultError(SupervisorError):
    """An :class:`AgentResult` failed Phase 4 boundary validation
    (task_id / agent_id / tenant_id / proposal hash mismatch).

    The result must not enter ``merged_state``; the task is marked
    ``failed``.  Unless the underlying error is explicitly retryable,
    the task is not retried.
    """


class InvalidInvocationReceiptError(SupervisorError):
    """R1 P0-4: An :class:`AgentInvocationReceipt` failed consistency
    validation.

    The receipt reported a ``tool_calls`` count that does not equal
    ``len(result.tool_calls)``, or a ``tokens_used`` value that does
    not match ``result.token_usage.total_tokens`` when
    ``provider_metadata`` is present.

    A mismatched receipt is treated as a *non-retryable* Task failure:
    the Task is marked ``failed``, the result does not enter Merge,
    and the error_code is ``invalid_receipt``.  This prevents a custom
    AgentInvoker from under-reporting usage to bypass budget
    enforcement.
    """


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class ExecutionUsageUnavailableError(SupervisorError):
    """Fail-closed signal: a token/cost budget is configured but the
    :class:`AgentInvocationReceipt` did not report actual usage.

    Phase 4 forbids using Phase 3 *estimates* as a substitute for
    actual usage, so a configured budget with no reported usage must
    fail closed rather than silently passing.
    """


# ---------------------------------------------------------------------------
# Run idempotency
# ---------------------------------------------------------------------------


class RunPlanConflictError(SupervisorError):
    """A run with the same ``run_id`` but a different ``plan_hash`` is
    already completed or in progress.

    Phase 4 Run Idempotency requires that the same ``run_id`` is bound
    to exactly one ``plan_hash``; a conflicting plan cannot reuse the
    run.
    """


class RunAlreadyInProgressError(SupervisorError):
    """A run with the same ``run_id`` is currently executing.

    A second ``SupervisorRuntime.execute`` call for the same ``run_id``
    must be rejected to prevent duplicate Handler invocations.
    """


__all__ = [
    "ExecutionUsageUnavailableError",
    "InvalidAgentResultError",
    "InvalidInvocationReceiptError",
    "NonRetryableAgentError",
    "RetryableAgentError",
    "RunAlreadyInProgressError",
    "RunPlanConflictError",
    "SupervisorError",
]
