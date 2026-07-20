"""Phase 4 execution contracts, context builder, result validator, and
cancellation protocol.

This module is the **single source of truth** for Phase 4 data shapes:

* :class:`SupervisorRunStatus` — Run lifecycle states.
* :class:`TaskAttemptRecord` / :class:`TaskExecutionRecord` — per-task
  execution history (no prompts, no chain-of-thought, no secrets).
* :class:`ExecutionTraceEvent` — ordered audit events.
* :class:`SupervisorRunResult` — final Run output.
* :class:`SupervisorConfig` — Runtime knobs.
* :class:`ExecutionCancellation` — Kill Switch / cancel Protocol.

Plus two pure helpers:

* :func:`build_execution_context` — constructs an
  :class:`AgentExecutionContext` from a :class:`PlanDraft` and a single
  :class:`AgentTask`.  The context's ``tenant_id`` / ``actor_id`` /
  ``roles`` / ``scopes`` are sourced from the plan and **cannot** be
  overridden by the task or the handler.
* :func:`validate_agent_result` — boundary check that every
  :class:`AgentResult` must pass before entering
  :func:`merge_parallel_results`.

Design notes
------------

``duration_ms`` on :class:`TaskAttemptRecord` is computed from
``time.monotonic()``; ``datetime`` fields are for audit display only
and never participate in a deterministic hash.

``ExecutionCancellation`` is deliberately a Protocol — Phase 4 must
not bind directly to the production Redis-backed Kill Switch.  Tests
inject :class:`FakeExecutionCancellation`; production wiring is a
Phase 5 concern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator

from multi_agent.contracts import (
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ExecutionUsage,
    JsonValue,
    StrictContract,
)
from multi_agent.errors import ProposalHashMismatchError
from multi_agent.execution_errors import InvalidAgentResultError
from multi_agent.planning import PlanDraft
from multi_agent.state import MergedState


# ---------------------------------------------------------------------------
# Run status
# ---------------------------------------------------------------------------


class SupervisorRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_SUCCESS = "partial_success"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


# ---------------------------------------------------------------------------
# Attempt / Task records
# ---------------------------------------------------------------------------


_TaskAttemptStatus = Literal[
    "running",
    "completed",
    "failed",
    "needs_input",
    "timed_out",
    "cancelled",
]


class TaskAttemptRecord(StrictContract):
    """One Handler invocation for a task.

    ``duration_ms`` is monotonic-clock derived.  ``started_at`` /
    ``completed_at`` are timezone-aware UTC for audit display only.

    The record never stores prompts, chain-of-thought, or secrets.
    """

    task_id: str
    agent_id: str
    attempt: int = Field(ge=0)

    started_at: datetime
    completed_at: datetime | None = None

    status: _TaskAttemptStatus

    duration_ms: int | None = None
    error_code: str | None = None

    agent_calls: int = Field(default=1, ge=1)
    tool_calls: int = Field(default=0, ge=0)
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_aware_attempt(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
        return v


_TaskExecutionStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "needs_input",
    "skipped",
    "cancelled",
]


class TaskExecutionRecord(StrictContract):
    """Aggregated execution state for a single task across all attempts."""

    task_id: str
    agent_id: str

    status: _TaskExecutionStatus

    attempts: list[TaskAttemptRecord] = Field(default_factory=list)
    result: AgentResult | None = None

    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


# Stable event-type strings.  These are the only values that may
# appear in ``ExecutionTraceEvent.event_type``.
TRACE_RUN_STARTED = "run_started"
TRACE_PLAN_VALIDATED = "plan_validated"
TRACE_TASK_READY = "task_ready"
TRACE_TASK_STARTED = "task_started"
TRACE_TASK_RETRYING = "task_retrying"
TRACE_TASK_COMPLETED = "task_completed"
TRACE_TASK_FAILED = "task_failed"
TRACE_TASK_NEEDS_INPUT = "task_needs_input"
TRACE_TASK_TIMED_OUT = "task_timed_out"
TRACE_TASK_SKIPPED = "task_skipped"
TRACE_BUDGET_EXCEEDED = "budget_exceeded"
TRACE_RUN_CANCELLED = "run_cancelled"
TRACE_RESULTS_MERGED = "results_merged"
TRACE_RUN_COMPLETED = "run_completed"


class ExecutionTraceEvent(StrictContract):
    """Ordered audit event.

    ``sequence`` is a strictly-increasing integer assigned by the
    Supervisor.  ``occurred_at`` is timezone-aware UTC for display.

    ``data`` carries event-specific payload (e.g. attempt number,
    error code).  It must not contain prompts, chain-of-thought, or
    secrets — the same sensitive-key rejection that applies to
    contract metadata applies here.
    """

    sequence: int = Field(ge=0)
    event_type: str

    run_id: str
    task_id: str | None = None
    agent_id: str | None = None

    occurred_at: datetime
    data: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _utc_aware_trace(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        return v


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


class SupervisorRunResult(StrictContract):
    """Final output of a :class:`SupervisorRuntime.execute` call."""

    run_id: str
    plan_hash: str
    registry_version: str

    status: SupervisorRunStatus

    task_records: list[TaskExecutionRecord] = Field(default_factory=list)
    merged_state: MergedState

    usage: ExecutionUsage
    trace: list[ExecutionTraceEvent] = Field(default_factory=list)

    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_aware_result(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
        return v


class SupervisorConfig(StrictContract):
    """Runtime knobs.

    Defaults are deterministic-friendly: ``retry_backoff_ms=0`` so the
    same plan + fake handler produces a repeatable trace.

    R1 P1: the previous ``continue_independent_branches`` and
    ``deterministic_mode`` fields were removed because they were never
    read by the Scheduler or Supervisor.  ``continue_independent_branches``
    in particular would conflict with the Scheduler's documented
    contract ("independent branches continue" is the *only* supported
    behaviour — see :class:`DagScheduler`).  Passing either keyword
    now raises ``ValidationError`` thanks to ``extra='forbid'``.
    """

    max_concurrency: int = Field(default=4, ge=1, le=32)
    retry_backoff_ms: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Cancellation Protocol
# ---------------------------------------------------------------------------


class ExecutionCancellation(Protocol):
    """Cancel / Kill Switch boundary.

    Phase 4 does not bind to the production Redis-backed
    :class:`governance.kill_switch.AgentKillSwitch`; production wiring
    is a Phase 5 concern.  Tests inject a fake implementation.

    Both methods are async so a future Redis-backed adapter can poll
    the real switch without changing the call sites.
    """

    async def is_cancelled(self, run_id: str) -> bool: ...

    async def is_kill_switch_active(self, tenant_id: str) -> bool: ...


class FakeExecutionCancellation:
    """In-memory cancellation source for tests.

    Starts inactive; tests flip ``cancelled_runs`` /
    ``kill_switch_tenants`` to simulate a cancel or kill-switch event.
    """

    def __init__(self) -> None:
        self.cancelled_runs: set[str] = set()
        self.kill_switch_tenants: set[str] = set()

    def cancel_run(self, run_id: str) -> None:
        self.cancelled_runs.add(run_id)

    def activate_kill_switch(self, tenant_id: str) -> None:
        self.kill_switch_tenants.add(tenant_id)

    async def is_cancelled(self, run_id: str) -> bool:
        return run_id in self.cancelled_runs

    async def is_kill_switch_active(self, tenant_id: str) -> bool:
        return tenant_id in self.kill_switch_tenants


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def build_execution_context(
    plan: PlanDraft,
    task: AgentTask,
    *,
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    policy_context: dict[str, JsonValue] | None = None,
) -> AgentExecutionContext:
    """Construct a fresh :class:`AgentExecutionContext` for *task*.

    Identity fields (``tenant_id``, ``actor_id``, ``roles``, ``scopes``)
    are sourced from the :class:`PlanDraft` and **cannot** be overridden
    by ``task.input_data`` or by the handler.  ``run_id`` / ``task_id``
    / ``actor_type`` / ``actor_id`` are placed in ``run_metadata``
    because :class:`AgentExecutionContext` (Phase 2 contract) does not
    carry dedicated fields for them and Phase 4 must not modify
    ``contracts.py``.

    Each call returns an independent context so one task's handler
    cannot mutate another task's context.
    """
    request = plan.request
    run_metadata: dict[str, JsonValue] = {
        "run_id": plan.run_id,
        "task_id": task.task_id,
        "actor_type": request.actor_type,
        "actor_id": request.actor_id,
    }
    return AgentExecutionContext(
        tenant_id=plan.tenant_id,
        user_id=request.actor_id if request.actor_type == "user" else None,
        roles=list(roles or []),
        scopes=list(scopes or []),
        policy_context=dict(policy_context or {}),
        correlation_id=plan.run_id,
        parent_task_id=None,
        run_metadata=run_metadata,
    )


# ---------------------------------------------------------------------------
# Result validator
# ---------------------------------------------------------------------------


_ALLOWED_RESULT_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "degraded", "cancelled", "needs_input", "skipped"}
)


def validate_agent_result(
    result: AgentResult,
    *,
    task: AgentTask,
    plan: PlanDraft,
) -> None:
    """Boundary check that *result* must pass before Merge.

    Raises :class:`InvalidAgentResultError` on any mismatch.  The
    check is defensive: :class:`AgentResult` already enforces tenant
    homogeneity and proposal ``created_by_agent`` at construction, but
    Phase 4 re-verifies the invariants at the execution boundary so a
    tampered or buggy handler cannot slip a foreign-tenant result past
    the Supervisor.

    R1 P0-5: the check also re-verifies that every
    ``action_proposals[*].evidence_ids`` references an evidence_id
    that still exists in ``result.evidence``.  ``AgentResult`` validates
    this at construction, but list fields can be mutated in place
    after construction (e.g. ``result.evidence.clear()``); without
    re-validation a tampered result with dangling evidence_ids would
    pass the Supervisor boundary and only be excluded later by
    Phase 2 Merge, leaving the Task marked ``completed``.

    Checks:

    * ``result.task_id == task.task_id``
    * ``result.agent_id == task.agent_id``
    * ``result.tenant_id == plan.tenant_id``
    * ``result.status`` is one of the allowed literals
    * every ``action_proposals[*].proposal_hash`` verifies
    * every ``action_proposals[*].created_by_agent == task.agent_id``
    * every ``action_proposals[*].tenant_id == plan.tenant_id``
    * every ``action_proposals[*].evidence_ids`` references an
      evidence_id present in ``result.evidence``
    * every ``evidence[*].tenant_id == plan.tenant_id``
    """
    if result.task_id != task.task_id:
        raise InvalidAgentResultError(
            f"result.task_id={result.task_id!r} != task.task_id={task.task_id!r}"
        )
    if result.agent_id != task.agent_id:
        raise InvalidAgentResultError(
            f"result.agent_id={result.agent_id!r} != task.agent_id={task.agent_id!r}"
        )
    if result.tenant_id != plan.tenant_id:
        raise InvalidAgentResultError(
            f"result.tenant_id={result.tenant_id!r} != plan.tenant_id={plan.tenant_id!r}"
        )
    if result.status not in _ALLOWED_RESULT_STATUSES:
        raise InvalidAgentResultError(
            f"result.status={result.status!r} is not an allowed value"
        )

    known_evidence_ids = {ev.evidence_id for ev in result.evidence}

    for proposal in result.action_proposals:
        if proposal.created_by_agent != task.agent_id:
            raise InvalidAgentResultError(
                f"proposal {proposal.proposal_id!r} created_by_agent="
                f"{proposal.created_by_agent!r} != task.agent_id={task.agent_id!r}"
            )
        if proposal.tenant_id != plan.tenant_id:
            raise InvalidAgentResultError(
                f"proposal {proposal.proposal_id!r} tenant_id="
                f"{proposal.tenant_id!r} != plan.tenant_id={plan.tenant_id!r}"
            )
        # R1 P0-5: re-validate evidence_ids against the current
        # ``result.evidence`` set.  ``AgentResult.__init__`` validates
        # this once at construction but the list can be mutated
        # afterward.
        missing_evidence = [
            eid for eid in proposal.evidence_ids if eid not in known_evidence_ids
        ]
        if missing_evidence:
            raise InvalidAgentResultError(
                f"proposal {proposal.proposal_id!r} references missing "
                f"evidence_ids={missing_evidence!r}"
            )
        try:
            proposal.verify_integrity()
        except (ProposalHashMismatchError, ValueError, TypeError) as exc:
            raise InvalidAgentResultError(
                f"proposal {proposal.proposal_id!r} failed integrity check: {exc}"
            ) from exc

    for evidence in result.evidence:
        if evidence.tenant_id != plan.tenant_id:
            raise InvalidAgentResultError(
                f"evidence {evidence.evidence_id!r} tenant_id="
                f"{evidence.tenant_id!r} != plan.tenant_id={plan.tenant_id!r}"
            )


# ---------------------------------------------------------------------------
# Final-status priority
# ---------------------------------------------------------------------------


# Lower index = higher priority.  Used to pick the final Run status when
# multiple terminal task statuses coexist (e.g. one failed + one
# completed → partial_success; a cancelled task overrides everything
# else when the run was cancelled).
_FINAL_STATUS_PRIORITY: dict[SupervisorRunStatus, int] = {
    SupervisorRunStatus.CANCELLED: 0,
    SupervisorRunStatus.BUDGET_EXCEEDED: 1,
    SupervisorRunStatus.FAILED: 2,
    SupervisorRunStatus.NEEDS_INPUT: 3,
    SupervisorRunStatus.PARTIAL_SUCCESS: 4,
    SupervisorRunStatus.COMPLETED: 5,
}


def final_status_priority(status: SupervisorRunStatus) -> int:
    """Return the priority rank of *status* (lower = higher priority).

    Unknown statuses (e.g. ``PENDING`` / ``RUNNING``) rank below every
    terminal status so they never win the final-status election.
    """
    return _FINAL_STATUS_PRIORITY.get(status, len(_FINAL_STATUS_PRIORITY))


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime.

    Centralised so tests can monkeypatch a fixed clock.
    """
    return datetime.now(timezone.utc)


__all__ = [
    "ExecutionCancellation",
    "ExecutionTraceEvent",
    "FakeExecutionCancellation",
    "SupervisorConfig",
    "SupervisorRunResult",
    "SupervisorRunStatus",
    "TaskAttemptRecord",
    "TaskExecutionRecord",
    "TRACE_BUDGET_EXCEEDED",
    "TRACE_PLAN_VALIDATED",
    "TRACE_RESULTS_MERGED",
    "TRACE_RUN_CANCELLED",
    "TRACE_RUN_COMPLETED",
    "TRACE_RUN_STARTED",
    "TRACE_TASK_COMPLETED",
    "TRACE_TASK_FAILED",
    "TRACE_TASK_NEEDS_INPUT",
    "TRACE_TASK_READY",
    "TRACE_TASK_RETRYING",
    "TRACE_TASK_SKIPPED",
    "TRACE_TASK_STARTED",
    "TRACE_TASK_TIMED_OUT",
    "build_execution_context",
    "final_status_priority",
    "utc_now",
    "validate_agent_result",
]
