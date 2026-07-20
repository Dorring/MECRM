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
    "RetryableAgentError",
    "RunAlreadyInProgressError",
    "RunPlanConflictError",
    "SupervisorError",
]
