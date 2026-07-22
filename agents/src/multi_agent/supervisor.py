"""Phase 4 Supervisor Runtime.

The Supervisor is the single entry point that turns a validated
:class:`PlanDraft` into a :class:`SupervisorRunResult`.  It owns:

* Pre-flight validation (plan integrity, registry version, plan
  validator, run idempotency lease, handler resolution).
* Actual-budget accounting — every Attempt counts toward
  ``max_agent_calls`` / ``max_tool_calls`` / ``token_budget`` /
  ``cost_budget_usd``.  Phase 3 *estimates* are never used as a
  substitute for actual usage.
* Retry + Timeout — a single ``run_task`` closure handed to
  :class:`DagScheduler`.
* Result validation via :func:`validate_agent_result` before any result
  enters :func:`merge_parallel_results`.
* Final-status election via :func:`final_status_priority`.
* Trace emission via :class:`ExecutionTraceEvent`.

The Supervisor does **not** execute :class:`ActionProposal` — it only
collects and validates them.  Execution is a Phase 5 concern.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, Sequence

from multi_agent.contracts import (
    AgentError,
    AgentErrorCategory,
    AgentResult,
    AgentTask,
    ExecutionBudget,
    ExecutionUsage,
    UsageAvailabilityStatus,
)
from multi_agent.planning import (
    NEVER_RETRYABLE_ERROR_CODES,
    PlannedTask,
    RetryPolicy,
)
from multi_agent.execution import (
    ExecutionBinding,
    ExecutionCancellation,
    ExecutionTraceEvent,
    FakeExecutionCancellation,
    SupervisorConfig,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TaskExecutionRecord,
    TRACE_BUDGET_EXCEEDED,
    TRACE_PLAN_VALIDATED,
    TRACE_RESULTS_MERGED,
    TRACE_RUN_CANCELLED,
    TRACE_RUN_COMPLETED,
    TRACE_RUN_STARTED,
    TRACE_TASK_COMPLETED,
    TRACE_TASK_FAILED,
    TRACE_TASK_NEEDS_INPUT,
    TRACE_TASK_READY,
    TRACE_TASK_RETRYING,
    TRACE_TASK_SKIPPED,
    TRACE_TASK_STARTED,
    TRACE_TASK_TIMED_OUT,
    build_execution_context,
    final_status_priority,
    utc_now,
    validate_agent_result,
)
from multi_agent.execution_errors import (
    ExecutionUsageUnavailableError,
    InvalidAgentResultError,
    InvalidInvocationReceiptError,
    NonRetryableAgentError,
    RetryableAgentError,
    RunAlreadyInProgressError,
    RunPlanConflictError,
    SupervisorError,
)
from multi_agent.invocation import (
    AgentInvocationFailure,
    AgentInvocationOutcome,
    AgentInvocationReceipt,
    AgentInvoker,
    AttemptUsageDisposition,
    AttemptUsageRecord,
    UsageVerificationCapabilities,
    get_usage_capabilities,
    validate_invocation_receipt,
)
from multi_agent.usage import (
    ERROR_EXECUTION_USAGE_UNAVAILABLE,
    ERROR_INFRASTRUCTURE_EXCEPTION,
    ERROR_TOOL_USAGE_UNAVAILABLE,
)
from multi_agent.plan_validator import PlanValidator
from multi_agent.planning import PlanDraft
from multi_agent.registry import AgentHandler, AgentRegistry
from multi_agent.run_store import InMemoryRunStore, RunLease, RunStore
from multi_agent.scheduler import (
    AgentCallPermit,
    DagScheduler,
    DispatchDecision,
    PreDispatch,
    TaskOutcome,
)
from multi_agent.state import merge_parallel_results


# ---------------------------------------------------------------------------
# R6 P0-2: Executable RetryPolicy — pure retry decision function
# ---------------------------------------------------------------------------


# R6 P1: ``NEVER_RETRYABLE_ERROR_CODES`` is now imported from
# ``planning.py`` so the planning-layer RetryPolicy validator and the
# runtime ``should_retry()`` function share the same canonical
# definition.  See ``multi_agent.planning.NEVER_RETRYABLE_ERROR_CODES``.

# Default safe categories retried when ``retryable_error_codes`` is empty.
_DEFAULT_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"task_timeout", "retryable_error"}
)


def should_retry(
    *,
    policy: RetryPolicy,
    attempt_index: int,
    error_code: str | None,
    explicitly_retryable: bool,
) -> bool:
    """R6 P0-2: Pure function that decides whether a failed attempt
    should be retried based on the task's :class:`RetryPolicy`.

    Semantics:

    * ``max_retries == 0`` → never retry.
    * ``attempt_index >= max_retries`` → budget exhausted, no retry.
    * Error codes in :data:`NEVER_RETRYABLE_ERROR_CODES` → never retry,
      even if the code appears in ``retryable_error_codes``.  This
      prevents ``NonRetryableAgentError`` (``non_retryable_error``)
      from becoming retryable just because its code was listed.
    * ``retryable_error_codes`` non-empty → ``error_code`` must be in
      the set (and not in the never-retry list).
    * ``retryable_error_codes`` empty → only default safe categories
      (``task_timeout``, ``retryable_error``) and
      ``explicitly_retryable=True`` (from ``AgentResult.errors[].retryable``)
      are retried.
    """
    if policy.max_retries <= 0:
        return False
    if attempt_index >= policy.max_retries:
        return False

    if error_code is not None and error_code in NEVER_RETRYABLE_ERROR_CODES:
        return False

    if policy.retryable_error_codes:
        if error_code is None:
            return False
        return error_code in policy.retryable_error_codes

    # Empty retryable_error_codes → default safe categories.
    if error_code is not None and error_code in _DEFAULT_RETRYABLE_ERROR_CODES:
        return True
    if explicitly_retryable:
        return True
    return False


def should_retry_result(
    *,
    policy: RetryPolicy,
    attempt_index: int,
    errors: Sequence[AgentError],
) -> bool:
    """R7 P0-4: Pure function that decides whether a failed attempt
    should be retried based on the task's :class:`RetryPolicy` and the
    FULL list of :class:`AgentError` records from the result.

    This replaces the R6 pattern of passing
    ``error_code=errors[0].error_code`` and
    ``explicitly_retryable=any(e.retryable for e in errors)`` — which
    could combine the ``error_code`` from one error with the
    ``retryable`` flag from a DIFFERENT error, leading to incorrect
    retry decisions.

    Decision rules (in priority order):

    1. ``policy.max_retries <= 0`` → ``False``
    2. ``attempt_index >= policy.max_retries`` → ``False``
    3. No errors → ``False`` (can't determine retryability)
    4. ANY error with a code in :data:`NEVER_RETRYABLE_ERROR_CODES`
       → ``False`` (fail-closed — identity, cancellation, usage, and
       receipt errors are never retried regardless of the
       ``retryable`` flag on any error)
    5. Filter to errors where ``retryable=True`` — the ``error_code``
       and ``retryable`` flag MUST come from the SAME
       :class:`AgentError`.
    6. No retryable errors → ``False``
    7. ``retryable_error_codes`` non-empty → retry only if at least
       one retryable error's ``error_code`` is in the allowlist.
    8. ``retryable_error_codes`` empty → retry (at least one retryable
       error is enough).
    """
    if policy.max_retries <= 0:
        return False
    if attempt_index >= policy.max_retries:
        return False

    if not errors:
        return False

    # Step 4: never-retryable codes always block retry, regardless of
    # the retryable flag on any error.  This ensures that a
    # non-retryable error in the list cannot be "borrowed" over by a
    # retryable error.
    for err in errors:
        if err.error_code in NEVER_RETRYABLE_ERROR_CODES:
            return False

    # Step 5: consider only errors where retryable=True — error_code
    # and retryable must come from the SAME AgentError.
    retryable_errors = [err for err in errors if err.retryable]
    if not retryable_errors:
        return False

    # Step 7: allowlist non-empty → at least one retryable error's
    # code must be in the allowlist.
    if policy.retryable_error_codes:
        return any(
            err.error_code in policy.retryable_error_codes for err in retryable_errors
        )

    # Step 8: empty allowlist → at least one retryable error is enough.
    return True


# ---------------------------------------------------------------------------
# Trace builder
# ---------------------------------------------------------------------------


class _TraceBuilder:
    """Assigns sequential ``sequence`` numbers to trace events."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._events: list[ExecutionTraceEvent] = []
        self._seq = 0

    def emit(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        data: dict[str, Any] | None = None,
        occurred_at: Any | None = None,
    ) -> ExecutionTraceEvent:
        ts = occurred_at if occurred_at is not None else utc_now()
        event = ExecutionTraceEvent(
            sequence=self._seq,
            event_type=event_type,
            run_id=self._run_id,
            task_id=task_id,
            agent_id=agent_id,
            occurred_at=ts,
            data=dict(data or {}),
        )
        self._seq += 1
        self._events.append(event)
        return event

    @property
    def events(self) -> list[ExecutionTraceEvent]:
        return list(self._events)


# ---------------------------------------------------------------------------
# Mutable cancellation state — shared between run_task and should_stop
# ---------------------------------------------------------------------------


class _CancellationState:
    """Sync flag set by ``run_task`` when the async cancellation
    Protocol reports an active cancel / kill switch.

    The Scheduler's ``should_stop`` is synchronous, so it cannot
    ``await`` the Protocol directly.  Instead, ``run_task`` performs
    the async check before each attempt and flips this flag; the next
    ``should_stop`` call observes the flip and stops dispatching.
    """

    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled: bool = False


# ---------------------------------------------------------------------------
# Actual-budget accountant
# ---------------------------------------------------------------------------


class _BudgetAccountant:
    """Tracks actual usage against an :class:`ExecutionBudget`.

    Fail-closed rules:

    * ``max_tasks`` — checked once before scheduling.
    * ``max_agent_calls`` — incremented *before* every Attempt.
    * ``max_tool_calls`` — incremented *after* every successful
      Attempt using ``receipt.tool_calls``.
    * ``max_iterations`` — R1 P0-2: incremented *before* a real Ready
      Task wave is dispatched.  Skip propagation and cleanup waves do
      NOT consume an iteration.
    * ``deadline_ms`` — R1 P0-3: checked via ``time.monotonic()``.
      Deadline exhaustion sets ``exceeded=True`` with reason
      ``deadline_exceeded`` so the Run finalises as
      ``budget_exceeded`` rather than ``failed``.
    * ``token_budget`` / ``cost_budget_usd`` — fail-closed when
      configured but the receipt reports ``None`` usage.
    """

    def __init__(self, budget: ExecutionBudget, *, start_monotonic: float) -> None:
        self._budget = budget
        self._start_monotonic = start_monotonic
        self._agent_calls = 0
        self._permits_outstanding = (
            0  # R5 P0-3: permits issued but not committed/released
        )
        self._permit_counter = 0  # R5 P0-3: monotonic permit sequence
        self._tool_calls = 0
        self._tokens_used = 0
        self._tokens_usage_available = False
        self._cost_usd = Decimal("0.00")
        self._cost_usage_available = False
        self._iterations = 0
        self._exceeded: bool = False
        self._exceeded_reason: str | None = None
        # R6 P0-3: fail-closed flag for no-receipt attempts with budget.
        self._usage_unavailable: bool = False
        # R9 Section 2: fail-closed flag for unknown tool-call counts.
        # Set when ``record_observed_tool_calls(None)`` is called —
        # the Runtime cannot account for tool calls that may have
        # been made before a timeout/exception.
        self._tool_usage_unavailable: bool = False
        # R7 P0-3: Token and Cost now have INDEPENDENT coverage
        # denominators.  An attempt is "applicable" for a dimension
        # when its disposition is NOT ``NO_PROVIDER_CALL`` — i.e. a
        # provider call was made OR we can't prove it wasn't.  Only
        # pure deterministic invokers (``never_calls_provider=True``,
        # R9 Section 3) can produce ``NO_PROVIDER_CALL`` dispositions.
        self._token_usage_applicable_attempts = 0
        self._cost_usage_applicable_attempts = 0
        self._verified_token_attempts = 0
        self._verified_cost_attempts = 0
        # R7 P0-3: per-attempt records for auditability.  Each
        # committed agent call produces exactly one
        # :class:`AttemptUsageRecord` stored here.
        self._attempt_records: list[AttemptUsageRecord] = []

    @property
    def agent_calls(self) -> int:
        return self._agent_calls

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def tokens_usage_available(self) -> bool:
        return self._tokens_usage_available

    @property
    def cost_usd(self) -> Decimal:
        return self._cost_usd

    @property
    def cost_usage_available(self) -> bool:
        return self._cost_usage_available

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def exceeded(self) -> bool:
        return self._exceeded

    @property
    def exceeded_reason(self) -> str | None:
        return self._exceeded_reason

    @property
    def usage_unavailable(self) -> bool:
        """R6 P0-3: True when a committed attempt produced no receipt
        and a token/cost budget is configured."""
        return self._usage_unavailable

    @property
    def tool_usage_unavailable(self) -> bool:
        """R9 Section 2: True when a committed attempt's tool-call
        count is unknown (timeout/exception before any receipt)."""
        return self._tool_usage_unavailable

    @property
    def last_attempt_record(self) -> AttemptUsageRecord | None:
        """R8 P0-5: return the most recently committed
        :class:`AttemptUsageRecord`, or ``None`` when no record has
        been committed yet.

        Used by :meth:`_execute_task` to retrieve the explicit
        dispositions and source ids that were frozen during the atomic
        commit, so the :class:`TaskAttemptRecord` audit fields stay
        consistent with the Accountant's internal record without
        duplicating the disposition decision logic.
        """
        return self._attempt_records[-1] if self._attempt_records else None

    def _compute_usage_status(
        self, verified: int, capable: int
    ) -> UsageAvailabilityStatus:
        """R6 P1: compute three-state usage availability."""
        if capable == 0:
            return UsageAvailabilityStatus.COMPLETE
        if verified == capable:
            return UsageAvailabilityStatus.COMPLETE
        if verified > 0:
            return UsageAvailabilityStatus.PARTIAL
        return UsageAvailabilityStatus.UNAVAILABLE

    @property
    def usage(self) -> ExecutionUsage:
        # R7 P0-3 / R9 Section 3: Token and Cost now use INDEPENDENT
        # coverage denominators.  An attempt is "applicable" for a
        # dimension when its disposition is NOT ``NO_PROVIDER_CALL`` —
        # i.e. a provider call was made OR we cannot prove it wasn't
        # (UNAVAILABLE).  Only pure deterministic invokers
        # (``never_calls_provider=True``, R9 Section 3) can produce
        # ``NO_PROVIDER_CALL`` dispositions.
        tokens_status = self._compute_usage_status(
            self._verified_token_attempts, self._token_usage_applicable_attempts
        )
        cost_status = self._compute_usage_status(
            self._verified_cost_attempts, self._cost_usage_applicable_attempts
        )
        # R7 P0-3: ``provider_usage_capable_attempts`` is DEPRECATED —
        # retained as a backwards-compatible diagnostic computed as the
        # max of the two independent denominators.  Consumers should use
        # ``token_usage_applicable_attempts`` and
        # ``cost_usage_applicable_attempts`` instead.
        deprecated_capable = max(
            self._token_usage_applicable_attempts,
            self._cost_usage_applicable_attempts,
        )
        # R8 P1-1: expose per-attempt usage records as a defensive
        # deep copy so external consumers can audit each attempt's
        # disposition and source after the run finishes.
        records_copy = [r.model_copy(deep=True) for r in self._attempt_records]
        return ExecutionUsage(
            agent_calls=self._agent_calls,
            tool_calls=self._tool_calls,
            tokens_used=self._tokens_used,
            tokens_usage_available=tokens_status != UsageAvailabilityStatus.UNAVAILABLE,
            tokens_usage_status=tokens_status,
            cost_usd=self._cost_usd,
            cost_usage_available=cost_status != UsageAvailabilityStatus.UNAVAILABLE,
            cost_usage_status=cost_status,
            token_usage_applicable_attempts=self._token_usage_applicable_attempts,
            cost_usage_applicable_attempts=self._cost_usage_applicable_attempts,
            verified_token_attempts=self._verified_token_attempts,
            verified_cost_attempts=self._verified_cost_attempts,
            provider_usage_capable_attempts=deprecated_capable,
            attempt_usage_records=records_copy,
            tool_usage_unavailable=self._tool_usage_unavailable,
            iterations=self._iterations,
        )

    def _compute_attempt_usage(
        self,
        receipt: AgentInvocationReceipt,
        *,
        invoker_capabilities: UsageVerificationCapabilities,
        task_id: str,
        attempt: int,
    ) -> AttemptUsageRecord:
        """R8 P0-4: PURE COMPUTATION phase — compute the
        :class:`AttemptUsageRecord` and budget deltas WITHOUT
        mutating any accountant state.

        R8 P0-1: the dispositions are taken from the Receipt's explicit
        ``token_disposition`` / ``cost_disposition`` fields (declared
        by the Invoker boundary) — NOT inferred from
        ``provider_metadata is None``.  The Accountant only VALIDATES
        them against Invoker capabilities.

        Returns the computed :class:`AttemptUsageRecord`.  Raises
        :class:`ExecutionUsageUnavailableError` on any validation
        failure (Trust / Source / Capability mismatch).
        """
        prov = receipt.usage_provenance
        token_disp = receipt.token_disposition
        cost_disp = receipt.cost_disposition

        # R8 P0-1: VALIDATE the declared dispositions against Invoker
        # capabilities.
        if token_disp == AttemptUsageDisposition.VERIFIED:
            if not invoker_capabilities.verifies_tokens:
                raise ExecutionUsageUnavailableError(
                    f"receipt.token_disposition=VERIFIED but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"verifies_tokens=True — receipt cannot self-elevate "
                    f"token trust above the invoker's capabilities"
                )
            if not prov.tokens_verified:
                raise ExecutionUsageUnavailableError(
                    "receipt.token_disposition=VERIFIED but "
                    "usage_provenance.tokens_verified=False — a VERIFIED "
                    "disposition requires the corresponding provenance flag"
                )
            # R8 P0-3 / R9 Section 6: per-dimension source binding.
            # R9 Section 6: NO fallback to legacy ``prov.source_id`` —
            # the token dimension must use ``token_source_id`` only.
            if invoker_capabilities.bound_token_source_ids:
                src = prov.token_source_id
                if src is None:
                    raise ExecutionUsageUnavailableError(
                        "receipt token_disposition=VERIFIED but "
                        "usage_provenance.token_source_id is None — "
                        "R9 Section 6: token_source_id is required "
                        "(no fallback to legacy source_id)"
                    )
                if src not in invoker_capabilities.bound_token_source_ids:
                    raise ExecutionUsageUnavailableError(
                        f"receipt token_source_id={src!r} is not in the "
                        f"invoker's bound_token_source_ids "
                        f"({sorted(invoker_capabilities.bound_token_source_ids)}) "
                        f"— receipt cannot claim token provenance from an "
                        f"unbound source"
                    )
        if cost_disp == AttemptUsageDisposition.VERIFIED:
            if not invoker_capabilities.verifies_cost:
                raise ExecutionUsageUnavailableError(
                    f"receipt.cost_disposition=VERIFIED but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"verifies_cost=True — receipt cannot self-elevate "
                    f"cost trust above the invoker's capabilities"
                )
            if not prov.cost_verified:
                raise ExecutionUsageUnavailableError(
                    "receipt.cost_disposition=VERIFIED but "
                    "usage_provenance.cost_verified=False — a VERIFIED "
                    "disposition requires the corresponding provenance flag"
                )
            # R8 P0-3 / R9 Section 6: per-dimension source binding.
            # R9 Section 6: NO fallback to legacy ``prov.source_id`` —
            # the cost dimension must use ``cost_source_id`` only.
            if invoker_capabilities.bound_cost_source_ids:
                src = prov.cost_source_id
                if src is None:
                    raise ExecutionUsageUnavailableError(
                        "receipt cost_disposition=VERIFIED but "
                        "usage_provenance.cost_source_id is None — "
                        "R9 Section 6: cost_source_id is required "
                        "(no fallback to legacy source_id)"
                    )
                if src not in invoker_capabilities.bound_cost_source_ids:
                    raise ExecutionUsageUnavailableError(
                        f"receipt cost_source_id={src!r} is not in the "
                        f"invoker's bound_cost_source_ids "
                        f"({sorted(invoker_capabilities.bound_cost_source_ids)}) "
                        f"— receipt cannot claim cost provenance from an "
                        f"unbound source"
                    )
        if token_disp == AttemptUsageDisposition.NO_PROVIDER_CALL:
            if not invoker_capabilities.never_calls_provider:
                raise ExecutionUsageUnavailableError(
                    f"receipt.token_disposition=NO_PROVIDER_CALL but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"never_calls_provider=True — only a pure "
                    f"deterministic invoker can attest no provider call "
                    f"(R9 Section 3)"
                )
        if cost_disp == AttemptUsageDisposition.NO_PROVIDER_CALL:
            if not invoker_capabilities.never_calls_provider:
                raise ExecutionUsageUnavailableError(
                    f"receipt.cost_disposition=NO_PROVIDER_CALL but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"never_calls_provider=True — only a pure "
                    f"deterministic invoker can attest no provider call "
                    f"(R9 Section 3)"
                )

        # Build the AttemptUsageRecord (pure — no state mutation).
        # R9 Section 6: source_id fields come ONLY from the
        # per-dimension ``token_source_id`` / ``cost_source_id`` — no
        # fallback to legacy ``prov.source_id``.
        return AttemptUsageRecord(
            task_id=task_id,
            attempt=attempt,
            token_disposition=token_disp,
            cost_disposition=cost_disp,
            tokens_used=receipt.tokens_used
            if token_disp == AttemptUsageDisposition.VERIFIED
            else None,
            cost_usd=receipt.cost_usd
            if cost_disp == AttemptUsageDisposition.VERIFIED
            else None,
            token_source_id=(
                prov.token_source_id
                if token_disp == AttemptUsageDisposition.VERIFIED
                else None
            ),
            cost_source_id=(
                prov.cost_source_id
                if cost_disp == AttemptUsageDisposition.VERIFIED
                else None
            ),
        )

    def _check_usage_unavailable(
        self,
        record: AttemptUsageRecord,
        budget: ExecutionBudget,
    ) -> bool:
        """R9 Section 1: PURE CHECK — returns ``True`` when the record
        has any dimension ``UNAVAILABLE`` for which a budget is
        configured.

        R9 Section 1: unlike the R8 ``_validate_attempt_budget`` which
        REJECTED mixed records, this method does NOT reject anything.
        The record is ALWAYS committed (preserving verified dimensions),
        and the caller (:meth:`record_receipt`) uses this check to
        mark ``_usage_unavailable`` and ``_exceeded`` AFTER the commit.

        This ensures a mixed record (e.g. Token=VERIFIED + Cost=UNAVAILABLE)
        preserves the verified token usage in the audit trail and
        accumulated totals, while still failing closed on the cost
        dimension.
        """
        if budget.token_budget is not None:
            if record.token_disposition == AttemptUsageDisposition.UNAVAILABLE:
                return True
            if (
                record.token_disposition == AttemptUsageDisposition.VERIFIED
                and record.tokens_used is None
            ):
                return True
        if budget.cost_budget_usd is not None:
            if record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE:
                return True
            if (
                record.cost_disposition == AttemptUsageDisposition.VERIFIED
                and record.cost_usd is None
            ):
                return True
        return False

    def _commit_attempt_usage(self, record: AttemptUsageRecord) -> None:
        """R8 P0-4: ATOMIC COMMIT phase — apply ALL state mutations
        for a computed :class:`AttemptUsageRecord` in a single step.

        This is the ONLY method that mutates the per-dimension
        counters, accumulated usage, verified-attempt counters, and
        the attempt-records list.  Because the compute and validate
        phases ran without side effects, a validation failure cannot
        leave the accountant in a half-committed state.

        R8 P0-4 invariant: every committed agent call produces
        EXACTLY ONE :class:`AttemptUsageRecord` via this method (or
        via :meth:`record_usage_unavailable` for the no-receipt /
        invalid-receipt paths).  There is no path that increments
        counters without also appending a record.
        """
        # Per-dimension applicable counters.
        if record.token_disposition != AttemptUsageDisposition.NO_PROVIDER_CALL:
            self._token_usage_applicable_attempts += 1
        if record.cost_disposition != AttemptUsageDisposition.NO_PROVIDER_CALL:
            self._cost_usage_applicable_attempts += 1
        # Accumulated VERIFIED usage + verified-attempt counters.
        if record.token_disposition == AttemptUsageDisposition.VERIFIED:
            if record.tokens_used is not None:
                self._tokens_used += record.tokens_used
                self._tokens_usage_available = True
                self._verified_token_attempts += 1
        if record.cost_disposition == AttemptUsageDisposition.VERIFIED:
            if record.cost_usd is not None:
                self._cost_usd += record.cost_usd
                self._cost_usage_available = True
                self._verified_cost_attempts += 1
        # Append the record — exactly one per committed call.
        self._attempt_records.append(record)

    def check_max_tasks(self, n_tasks: int) -> None:
        if n_tasks > self._budget.max_tasks:
            self._exceeded = True
            self._exceeded_reason = (
                f"max_tasks exceeded: {n_tasks} > {self._budget.max_tasks}"
            )

    def remaining_deadline_ms(self, now_monotonic: float) -> int:
        elapsed_ms = int((now_monotonic - self._start_monotonic) * 1000)
        return max(0, self._budget.deadline_ms - elapsed_ms)

    def has_time_for_attempt(self, now_monotonic: float) -> bool:
        if self._exceeded:
            return False
        return self.remaining_deadline_ms(now_monotonic) > 0

    def mark_deadline_exceeded(self) -> None:
        """R1 P0-3: explicitly mark the deadline as exhausted.

        Distinguished from ``max_*`` budget exceeded so the Supervisor
        can attribute the final status to ``deadline_exceeded`` rather
        than a generic budget overflow.
        """
        self._exceeded = True
        self._exceeded_reason = "deadline_exceeded"

    def can_start_agent_call(self) -> bool:
        """R5 P0-3: check whether a new agent call can be committed.

        Accounts for both actual calls and outstanding permits.
        Sets ``_exceeded`` when the budget would be breached.
        """
        if self._exceeded:
            return False
        if (
            self._agent_calls + self._permits_outstanding + 1
            > self._budget.max_agent_calls
        ):
            self._exceeded = True
            self._exceeded_reason = (
                f"max_agent_calls exceeded: "
                f"{self._agent_calls + self._permits_outstanding + 1} > "
                f"{self._budget.max_agent_calls}"
            )
            return False
        return True

    def has_agent_call_budget(self) -> bool:
        """R5 P0-3: side-effect-free check for agent call budget
        availability.  Accounts for both actual calls and outstanding
        permits.  Does NOT set ``_exceeded``.
        """
        if self._exceeded:
            return False
        return (
            self._agent_calls + self._permits_outstanding + 1
            <= self._budget.max_agent_calls
        )

    def issue_permit(self, task_id: str) -> AgentCallPermit:
        """R5 P0-3: issue a dispatch permit without incrementing
        ``agent_calls``.

        The permit reserves a slot in the budget (via
        ``_permits_outstanding``) so that subsequent ``has_agent_call_budget``
        checks account for it.  The actual ``agent_calls`` counter is
        only incremented when :meth:`commit_agent_call` is called —
        right before ``invoker.invoke()``.

        If the task is cancelled or deadline-exceeded before
        invocation, :meth:`release_permit` must be called so the slot
        is freed for future waves.
        """
        if not self.has_agent_call_budget():
            raise SupervisorError(
                self._exceeded_reason or "agent_call budget exhausted"
            )
        self._permit_counter += 1
        self._permits_outstanding += 1
        return AgentCallPermit(task_id=task_id, permit_sequence=self._permit_counter)

    def commit_agent_call(self, permit: AgentCallPermit) -> None:
        """R5 P0-3: commit a permit into an actual agent call.

        Called right before ``invoker.invoke()``.  Increments
        ``agent_calls`` and decrements ``_permits_outstanding``.
        If committing would exceed ``max_agent_calls``, sets
        ``_exceeded`` and raises ``SupervisorError``.

        R5 P0-2: does NOT check the general ``_exceeded`` flag.
        The permit was already issued (which checked budget
        availability), so the commit must succeed.  The
        ``_exceeded`` flag may have been set by
        :meth:`mark_budget_exhausted` during pre_dispatch when OTHER
        tasks were denied — that flag stops future waves via
        ``should_stop``, not the current wave's already-permitted
        tasks.
        """
        if self._agent_calls + 1 > self._budget.max_agent_calls:
            self._exceeded = True
            self._exceeded_reason = (
                f"max_agent_calls exceeded: "
                f"{self._agent_calls + 1} > {self._budget.max_agent_calls}"
            )
            raise SupervisorError(self._exceeded_reason)
        self._agent_calls += 1
        self._permits_outstanding = max(0, self._permits_outstanding - 1)

    def release_permit(self, permit: AgentCallPermit) -> None:
        """R5 P0-3: release an unused permit without charging an
        agent call.

        Called when a task is cancelled or deadline-exceeded before
        ``invoker.invoke()`` is called.  The slot is freed for future
        waves.
        """
        self._permits_outstanding = max(0, self._permits_outstanding - 1)

    def mark_budget_exhausted(self, reason: str) -> None:
        """R5 P0-2: mark the budget as exhausted due to pre-dispatch
        denial.  Used when a Ready Task is denied a call slot so the
        run finalises as ``budget_exceeded`` rather than ``completed``.
        """
        self._exceeded = True
        self._exceeded_reason = reason

    def reserve_agent_call(self) -> None:
        """R5 P0-3: legacy method — issues and immediately commits a
        permit.  Kept for retry compatibility.  Prefer
        :meth:`issue_permit` + :meth:`commit_agent_call` for new code.
        """
        permit = self.issue_permit("__legacy__")
        self.commit_agent_call(permit)

    def record_observed_tool_calls(self, observed: int | None) -> None:
        """R4 P0-3 / R9 Section 2: charge *observed* tool calls
        regardless of receipt consistency.

        ``observed`` is ``len(receipt.result.tool_calls)`` — the
        actual number of :class:`ToolCallRecord` entries the Handler
        returned.  This is charged *before* receipt validation so an
        invalid receipt (under-reported ``tool_calls``) cannot erase
        already-consumed budget.  If the charge pushes the total past
        ``max_tool_calls``, the accountant marks the budget exceeded
        so the Scheduler stops dispatching new tasks.

        R9 Section 2: when ``observed is None``, the actual tool call
        count is UNKNOWN (e.g. timeout, exception before any receipt).
        The accountant marks ``_tool_usage_unavailable=True`` and
        fails closed (sets ``_exceeded``) — the Runtime cannot safely
        allow retries or new tasks when it cannot account for tool
        calls that may have already been made.
        """
        if observed is None:
            # R9 Section 2: tool usage is unknown — fail closed.
            self._tool_usage_unavailable = True
            self._exceeded = True
            self._exceeded_reason = ERROR_TOOL_USAGE_UNAVAILABLE
            return
        self._tool_calls += observed
        if self._tool_calls > self._budget.max_tool_calls:
            self._exceeded = True
            self._exceeded_reason = (
                f"max_tool_calls exceeded: "
                f"{self._tool_calls} > {self._budget.max_tool_calls}"
            )

    def record_receipt(
        self,
        receipt: AgentInvocationReceipt,
        *,
        invoker_capabilities: UsageVerificationCapabilities,
        task_id: str,
        attempt: int,
    ) -> None:
        """R8 P0-4 / R9 Section 1: Accumulate *actual* token/cost usage
        from a successful invocation via a three-phase ATOMIC commit:

        1. **Compute** (:meth:`_compute_attempt_usage`) — pure
           function that validates the Receipt's explicit
           :class:`AttemptUsageDisposition` fields against Invoker
           capabilities and builds an :class:`AttemptUsageRecord`.
           No state mutation.
        2. **Commit** (:meth:`_commit_attempt_usage`) — the ONLY
           phase that mutates accountant state.  Applies all counters,
           accumulated usage, and appends the record in one step.
        3. **Post-commit check** (:meth:`_check_usage_unavailable`) —
           marks ``_usage_unavailable`` / ``_exceeded`` when any
           dimension is ``UNAVAILABLE`` with a configured budget.

        R9 Section 1 (CRITICAL): mixed-dimension records (e.g.
        Token=VERIFIED + Cost=UNAVAILABLE) are ALWAYS committed,
        preserving the verified dimension's usage.  The fail-closed
        flag is set AFTER the commit, NOT before.  This replaces the
        R8 behavior which rejected the entire record when any
        dimension was UNAVAILABLE, erasing the verified dimension.

        R8 P0-1: the dispositions are taken from the Receipt's
        explicit ``token_disposition`` / ``cost_disposition`` fields
        (declared by the Invoker boundary) — NOT inferred from
        ``provider_metadata is None``.  The Accountant only VALIDATES
        them against Invoker capabilities.

        On Trust/Source/Capability validation failure (compute phase),
        the method raises :class:`ExecutionUsageUnavailableError`
        WITHOUT mutating state.  The caller (:meth:`_execute_task`)
        is responsible for then calling :meth:`record_usage_unavailable`
        so the attempt still produces exactly one
        :class:`AttemptUsageRecord` with ``UNAVAILABLE`` dispositions.
        """
        # Phase 1: compute (pure — raises on Trust/Source/Capability
        # validation failure only).
        record = self._compute_attempt_usage(
            receipt,
            invoker_capabilities=invoker_capabilities,
            task_id=task_id,
            attempt=attempt,
        )
        # Phase 2: atomic commit (single mutation point).
        # R9 Section 1: ALWAYS commit — even mixed records.  The
        # verified dimension's usage is preserved in the audit trail
        # and accumulated totals.
        self._commit_attempt_usage(record)
        # Phase 3: post-commit fail-closed check.
        # R9 Section 1: NOW check whether any dimension is UNAVAILABLE
        # with a configured budget.  This happens AFTER the commit so
        # the verified dimension is preserved.
        if self._check_usage_unavailable(record, self._budget):
            self._usage_unavailable = True
            self._exceeded = True
            self._exceeded_reason = ERROR_EXECUTION_USAGE_UNAVAILABLE
        # Post-commit budget overflow check (e.g. the commit pushed
        # the total past the limit).  This does not raise — the record
        # is already committed — but marks the accountant so future
        # waves are stopped.
        if (
            self._budget.token_budget is not None
            and self._tokens_used > self._budget.token_budget
        ):
            self._exceeded = True
            self._exceeded_reason = (
                f"token_budget exceeded: "
                f"{self._tokens_used} > {self._budget.token_budget}"
            )
        if (
            self._budget.cost_budget_usd is not None
            and self._cost_usd > self._budget.cost_budget_usd
        ):
            self._exceeded = True
            self._exceeded_reason = (
                f"cost_budget_usd exceeded: "
                f"{self._cost_usd} > {self._budget.cost_budget_usd}"
            )

    def record_usage_unavailable(
        self,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        invoker_capabilities: UsageVerificationCapabilities | None = None,
        outcome: AgentInvocationOutcome | None = None,
    ) -> None:
        """R6 P0-3 / R7 P0-2 / R8 P0-4/P0-5 / R9 Section 2/3 / R10 P0-1:
        record that a committed agent call produced no usable Usage
        Receipt (timeout, exception, Handler pre-return error, OR
        invalid receipt).

        R10 P0-1 (CRITICAL): when an :class:`AgentInvocationOutcome`
        declares ``NO_PROVIDER_CALL``, the Accountant now VALIDATES
        ``invoker_capabilities.never_calls_provider == True``.  A
        Live/Hybrid Invoker can no longer self-attest ``NO_PROVIDER_CALL``
        on the failure path — the disposition is rejected and falls back
        to ``UNAVAILABLE``.

        R10 P0-3: this method is the SIMPLE failure path — it records
        ``UNAVAILABLE`` (or validated ``NO_PROVIDER_CALL``) for both
        dimensions.  For failure outcomes that carry ``VERIFIED`` usage
        (e.g. Token=VERIFIED + Cost=UNAVAILABLE), use
        :meth:`record_invocation_outcome` instead, which preserves the
        verified dimension's usage.

        R9 Section 2/3: the dispositions are determined as follows:

        * If the caller provides an :class:`AgentInvocationOutcome`
          (via the ``outcome`` parameter — e.g. from an
          :class:`AgentInvocationFailure`), the outcome's explicit
          ``token_disposition`` / ``cost_disposition`` are used.
          ``NO_PROVIDER_CALL`` is validated against
          ``invoker_capabilities.never_calls_provider`` (R10 P0-1).
        * If NO outcome is provided (timeout, unknown exception), both
          dimensions default to ``UNAVAILABLE``.

        R7 P0-2: this method is also called for invalid receipts
        (receipts that failed :func:`validate_invocation_receipt`).
        An invalid receipt's Token/Cost data cannot be trusted, so
        both dimensions are marked ``UNAVAILABLE`` regardless of
        Invoker capabilities.

        Sets ``_usage_unavailable=True`` and, when a budget is
        configured, ``_exceeded=True`` with reason
        ``execution_usage_unavailable`` so the run finalises as
        ``budget_exceeded``.
        """
        # R9 Section 2/3: determine dispositions from the explicit
        # Outcome when provided.  NO capability-based inference.
        if outcome is not None:
            token_disp = outcome.token_disposition
            cost_disp = outcome.cost_disposition
            # R10 P0-1: validate NO_PROVIDER_CALL against the Invoker's
            # frozen capability.  A Live/Hybrid Invoker
            # (never_calls_provider=False) CANNOT self-attest
            # NO_PROVIDER_CALL on the failure path — the disposition is
            # rejected and falls back to UNAVAILABLE.
            if (
                token_disp == AttemptUsageDisposition.NO_PROVIDER_CALL
                or cost_disp == AttemptUsageDisposition.NO_PROVIDER_CALL
            ):
                if (
                    invoker_capabilities is None
                    or not invoker_capabilities.never_calls_provider
                ):
                    # Reject the NO_PROVIDER_CALL attestation — the
                    # Invoker does not have the capability.  Fall back
                    # to UNAVAILABLE so the Runtime fails closed.
                    if token_disp == AttemptUsageDisposition.NO_PROVIDER_CALL:
                        token_disp = AttemptUsageDisposition.UNAVAILABLE
                    if cost_disp == AttemptUsageDisposition.NO_PROVIDER_CALL:
                        cost_disp = AttemptUsageDisposition.UNAVAILABLE
        else:
            # No Outcome available (timeout, unknown exception) —
            # both dimensions are UNAVAILABLE.  R9 Section 3: the
            # Runtime does NOT infer NO_PROVIDER_CALL from the
            # Invoker's static capability.
            token_disp = AttemptUsageDisposition.UNAVAILABLE
            cost_disp = AttemptUsageDisposition.UNAVAILABLE

        # R8 P0-4: build and commit the record atomically.
        if task_id is not None and attempt is not None:
            record = AttemptUsageRecord(
                task_id=task_id,
                attempt=attempt,
                token_disposition=token_disp,
                cost_disposition=cost_disp,
                tokens_used=None,
                cost_usd=None,
                token_source_id=None,
                cost_source_id=None,
            )
            self._commit_attempt_usage(record)

        # R7 P0-3 / R8 P0-5: fail-closed when a budget is configured
        # AND any dimension is UNAVAILABLE.  NO_PROVIDER_CALL does
        # not trigger fail-closed (trusted deterministic).
        if (
            self._budget.token_budget is not None
            and token_disp == AttemptUsageDisposition.UNAVAILABLE
        ) or (
            self._budget.cost_budget_usd is not None
            and cost_disp == AttemptUsageDisposition.UNAVAILABLE
        ):
            self._usage_unavailable = True
            self._exceeded = True
            self._exceeded_reason = ERROR_EXECUTION_USAGE_UNAVAILABLE

    def record_invocation_outcome(
        self,
        outcome: AgentInvocationOutcome,
        *,
        invoker_capabilities: UsageVerificationCapabilities,
        task_id: str,
        attempt: int,
    ) -> None:
        """R10 P0-3: Unified entry point for a failure
        :class:`AgentInvocationOutcome` that may carry VERIFIED usage.

        Unlike :meth:`record_usage_unavailable` (which always sets
        values to None), this method preserves VERIFIED usage from a
        failure Outcome.  For example, a failure Outcome with
        ``Token=VERIFIED + Cost=UNAVAILABLE`` will commit the verified
        token usage and fail closed on the cost dimension — the
        verified token usage is NOT discarded.

        The method follows the SAME three-phase pipeline as
        :meth:`record_receipt`:

        1. **Compute** — validate the Outcome's dispositions against
           Invoker capabilities (VERIFIED requires ``verifies_tokens`` /
           ``verifies_cost``; NO_PROVIDER_CALL requires
           ``never_calls_provider``) and source bindings.  Build an
           :class:`AttemptUsageRecord`.  No state mutation.
        2. **Commit** — atomic commit via :meth:`_commit_attempt_usage`.
        3. **Post-commit check** — fail-closed when any dimension is
           UNAVAILABLE with a configured budget.

        R10 P0-1: NO_PROVIDER_CALL is validated against
        ``invoker_capabilities.never_calls_provider``.  An invalid
        attestation raises :class:`ExecutionUsageUnavailableError` so
        the caller can fall back to :meth:`record_usage_unavailable`.

        Raises :class:`ExecutionUsageUnavailableError` on any
        validation failure (Trust / Source / Capability mismatch).
        The caller is responsible for then calling
        :meth:`record_usage_unavailable` so the attempt still produces
        exactly one :class:`AttemptUsageRecord`.
        """
        token_disp = outcome.token_disposition
        cost_disp = outcome.cost_disposition

        # R10 P0-1: validate NO_PROVIDER_CALL against the Invoker's
        # frozen capability.
        if token_disp == AttemptUsageDisposition.NO_PROVIDER_CALL:
            if not invoker_capabilities.never_calls_provider:
                raise ExecutionUsageUnavailableError(
                    f"outcome.token_disposition=NO_PROVIDER_CALL but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"never_calls_provider=True — a Live/Hybrid Invoker "
                    f"cannot self-attest no provider call on the failure "
                    f"path (R10 P0-1)"
                )
        if cost_disp == AttemptUsageDisposition.NO_PROVIDER_CALL:
            if not invoker_capabilities.never_calls_provider:
                raise ExecutionUsageUnavailableError(
                    f"outcome.cost_disposition=NO_PROVIDER_CALL but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"never_calls_provider=True — a Live/Hybrid Invoker "
                    f"cannot self-attest no provider call on the failure "
                    f"path (R10 P0-1)"
                )

        # R10 P0-3: validate VERIFIED dispositions against Invoker
        # capabilities and source bindings — same logic as
        # _compute_attempt_usage for receipts.
        if token_disp == AttemptUsageDisposition.VERIFIED:
            if not invoker_capabilities.verifies_tokens:
                raise ExecutionUsageUnavailableError(
                    f"outcome.token_disposition=VERIFIED but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"verifies_tokens=True — outcome cannot self-elevate "
                    f"token trust above the invoker's capabilities"
                )
            if outcome.tokens_used is None:
                raise ExecutionUsageUnavailableError(
                    "outcome.token_disposition=VERIFIED but tokens_used "
                    "is None — a VERIFIED disposition requires a "
                    "non-None value (R10 P0-3)"
                )
            if invoker_capabilities.bound_token_source_ids:
                src = outcome.token_source_id
                if src is None:
                    raise ExecutionUsageUnavailableError(
                        "outcome token_disposition=VERIFIED but "
                        "token_source_id is None — R10 P0-3: "
                        "token_source_id is required"
                    )
                if src not in invoker_capabilities.bound_token_source_ids:
                    raise ExecutionUsageUnavailableError(
                        f"outcome token_source_id={src!r} is not in the "
                        f"invoker's bound_token_source_ids "
                        f"({sorted(invoker_capabilities.bound_token_source_ids)})"
                    )
        if cost_disp == AttemptUsageDisposition.VERIFIED:
            if not invoker_capabilities.verifies_cost:
                raise ExecutionUsageUnavailableError(
                    f"outcome.cost_disposition=VERIFIED but invoker "
                    f"({invoker_capabilities.source_id}) does not have "
                    f"verifies_cost=True — outcome cannot self-elevate "
                    f"cost trust above the invoker's capabilities"
                )
            if outcome.cost_usd is None:
                raise ExecutionUsageUnavailableError(
                    "outcome.cost_disposition=VERIFIED but cost_usd "
                    "is None — a VERIFIED disposition requires a "
                    "non-None value (R10 P0-3)"
                )
            if invoker_capabilities.bound_cost_source_ids:
                src = outcome.cost_source_id
                if src is None:
                    raise ExecutionUsageUnavailableError(
                        "outcome cost_disposition=VERIFIED but "
                        "cost_source_id is None — R10 P0-3: "
                        "cost_source_id is required"
                    )
                if src not in invoker_capabilities.bound_cost_source_ids:
                    raise ExecutionUsageUnavailableError(
                        f"outcome cost_source_id={src!r} is not in the "
                        f"invoker's bound_cost_source_ids "
                        f"({sorted(invoker_capabilities.bound_cost_source_ids)})"
                    )

        # Build the AttemptUsageRecord — preserves VERIFIED values.
        record = AttemptUsageRecord(
            task_id=task_id,
            attempt=attempt,
            token_disposition=token_disp,
            cost_disposition=cost_disp,
            tokens_used=outcome.tokens_used
            if token_disp == AttemptUsageDisposition.VERIFIED
            else None,
            cost_usd=outcome.cost_usd
            if cost_disp == AttemptUsageDisposition.VERIFIED
            else None,
            token_source_id=(
                outcome.token_source_id
                if token_disp == AttemptUsageDisposition.VERIFIED
                else None
            ),
            cost_source_id=(
                outcome.cost_source_id
                if cost_disp == AttemptUsageDisposition.VERIFIED
                else None
            ),
        )
        # Phase 2: atomic commit (preserves VERIFIED usage).
        self._commit_attempt_usage(record)
        # Phase 3: post-commit fail-closed check.
        if self._check_usage_unavailable(record, self._budget):
            self._usage_unavailable = True
            self._exceeded = True
            self._exceeded_reason = ERROR_EXECUTION_USAGE_UNAVAILABLE
        # Post-commit budget overflow check.
        if (
            self._budget.token_budget is not None
            and self._tokens_used > self._budget.token_budget
        ):
            self._exceeded = True
            self._exceeded_reason = (
                f"token_budget exceeded: "
                f"{self._tokens_used} > {self._budget.token_budget}"
            )
        if (
            self._budget.cost_budget_usd is not None
            and self._cost_usd > self._budget.cost_budget_usd
        ):
            self._exceeded = True
            self._exceeded_reason = (
                f"cost_budget_usd exceeded: "
                f"{self._cost_usd} > {self._budget.cost_budget_usd}"
            )

    def can_start_iteration(self) -> bool:
        """R1 P0-2: check before reserving a new wave.

        Returns ``False`` (and marks exceeded) when one more iteration
        would exceed ``max_iterations``.  The check is made *before*
        the wave is dispatched so a violated budget stops new work
        immediately rather than after the wave completes.

        R5 P0-2: does NOT check the general ``_exceeded`` flag — that
        flag may have been set by pre_dispatch (agent_call budget
        exhaustion) during the CURRENT wave's pre-dispatch phase.
        The ``should_stop`` callback checks ``_exceeded`` at the TOP
        of the next loop iteration and prevents future waves.  But
        the current wave's iteration must still be reserved so its
        already-permitted tasks can run.
        """
        if self._iterations + 1 > self._budget.max_iterations:
            self._exceeded = True
            self._exceeded_reason = (
                f"max_iterations exceeded: "
                f"{self._iterations + 1} > {self._budget.max_iterations}"
            )
            return False
        return True

    def reserve_iteration(self) -> None:
        """R1 P0-2: increment the iteration counter *before*
        dispatching a real Ready Task wave.
        """
        if not self.can_start_iteration():
            raise SupervisorError(self._exceeded_reason or "iteration budget exhausted")
        self._iterations += 1


# ---------------------------------------------------------------------------
# SupervisorRuntime
# ---------------------------------------------------------------------------


class SupervisorRuntime:
    """Phase 4 execution orchestrator."""

    def __init__(
        self,
        *,
        invoker: AgentInvoker | None = None,
        run_store: RunStore | None = None,
        cancellation: ExecutionCancellation | None = None,
        config: SupervisorConfig | None = None,
        plan_validator: PlanValidator | None = None,
    ) -> None:
        self._default_config = config or SupervisorConfig()
        self._default_cancellation = cancellation
        self._run_store = run_store or InMemoryRunStore()
        self._plan_validator = plan_validator or PlanValidator()
        self._invoker = invoker

    # -- public API -------------------------------------------------------

    async def execute(
        self,
        plan: PlanDraft,
        registry: AgentRegistry,
        *,
        config: SupervisorConfig | None = None,
        cancellation: ExecutionCancellation | None = None,
    ) -> SupervisorRunResult:
        cfg = config or self._default_config
        canc = cancellation or self._default_cancellation or FakeExecutionCancellation()

        run_id = plan.run_id
        started_at = utc_now()
        start_mono = time.monotonic()
        trace = _TraceBuilder(run_id)

        # R6 P0-5: Cache Path Isolation — the invoker and its Usage
        # Capabilities are NOT touched until after the cache hit check.
        # A completed run cached under the same (run_id, plan_hash)
        # must be returned without reading any Live Invoker state —
        # the Invoker's ``usage_capabilities`` property might raise
        # or produce side effects, and the cache path must be pure.
        #
        # Correct order:
        #   1. plan.verify_integrity()              (no side effects)
        #   2. RunStore identity probe              (read-only)
        #       - same run + same plan + completed  → cache hit, return
        #       - same run + different plan          → RunPlanConflictError
        #       - same run + same plan + running     → RunAlreadyInProgressError
        #   3. Registry Version                      (no side effects)
        #   4. PlanValidator                         (no side effects)
        #   5. Execution Bindings (handler resolve)  (no side effects)
        #   6. Create or resolve Invoker             (may read registry)
        #   7. Freeze Usage Capabilities             (read invoker once)
        #   8. Cancellation / Kill Switch            (read-only)
        #   9. RunStore.begin()                      (mutates store)
        #  10. Dispatch or finalize as cancelled
        #
        # Step 6 (cancellation) is intentionally *after* the
        # side-effect-free pre-flight checks so that a cancelled run
        # still has a valid plan.  The cancellation only prevents
        # Handler invocation — it does not make an invalid plan
        # acceptable as a cached ``cancelled`` result.
        self._validate_plan_integrity(plan)

        # R3 P1-1 / R4 P0-1: identity probe replaces lookup_completed +
        # begin race.  Determines cache/conflict/in-progress status in
        # one read-only call so the Supervisor can pick the right path
        # without interleaving with another coroutine's begin().
        #
        # R4 P0-1: plan_hash mismatch is checked FIRST — before
        # status — so a RunPlanConflictError is raised regardless of
        # whether the stored run is completed or in_progress.  This
        # prevents a registry version mismatch from masking the real
        # conflict, and prevents an in_progress run with a different
        # plan from being misreported as RunAlreadyInProgressError.
        #
        # R6 P0-5: the cache hit path returns here WITHOUT touching
        # the Live Invoker, its Usage Capabilities, or the
        # ProviderUsageVerifier.  This is the pure cache path.
        identity = await self._run_store.lookup_run_identity(run_id, plan.plan_hash)
        if identity is not None:
            if not identity.plan_hash_matches:
                raise RunPlanConflictError(
                    f"run_id={run_id!r} is already bound to plan_hash="
                    f"{identity.stored_plan_hash!r}, cannot accept "
                    f"plan_hash={identity.requested_plan_hash!r}"
                )
            if identity.status == "completed":
                assert identity.cached_result is not None
                return identity.cached_result
            if identity.status == "in_progress":
                raise RunAlreadyInProgressError(
                    f"run_id={run_id!r} is already in progress"
                )

        # R1 P0-1: Pre-flight validation happens *before* the RunStore
        # lease is acquired.  All of these checks are side-effect-free
        # with respect to the RunStore — an invalid plan must not
        # poison the run_id for a later, valid attempt.
        self._validate_registry_version(plan, registry)
        self._validate_plan_via_validator(plan, registry)
        self._validate_handlers_resolvable(plan, registry)

        # R2 P0-1: Build immutable ExecutionBindings *before* acquiring
        # the lease.  The bindings capture (capability, handler) for
        # every task at pre-flight time; the Supervisor never calls
        # ``registry.resolve()`` again during execution, so a registry
        # mutation during the run cannot change what Handler actually
        # runs.  The SupervisorRunResult records ``plan.registry_version``
        # (not the live registry version) so the cached result is
        # stable across future cache lookups.
        bindings, bound_handlers = self._build_execution_bindings(plan, registry)

        # R6 P0-5: Create or resolve the Invoker AFTER the cache hit
        # path.  The Invoker's ``usage_capabilities`` property is read
        # once here and frozen for the entire run — a mutable Invoker
        # cannot change its capabilities mid-run.
        invoker = self._invoker
        if invoker is None:
            from multi_agent.invocation import RegistryAgentInvoker

            invoker = RegistryAgentInvoker(registry)
        invoker_caps = get_usage_capabilities(invoker)

        # R6 P0-2: Build retry_policy_by_task_id from the Canonical
        # Plan.  Each PlannedTask carries a RetryPolicy that now
        # actually controls runtime retry decisions via should_retry().
        retry_policies: dict[str, RetryPolicy] = {
            pt.task.task_id: pt.retry_policy for pt in plan.tasks
        }

        # R3 P0-1: Cancellation check now happens *after* pre-flight.
        # A pre-cancelled run with a valid plan produces a cached
        # ``cancelled`` result; a pre-cancelled run with an invalid
        # plan raises the appropriate SupervisorError *before* reaching
        # this point, so the run_id is not poisoned.
        if await canc.is_cancelled(run_id) or await canc.is_kill_switch_active(
            plan.tenant_id
        ):
            return await self._finalize_pre_cancelled(
                plan=plan,
                trace=trace,
                started_at=started_at,
                start_mono=start_mono,
                canc=canc,
            )

        # Idempotency lease — only acquired after pre-flight passes.
        lease = await self._run_store.begin(run_id, plan.plan_hash)
        if lease.cached_result is not None:
            return lease.cached_result

        # R1 P0-1: Every code path between begin() and complete() must
        # release the lease on failure.  The try block covers scheduler
        # construction, _build_run_task, _finalize, and scheduler.execute
        # — any of these can raise (TypeError, MemoryError, etc.) and
        # would otherwise leak the in-progress entry, poisoning the
        # run_id for a later valid attempt.  abort() is a no-op if the
        # run was already completed by _finalize, so it is safe to call
        # unconditionally in the except clause.
        try:
            trace.emit(TRACE_RUN_STARTED, data={"plan_hash": plan.plan_hash})
            trace.emit(TRACE_PLAN_VALIDATED)

            # Initial budget check — max_tasks.
            budget = plan.request.budget
            tasks = plan.build_execution_tasks()
            accountant = _BudgetAccountant(budget, start_monotonic=start_mono)
            accountant.check_max_tasks(len(tasks))
            if accountant.exceeded:
                trace.emit(
                    TRACE_BUDGET_EXCEEDED,
                    data={"reason": accountant.exceeded_reason},
                )
                return await self._finalize(
                    plan=plan,
                    task_records=self._seed_skipped_records(
                        tasks, accountant.exceeded_reason or ""
                    ),
                    valid_results=[],
                    trace=trace,
                    lease=lease,
                    start_mono=start_mono,
                    accountant=accountant,
                    forced_status=SupervisorRunStatus.BUDGET_EXCEEDED,
                    canc=canc,
                    started_at=started_at,
                )

            # Cancellation state shared between run_task and should_stop.
            canc_state = _CancellationState()

            # R5 P0-3: permits dict shared between pre_dispatch and
            # _execute_task.  pre_dispatch issues an AgentCallPermit
            # per allowed task (deterministic, by task_id order);
            # _execute_task pops the permit and commits it right
            # before invoker.invoke().  If the task exits before
            # invocation (cancellation / deadline), the permit is
            # released so the slot is available for future waves.
            permits: dict[str, AgentCallPermit] = {}

            # Build the run_task closure — uses bound_handlers, NOT
            # live registry.resolve().  R3 P1-3: bindings are passed
            # through so _execute_task can emit the capability
            # snapshot into the trace for audit correlation.
            run_task = self._build_run_task(
                plan=plan,
                bindings=bindings,
                bound_handlers=bound_handlers,
                invoker=invoker,
                accountant=accountant,
                trace=trace,
                cfg=cfg,
                canc=canc,
                canc_state=canc_state,
                permits=permits,
                invoker_caps=invoker_caps,
                retry_policies=retry_policies,
            )

            # Schedule.
            scheduler = DagScheduler(cfg)
            should_stop = self._build_should_stop(accountant, canc_state, canc, plan)
            before_wave = self._build_before_wave(canc, canc_state, plan)
            wave_callbacks = self._build_wave_callbacks(accountant, trace)
            # R5 P0-2 + P0-3 + P1-1: deterministic agent-call budget
            # pre-allocation via permit-based dispatch.  The filter
            # runs BEFORE on_wave_started (so a wave with no allowed
            # tasks does not consume an iteration or emit task_ready)
            # and issues AgentCallPermits (NOT actual agent calls) in
            # task_id order.  Denied tasks are marked skipped with
            # ``budget_exhausted`` so the run finalises as
            # ``budget_exceeded`` rather than ``completed``.
            pre_dispatch = self._build_pre_dispatch(accountant, permits)

            task_records = await scheduler.execute(
                tasks=tasks,
                run_task=run_task,
                should_stop=should_stop,
                on_wave_started=wave_callbacks.on_wave_started,
                on_wave_completed=wave_callbacks.on_wave_completed,
                on_tasks_skipped=wave_callbacks.on_tasks_skipped,
                before_wave=before_wave,
                pre_dispatch=pre_dispatch,
            )

            # Collect surviving results.
            valid_results = [
                rec.result
                for rec in task_records
                if rec.status == "completed" and rec.result is not None
            ]
            trace.emit(
                TRACE_RESULTS_MERGED,
                data={
                    "result_count": len(valid_results),
                },
            )

            return await self._finalize(
                plan=plan,
                task_records=task_records,
                valid_results=valid_results,
                trace=trace,
                lease=lease,
                start_mono=start_mono,
                accountant=accountant,
                forced_status=None,
                canc=canc,
                started_at=started_at,
            )
        except BaseException as exc:
            # R1 P0-1: Release the lease on any non-clean exit.  We
            # use BaseException (not Exception) so asyncio.CancelledError
            # and KeyboardInterrupt are also cleaned up; they re-raise
            # after the abort so cancellation propagates correctly.
            # abort() is a no-op when the run was already completed by
            # _finalize, so this is safe even if the failure happened
            # after complete().
            await self._run_store.abort(
                lease,
                error_code=type(exc).__name__,
            )
            raise

    # -- pre-flight validation -------------------------------------------

    @staticmethod
    def _validate_plan_integrity(plan: PlanDraft) -> None:
        plan.verify_integrity()

    @staticmethod
    def _validate_registry_version(plan: PlanDraft, registry: AgentRegistry) -> None:
        snapshot = registry.snapshot()
        if snapshot.version != plan.registry_version:
            raise SupervisorError(
                f"registry version mismatch: plan={plan.registry_version[:12]!r} "
                f"registry={snapshot.version[:12]!r}"
            )

    def _validate_plan_via_validator(
        self, plan: PlanDraft, registry: AgentRegistry
    ) -> None:
        report = self._plan_validator.validate(plan.request, plan, registry)
        if not report.valid:
            codes = ",".join(issue.code for issue in report.issues)
            raise SupervisorError(
                f"PlanValidator rejected plan {plan.run_id!r}: {codes}"
            )

    @staticmethod
    def _validate_handlers_resolvable(plan: PlanDraft, registry: AgentRegistry) -> None:
        for planned_task in plan.tasks:
            agent_id = planned_task.task.agent_id
            if not registry.is_registered(agent_id):
                raise SupervisorError(
                    f"handler for agent_id={agent_id!r} is not registered"
                )

    # -- R2 P0-1: Execution Binding -------------------------------------

    @staticmethod
    def _build_execution_bindings(
        plan: PlanDraft,
        registry: AgentRegistry,
    ) -> tuple[
        dict[str, "ExecutionBinding"],
        dict[str, AgentHandler],
    ]:
        """Build immutable (capability, handler) bindings for every task.

        Called *after* pre-flight (integrity, registry version,
        PlanValidator, handler resolvability) passes and *before*
        ``RunStore.begin()``.  The bindings are the frozen "execution
        world" for this run — the Supervisor never calls
        ``registry.resolve()`` again during execution.

        Returns a tuple ``(bindings, bound_handlers)``:

        * ``bindings`` — serialisable :class:`ExecutionBinding` per
          ``task_id``, carrying a deep-copied :class:`AgentCapability`.
        * ``bound_handlers`` — non-serialisable ``Mapping[str,
          AgentHandler]`` kept on the runtime; used by ``run_task``.
        """
        bindings: dict[str, ExecutionBinding] = {}
        bound_handlers: dict[str, AgentHandler] = {}
        for planned_task in plan.tasks:
            task = planned_task.task
            cap, handler = registry.resolve(task.agent_id)
            bindings[task.task_id] = ExecutionBinding(
                task_id=task.task_id,
                agent_id=task.agent_id,
                capability_snapshot=cap,
            )
            bound_handlers[task.task_id] = handler
        return bindings, bound_handlers

    # -- R2 P0-3: pre-cancelled finalize --------------------------------

    async def _finalize_pre_cancelled(
        self,
        *,
        plan: PlanDraft,
        trace: _TraceBuilder,
        started_at: Any,
        start_mono: float,
        canc: ExecutionCancellation,
    ) -> SupervisorRunResult:
        """Build a cancelled result for a run that was cancelled
        *before* the lease was acquired.

        R2 P0-3: this path produces ``iterations=0``, no
        ``task_ready`` events, no ``task_started`` events, and no
        Handler invocations.  All tasks are marked ``cancelled``.
        """
        tasks = plan.build_execution_tasks()
        task_records = [
            TaskExecutionRecord(
                task_id=t.task_id,
                agent_id=t.agent_id,
                status="cancelled",
                skip_reason="cancelled before run started",
            )
            for t in tasks
        ]
        accountant = _BudgetAccountant(plan.request.budget, start_monotonic=start_mono)
        trace.emit(
            TRACE_RUN_CANCELLED,
            data={"reason": "cancelled before run started"},
        )
        # Synthesise a lease so _finalize's complete() call works —
        # but actually we don't need a lease here because we never
        # called begin().  Build the result directly and complete it
        # via a fresh begin/complete cycle.
        lease = await self._run_store.begin(plan.run_id, plan.plan_hash)
        try:
            return await self._finalize(
                plan=plan,
                task_records=task_records,
                valid_results=[],
                trace=trace,
                lease=lease,
                start_mono=start_mono,
                accountant=accountant,
                forced_status=SupervisorRunStatus.CANCELLED,
                canc=canc,
                started_at=started_at,
            )
        except BaseException as exc:
            await self._run_store.abort(lease, error_code=type(exc).__name__)
            raise

    # -- run_task closure ------------------------------------------------

    def _build_run_task(
        self,
        *,
        plan: PlanDraft,
        bindings: dict[str, ExecutionBinding],
        bound_handlers: dict[str, AgentHandler],
        invoker: AgentInvoker,
        accountant: _BudgetAccountant,
        trace: _TraceBuilder,
        cfg: SupervisorConfig,
        canc: ExecutionCancellation,
        canc_state: _CancellationState,
        permits: dict[str, AgentCallPermit],
        invoker_caps: UsageVerificationCapabilities,
        retry_policies: dict[str, RetryPolicy],
    ):
        async def run_task(task: AgentTask) -> TaskOutcome:
            return await self._execute_task(
                task=task,
                plan=plan,
                binding=bindings[task.task_id],
                bound_handlers=bound_handlers,
                invoker=invoker,
                accountant=accountant,
                trace=trace,
                cfg=cfg,
                canc=canc,
                canc_state=canc_state,
                permits=permits,
                invoker_caps=invoker_caps,
                retry_policy=retry_policies.get(task.task_id, RetryPolicy()),
            )

        return run_task

    async def _execute_task(
        self,
        *,
        task: AgentTask,
        plan: PlanDraft,
        binding: ExecutionBinding,
        bound_handlers: dict[str, AgentHandler],
        invoker: AgentInvoker,
        accountant: _BudgetAccountant,
        trace: _TraceBuilder,
        cfg: SupervisorConfig,
        canc: ExecutionCancellation,
        canc_state: _CancellationState,
        permits: dict[str, AgentCallPermit],
        invoker_caps: UsageVerificationCapabilities,
        retry_policy: RetryPolicy,
    ) -> TaskOutcome:
        # R3 P1-3: ExecutionBinding is the authoritative input — it
        # carries the pre-flight capability snapshot AND the handler.
        # The capability_snapshot is emitted into the trace so audit
        # consumers can correlate the executed task with the capability
        # version that was bound at pre-flight time.
        handler = bound_handlers[task.task_id]

        # R5 P0-5: invoker_caps was frozen once at pre-flight time in
        # execute() and passed through _build_run_task.  This prevents
        # a mutable Invoker from changing its usage_capabilities
        # mid-run.  Do NOT call get_usage_capabilities(invoker) here.

        attempts: list[TaskAttemptRecord] = []
        # R6 P0-2: use RetryPolicy from the Canonical Plan, not
        # task.max_retries.  The policy's retryable_error_codes now
        # actually control which error codes are retried.
        max_attempts = 1 + max(0, retry_policy.max_retries)
        final_status: str = "failed"
        final_result: AgentResult | None = None
        skip_reason: str | None = None

        for attempt_idx in range(max_attempts):
            # R5 P0-3: obtain a Call Permit for this attempt.
            #
            # First attempt (attempt_idx == 0): the permit was issued
            # deterministically by the pre_dispatch filter and stored
            # in the shared ``permits`` dict.  We pop it here — the
            # permit reserves a budget slot but does NOT increment
            # ``agent_calls``.
            #
            # Retry attempts (attempt_idx > 0): issue a new permit
            # directly.  If the budget cannot accommodate another call,
            # the task is skipped (not retried).
            #
            # The permit is committed into an actual agent call ONLY
            # when we are about to call ``invoker.invoke()``.  If the
            # task exits before invocation (cancellation, deadline
            # exhaustion), the permit is released so the slot is
            # available for future waves.
            if attempt_idx == 0:
                permit = permits.pop(task.task_id, None)
                if permit is None:
                    # Defensive — pre_dispatch should have issued one
                    # for every allowed task.  If the task reaches
                    # _execute_task, it was in the allowed set.  Issue
                    # one as a fallback (shouldn't happen in practice).
                    permit = accountant.issue_permit(task.task_id)
            else:
                # Retry: check budget and issue a new permit.
                if not accountant.has_agent_call_budget():
                    if not accountant.exceeded:
                        accountant.mark_budget_exhausted(
                            "agent_call budget exhausted during retry"
                        )
                    final_status = "skipped"
                    skip_reason = (
                        accountant.exceeded_reason or "agent_call budget exhausted"
                    )
                    if accountant.exceeded:
                        trace.emit(
                            TRACE_BUDGET_EXCEEDED,
                            task_id=task.task_id,
                            agent_id=task.agent_id,
                            data={"reason": accountant.exceeded_reason},
                        )
                    break
                permit = accountant.issue_permit(task.task_id)

            # Pre-attempt cancellation check — flips the shared flag
            # so the Scheduler's ``should_stop`` stops dispatching
            # the next wave.  R5 P0-3: release the permit so the
            # slot is not consumed by a task that never invoked.
            if await canc.is_cancelled(plan.run_id) or await canc.is_kill_switch_active(
                plan.tenant_id
            ):
                canc_state.cancelled = True
                accountant.release_permit(permit)
                final_status = "cancelled"
                skip_reason = "cancelled before attempt"
                break

            # Pre-attempt deadline check.  R5 P0-3: release the
            # permit — the task cannot run without time, so the
            # slot must be freed for future waves.
            now_mono = time.monotonic()
            remaining_deadline_ms = accountant.remaining_deadline_ms(now_mono)
            if remaining_deadline_ms <= 0:
                # R1 P0-3: deadline exhausted before this attempt.
                # Mark the Run as budget_exceeded (reason=
                # deadline_exceeded) and skip the task — it cannot
                # run without time.
                accountant.release_permit(permit)
                accountant.mark_deadline_exceeded()
                final_status = "skipped"
                skip_reason = "deadline_exceeded"
                trace.emit(
                    TRACE_BUDGET_EXCEEDED,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={"reason": "deadline_exceeded"},
                )
                break

            # R5 P0-3: commit the permit into an actual agent call
            # right before invocation.  This is the point of no
            # return — after this, ``agent_calls`` is incremented
            # and the call is counted regardless of what the Handler
            # returns or whether it times out.  ``commit_agent_call``
            # decrements ``_permits_outstanding`` so the slot is
            # transferred from "reserved" to "used".
            accountant.commit_agent_call(permit)

            attempt_started_at = utc_now()
            attempt_started_mono = time.monotonic()
            trace.emit(
                TRACE_TASK_STARTED,
                task_id=task.task_id,
                agent_id=task.agent_id,
                data={
                    "attempt": attempt_idx,
                    # R3 P1-3: emit the pre-flight capability snapshot
                    # version so audit consumers can correlate the
                    # executed task with the capability bound at
                    # pre-flight time, not the live registry version.
                    "binding_agent_id": binding.agent_id,
                    "binding_capability_agent_id": (
                        binding.capability_snapshot.agent_id
                    ),
                    "binding_capability_authority": (
                        binding.capability_snapshot.authority.value
                    ),
                    # R4 P0-4: emit the capability version so the
                    # trace records exactly which version of the agent
                    # was bound at pre-flight.  This is the version
                    # validate_agent_result checks against
                    # result.agent_version.
                    "binding_capability_version": (binding.capability_snapshot.version),
                },
                occurred_at=attempt_started_at,
            )

            attempt_status: str = "running"
            error_code: str | None = None
            receipt: AgentInvocationReceipt | None = None
            invocation_error: BaseException | None = None
            # R9 Section 2: when the Invoker raises
            # :class:`AgentInvocationFailure`, the carried Outcome
            # provides partial usage info (e.g. observed tool calls
            # before the exception, or an explicit NO_PROVIDER_CALL
            # attestation).  When no Outcome is available (timeout,
            # unknown exception), this stays ``None`` and the Runtime
            # fails closed with ``UNAVAILABLE`` dispositions and
            # ``observed_tool_calls=None``.
            failure_outcome: AgentInvocationOutcome | None = None
            # R1 P0-3: cap the wait_for timeout by the *remaining run
            # deadline*, not just task.timeout_ms.  This prevents a
            # single Attempt from outliving the Run budget.
            effective_timeout_s = min(task.timeout_ms, remaining_deadline_ms) / 1000.0
            # R2 P0-4: record whether the effective timeout was capped
            # by the run deadline.  If so, any TimeoutError is a
            # deadline-caused timeout — we must not rely on a post-hoc
            # ``remaining_deadline_ms <= 0`` check because timer
            # resolution on some platforms (notably Windows) can fire
            # the timeout a few milliseconds early, leaving a sliver
            # of remaining time that incorrectly classifies the
            # timeout as ``task_timeout`` instead of
            # ``run_deadline_exceeded``.
            deadline_was_binding = remaining_deadline_ms <= task.timeout_ms
            deadline_caused_timeout = False

            try:
                context = build_execution_context(plan, task)
                receipt = await asyncio.wait_for(
                    invoker.invoke(handler, task, context),
                    timeout=effective_timeout_s,
                )
            except asyncio.TimeoutError as exc:
                attempt_status = "timed_out"
                # Distinguish run-deadline timeout from task timeout.
                # R2 P0-4: if the effective timeout was capped by the
                # run deadline (``deadline_was_binding``), the timeout
                # is definitively deadline-caused regardless of timer
                # jitter.  Otherwise fall back to checking whether the
                # remaining deadline is now exhausted.
                post_mono = time.monotonic()
                if (
                    deadline_was_binding
                    or accountant.remaining_deadline_ms(post_mono) <= 0
                ):
                    error_code = "run_deadline_exceeded"
                    deadline_caused_timeout = True
                else:
                    error_code = "task_timeout"
                invocation_error = exc
                # R9 Section 2: timeout — no Outcome available.  The
                # actual tool-call count is UNKNOWN (the Handler may
                # have made tool calls before timing out).  The Runtime
                # fails closed with ``observed_tool_calls=None``.
            except AgentInvocationFailure as exc:
                # R9 Section 2: the Invoker raised a controlled
                # failure carrying a partial Outcome.  Use the
                # Outcome's dispositions and observed_tool_calls.
                failure_outcome = exc.outcome
                attempt_status = "failed"
                error_code = failure_outcome.error_code or "invocation_failure"
                invocation_error = exc
            except RetryableAgentError as exc:
                attempt_status = "failed"
                error_code = "retryable_error"
                invocation_error = exc
            except NonRetryableAgentError as exc:
                # R3 P0-2: explicit non-retryable Agent Domain Error.
                # The task is marked ``failed`` and the retry loop
                # breaks (see the ``isinstance(invocation_error,
                # RetryableAgentError)`` guard below).  Siblings are
                # NOT cancelled — this is a business-domain failure,
                # not a programming/infrastructure error.
                attempt_status = "failed"
                error_code = "non_retryable_error"
                invocation_error = exc
            except InvalidAgentResultError as exc:
                attempt_status = "failed"
                error_code = "invalid_result"
                invocation_error = exc
            except InvalidInvocationReceiptError as exc:
                # R1 P0-4: receipt consistency failure.  Non-retryable.
                attempt_status = "failed"
                error_code = "invalid_receipt"
                invocation_error = exc
            except Exception as infra_exc:
                # R10 P1-1: Infrastructure Exception Audit.
                #
                # R3 P0-2 originally had NO ``except Exception`` catch-all
                # here — unknown errors (RuntimeError, TypeError, KeyError,
                # AssertionError, etc.) are programming/infrastructure
                # failures that must propagate to the Scheduler's
                # structured-concurrency boundary so sibling tasks are
                # cancelled and awaited.  Downgrading them to a plain
                # task failure would let siblings continue running on a
                # corrupted state.
                #
                # R10 P1-1 adds a NARROW ``except Exception`` that does
                # NOT downgrade the error — it records a fail-closed
                # UNAVAILABLE usage audit so the invariant "every
                # committed agent call produces exactly one
                # AttemptUsageRecord" holds, then RE-RAISES the original
                # exception so the Scheduler still cancels siblings.
                #
                # The agent call was already committed at this point
                # (``commit_agent_call`` ran before ``invoker.invoke()``),
                # so ``agent_calls`` is incremented but no
                # AttemptUsageRecord would be created without this audit.
                try:
                    accountant.record_observed_tool_calls(None)
                    accountant.record_usage_unavailable(
                        task_id=task.task_id,
                        attempt=attempt_idx,
                    )
                    trace.emit(
                        TRACE_TASK_FAILED,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={
                            "attempt": attempt_idx,
                            "error_code": ERROR_INFRASTRUCTURE_EXCEPTION,
                            "error": (f"{type(infra_exc).__name__}: {infra_exc}"),
                            "infrastructure_exception": True,
                        },
                        occurred_at=utc_now(),
                    )
                except Exception:
                    # Best-effort audit — never mask the original
                    # infrastructure exception with an audit-recording
                    # failure.
                    pass
                raise

            attempt_completed_at = utc_now()
            attempt_completed_mono = time.monotonic()
            duration_ms = int((attempt_completed_mono - attempt_started_mono) * 1000)

            # R1 P0-3: if the attempt timed out because the run
            # deadline was exhausted, mark the accountant and stop.
            if deadline_caused_timeout:
                accountant.mark_deadline_exceeded()

            # R8 P0-5: track untrusted declared values from an invalid
            # receipt or a receipt whose usage validation failed.  These
            # are retained for audit ONLY — the actual usage fields are
            # populated from the Accountant's last committed
            # AttemptUsageRecord, which carries the explicit
            # dispositions decided during the atomic commit.
            declared_tokens_used: int | None = None
            declared_cost_usd: Decimal | None = None

            # R9 Section 2: ``observed_tool_calls`` may be a concrete
            # int (receipt path) or None (failure path with unknown
            # tool-call count).  Declared up-front with the union type
            # so mypy can follow the later reassignment.
            observed_tool_calls: int | None = None

            if receipt is not None:
                # R4 P0-3: charge *observed* tool calls BEFORE receipt
                # validation.  ``observed_tool_calls`` is the actual
                # number of ToolCallRecord entries the Handler returned
                # — it is charged regardless of whether the receipt is
                # consistent, so an under-reporting receipt cannot
                # erase already-consumed budget.
                observed_tool_calls = len(receipt.result.tool_calls)
                accountant.record_observed_tool_calls(observed_tool_calls)

                # R1 P0-4: validate receipt consistency before
                # recording token/cost usage.  A mismatched receipt is
                # treated as a non-retryable failure.
                try:
                    validate_invocation_receipt(receipt)
                except InvalidInvocationReceiptError as exc:
                    attempt_status = "failed"
                    error_code = "invalid_receipt"
                    invocation_error = exc
                    receipt_for_record = receipt
                    receipt = None
                    # R8 P0-5: retain the invalid receipt's declared
                    # values as UNTRUSTED audit fields.  The actual
                    # usage fields will be None because the Accountant
                    # records UNAVAILABLE dispositions for invalid
                    # receipts.
                    declared_tokens_used = receipt_for_record.tokens_used
                    declared_cost_usd = receipt_for_record.cost_usd
                    # R7 P0-2 / R8 P0-5: Invalid Receipt — both
                    # dimensions are UNAVAILABLE regardless of Invoker
                    # capabilities (the receipt existed but was invalid,
                    # so the Invoker's attestation cannot be used).
                    # invoker_capabilities is intentionally NOT passed.
                    accountant.record_usage_unavailable(
                        task_id=task.task_id, attempt=attempt_idx
                    )
                else:
                    receipt_for_record = receipt
                    try:
                        # R4 P0-2: pass invoker capabilities so the
                        # accountant can cross-check the receipt's
                        # usage_provenance against what the Invoker can
                        # actually verify.  R7: also pass task_id and
                        # attempt so an AttemptUsageRecord is stored.
                        # R8 P0-4: record_receipt now uses a three-phase
                        # atomic commit (compute → validate → commit).
                        accountant.record_receipt(
                            receipt,
                            invoker_capabilities=invoker_caps,
                            task_id=task.task_id,
                            attempt=attempt_idx,
                        )
                    except ExecutionUsageUnavailableError as exc:
                        attempt_status = "failed"
                        error_code = "usage_unavailable"
                        invocation_error = exc
                        # R8 P0-4: record_receipt's compute/validate
                        # phases are pure — no state was mutated.  We
                        # must call record_usage_unavailable to produce
                        # exactly one AttemptUsageRecord (R8 P0-4
                        # invariant: every committed call produces
                        # exactly one record).  The receipt's declared
                        # values are retained as untrusted audit fields.
                        declared_tokens_used = receipt_for_record.tokens_used
                        declared_cost_usd = receipt_for_record.cost_usd
                        receipt = None
                        # R8 P0-5: Trust/Source/Capability validation
                        # failed — both dimensions are UNAVAILABLE.
                        # invoker_capabilities is intentionally NOT
                        # passed (the Invoker's attestation was what
                        # failed validation).
                        accountant.record_usage_unavailable(
                            task_id=task.task_id, attempt=attempt_idx
                        )
            else:
                receipt_for_record = None
                # R9 Section 2: No receipt produced (timeout, exception,
                # or Handler pre-return error).  The actual tool-call
                # count is UNKNOWN — the Handler may have made tool
                # calls before erroring.  ``observed_tool_calls=None``
                # triggers ``tool_usage_unavailable`` fail-closed in
                # :meth:`record_observed_tool_calls`.
                #
                # R9 Section 3: the Runtime does NOT infer
                # ``NO_PROVIDER_CALL`` from the Invoker's static
                # ``never_calls_provider`` capability.  Only an explicit
                # :class:`AgentInvocationOutcome` (from
                # :class:`AgentInvocationFailure`) can attest
                # ``NO_PROVIDER_CALL`` on the no-receipt path.
                #
                # R10 P0-3: when a failure Outcome is available, use
                # :meth:`record_invocation_outcome` (the unified entry
                # point) which preserves VERIFIED usage.  If the Outcome
                # fails capability/source validation, fall back to
                # :meth:`record_usage_unavailable` so the attempt still
                # produces exactly one :class:`AttemptUsageRecord`.
                if failure_outcome is not None:
                    # R9 Section 2: use the Outcome's observed_tool_calls
                    # (may be a concrete int from before the exception,
                    # or None when unknown).
                    observed_tool_calls = failure_outcome.observed_tool_calls
                    accountant.record_observed_tool_calls(observed_tool_calls)
                    # R10 P0-3: try the unified outcome recording first.
                    # This preserves VERIFIED usage (e.g. Token=VERIFIED
                    # + Cost=UNAVAILABLE).  If capability/source
                    # validation fails, fall back to the simple
                    # UNAVAILABLE path.
                    try:
                        accountant.record_invocation_outcome(
                            failure_outcome,
                            invoker_capabilities=invoker_caps,
                            task_id=task.task_id,
                            attempt=attempt_idx,
                        )
                    except ExecutionUsageUnavailableError:
                        # R10 P0-1/P0-3: the Outcome's dispositions
                        # failed validation (e.g. a Live Invoker tried
                        # to declare NO_PROVIDER_CALL, or a VERIFIED
                        # dimension lacked a valid source).  Fall back
                        # to UNAVAILABLE so the Runtime fails closed.
                        accountant.record_usage_unavailable(
                            task_id=task.task_id,
                            attempt=attempt_idx,
                            invoker_capabilities=invoker_caps,
                        )
                else:
                    # R9 Section 2: no Outcome available (timeout,
                    # unknown exception).  Tool calls are unknown.
                    observed_tool_calls = None
                    accountant.record_observed_tool_calls(None)
                    accountant.record_usage_unavailable(
                        task_id=task.task_id,
                        attempt=attempt_idx,
                    )

            # R8 P0-5: retrieve the explicit dispositions and source ids
            # from the Accountant's last committed AttemptUsageRecord.
            # This ensures the TaskAttemptRecord's audit fields are
            # consistent with the Accountant's internal state — no
            # duplicated disposition decision logic.
            last_usage = accountant.last_attempt_record
            attempt_token_disp: Any = (
                last_usage.token_disposition
                if last_usage
                else AttemptUsageDisposition.UNAVAILABLE
            )
            attempt_cost_disp: Any = (
                last_usage.cost_disposition
                if last_usage
                else AttemptUsageDisposition.UNAVAILABLE
            )
            attempt_token_src: str | None = (
                last_usage.token_source_id if last_usage else None
            )
            attempt_cost_src: str | None = (
                last_usage.cost_source_id if last_usage else None
            )
            # R8 P0-5: actual usage values are ONLY populated when the
            # corresponding disposition is VERIFIED.  For UNAVAILABLE or
            # NO_PROVIDER_CALL, actual values are None.
            attempt_actual_tokens: int | None = (
                last_usage.tokens_used
                if last_usage and attempt_token_disp == AttemptUsageDisposition.VERIFIED
                else None
            )
            attempt_actual_cost: Decimal | None = (
                last_usage.cost_usd
                if last_usage and attempt_cost_disp == AttemptUsageDisposition.VERIFIED
                else None
            )

            attempt_record = TaskAttemptRecord(
                task_id=task.task_id,
                agent_id=task.agent_id,
                attempt=attempt_idx,
                started_at=attempt_started_at,
                completed_at=attempt_completed_at,
                status=attempt_status,  # type: ignore[arg-type]
                duration_ms=duration_ms,
                error_code=error_code,
                agent_calls=1,
                # R4 P0-3: use the observed tool call count, not the
                # receipt's declared count, so an under-reporting
                # receipt does not produce a misleading audit record.
                tool_calls=observed_tool_calls,
                # R8 P0-5: actual VERIFIED usage — None for
                # UNAVAILABLE / NO_PROVIDER_CALL dispositions.
                tokens_used=attempt_actual_tokens,
                cost_usd=attempt_actual_cost,
                # R8 P0-5: per-dimension disposition + source id.
                token_disposition=attempt_token_disp,
                cost_disposition=attempt_cost_disp,
                token_source_id=attempt_token_src,
                cost_source_id=attempt_cost_src,
                # R8 P0-5: untrusted declared values from invalid
                # receipts — None for valid receipts.
                declared_tokens_used=declared_tokens_used,
                declared_cost_usd=declared_cost_usd,
            )
            attempts.append(attempt_record)

            # Successful Handler return.
            if attempt_status == "running" and receipt is not None:
                try:
                    # R4 P0-4: pass the ExecutionBinding so the
                    # validator can check result.agent_version against
                    # binding.capability_snapshot.version — a Handler
                    # cannot return a result from a different
                    # capability version than the one bound at
                    # pre-flight.
                    validate_agent_result(
                        receipt.result,
                        task=task,
                        plan=plan,
                        binding=binding,
                    )
                except InvalidAgentResultError as exc:
                    attempts[-1] = TaskAttemptRecord(
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        attempt=attempt_idx,
                        started_at=attempt_started_at,
                        completed_at=attempt_completed_at,
                        status="failed",
                        duration_ms=duration_ms,
                        error_code="invalid_result",
                        agent_calls=1,
                        tool_calls=observed_tool_calls,
                        tokens_used=attempt_actual_tokens,
                        cost_usd=attempt_actual_cost,
                        token_disposition=attempt_token_disp,
                        cost_disposition=attempt_cost_disp,
                        token_source_id=attempt_token_src,
                        cost_source_id=attempt_cost_src,
                        declared_tokens_used=None,
                        declared_cost_usd=None,
                    )
                    trace.emit(
                        TRACE_TASK_FAILED,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={
                            "attempt": attempt_idx,
                            "error_code": "invalid_result",
                            "error": str(exc),
                        },
                        occurred_at=attempt_completed_at,
                    )
                    final_status = "failed"
                    final_result = None
                    break

                result = receipt.result
                # R1 P0-5: explicit status mapping.  Each branch
                # maps a result.status to a TaskExecutionRecord.status
                # — no fall-through that collapses cancelled/degraded
                # into failed.
                if result.status == "completed":
                    attempts[-1] = TaskAttemptRecord(
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        attempt=attempt_idx,
                        started_at=attempt_started_at,
                        completed_at=attempt_completed_at,
                        status="completed",
                        duration_ms=duration_ms,
                        agent_calls=1,
                        tool_calls=receipt.tool_calls,
                        tokens_used=attempt_actual_tokens,
                        cost_usd=attempt_actual_cost,
                        token_disposition=attempt_token_disp,
                        cost_disposition=attempt_cost_disp,
                        token_source_id=attempt_token_src,
                        cost_source_id=attempt_cost_src,
                        declared_tokens_used=None,
                        declared_cost_usd=None,
                    )
                    trace.emit(
                        TRACE_TASK_COMPLETED,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={"attempt": attempt_idx},
                        occurred_at=attempt_completed_at,
                    )
                    final_status = "completed"
                    final_result = result
                    break

                if result.status == "needs_input":
                    attempts[-1] = TaskAttemptRecord(
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        attempt=attempt_idx,
                        started_at=attempt_started_at,
                        completed_at=attempt_completed_at,
                        status="needs_input",
                        duration_ms=duration_ms,
                        agent_calls=1,
                        tool_calls=receipt.tool_calls,
                        tokens_used=attempt_actual_tokens,
                        cost_usd=attempt_actual_cost,
                        token_disposition=attempt_token_disp,
                        cost_disposition=attempt_cost_disp,
                        token_source_id=attempt_token_src,
                        cost_source_id=attempt_cost_src,
                        declared_tokens_used=None,
                        declared_cost_usd=None,
                    )
                    trace.emit(
                        TRACE_TASK_NEEDS_INPUT,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={"attempt": attempt_idx},
                        occurred_at=attempt_completed_at,
                    )
                    final_status = "needs_input"
                    final_result = result
                    break

                if result.status == "skipped":
                    attempts[-1] = TaskAttemptRecord(
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        attempt=attempt_idx,
                        started_at=attempt_started_at,
                        completed_at=attempt_completed_at,
                        status="skipped",
                        duration_ms=duration_ms,
                        agent_calls=1,
                        tool_calls=receipt.tool_calls,
                        tokens_used=attempt_actual_tokens,
                        cost_usd=attempt_actual_cost,
                        token_disposition=attempt_token_disp,
                        cost_disposition=attempt_cost_disp,
                        token_source_id=attempt_token_src,
                        cost_source_id=attempt_cost_src,
                        declared_tokens_used=None,
                        declared_cost_usd=None,
                    )
                    trace.emit(
                        TRACE_TASK_SKIPPED,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={"attempt": attempt_idx, "reason": "result.skipped"},
                        occurred_at=attempt_completed_at,
                    )
                    final_status = "skipped"
                    final_result = result
                    break

                if result.status == "cancelled":
                    # R1 P0-5: cancelled is its own semantic — not
                    # collapsed into failed.
                    attempts[-1] = TaskAttemptRecord(
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        attempt=attempt_idx,
                        started_at=attempt_started_at,
                        completed_at=attempt_completed_at,
                        status="cancelled",
                        duration_ms=duration_ms,
                        error_code=(
                            result.errors[0].error_code if result.errors else None
                        ),
                        agent_calls=1,
                        tool_calls=receipt.tool_calls,
                        tokens_used=attempt_actual_tokens,
                        cost_usd=attempt_actual_cost,
                        token_disposition=attempt_token_disp,
                        cost_disposition=attempt_cost_disp,
                        token_source_id=attempt_token_src,
                        cost_source_id=attempt_cost_src,
                        declared_tokens_used=None,
                        declared_cost_usd=None,
                    )
                    trace.emit(
                        TRACE_TASK_SKIPPED,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={
                            "attempt": attempt_idx,
                            "reason": "result.cancelled",
                        },
                        occurred_at=attempt_completed_at,
                    )
                    final_status = "cancelled"
                    final_result = result
                    break

                # result.status in {"failed", "degraded"}
                # R1 P0-5: degraded is treated as failed with an
                # explicit error_code so the audit log distinguishes
                # the two.  Both are retryable iff result.errors
                # contain a retryable error.
                #
                # R10 P0-4: the TaskAttemptRecord MUST use the
                # Accountant's committed values
                # (``attempt_actual_tokens`` / ``attempt_actual_cost``
                # / ``attempt_token_disp`` / ``attempt_cost_disp`` /
                # ``attempt_token_src`` / ``attempt_cost_src``) — NOT
                # the raw Receipt's declared values.  Previously this
                # branch wrote ``receipt.tokens_used`` /
                # ``receipt.cost_usd`` without the corresponding
                # dispositions, causing the audit to publish
                # unverified values as "Actual Usage".
                result_retryable = any(err.retryable for err in result.errors)
                degraded = result.status == "degraded"
                attempts[-1] = TaskAttemptRecord(
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    attempt=attempt_idx,
                    started_at=attempt_started_at,
                    completed_at=attempt_completed_at,
                    status="failed",
                    duration_ms=duration_ms,
                    error_code=(
                        "degraded"
                        if degraded
                        else (result.errors[0].error_code if result.errors else None)
                    ),
                    agent_calls=1,
                    tool_calls=receipt.tool_calls,
                    tokens_used=attempt_actual_tokens,
                    cost_usd=attempt_actual_cost,
                    token_disposition=attempt_token_disp,
                    cost_disposition=attempt_cost_disp,
                    token_source_id=attempt_token_src,
                    cost_source_id=attempt_cost_src,
                    declared_tokens_used=None,
                    declared_cost_usd=None,
                )
                trace.emit(
                    TRACE_TASK_FAILED,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={
                        "attempt": attempt_idx,
                        "result_status": result.status,
                        "retryable": result_retryable,
                    },
                    occurred_at=attempt_completed_at,
                )
                if accountant.usage_unavailable or not should_retry_result(
                    policy=retry_policy,
                    attempt_index=attempt_idx,
                    errors=result.errors,
                ):
                    final_status = "failed"
                    final_result = result
                    break
                trace.emit(
                    TRACE_TASK_RETRYING,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={"next_attempt": attempt_idx + 1},
                )
                backoff_result = await self._maybe_sleep(cfg, accountant, canc, plan)
                if backoff_result == "deadline_exceeded":
                    final_status = "skipped"
                    skip_reason = "deadline_exceeded_during_backoff"
                    break
                if backoff_result == "cancelled":
                    canc_state.cancelled = True
                    final_status = "cancelled"
                    skip_reason = "cancelled_during_backoff"
                    break
                continue

            # Handler raised (timeout / RetryableAgentError / other).
            if attempt_status == "timed_out":
                # R1 P0-3: if the timeout was caused by the run
                # deadline, mark budget_exceeded and stop the task.
                if deadline_caused_timeout:
                    trace.emit(
                        TRACE_BUDGET_EXCEEDED,
                        task_id=task.task_id,
                        agent_id=task.agent_id,
                        data={
                            "reason": "run_deadline_exceeded",
                            "timeout_ms": task.timeout_ms,
                        },
                        occurred_at=attempt_completed_at,
                    )
                    final_status = "skipped"
                    skip_reason = "run_deadline_exceeded"
                    break
                trace.emit(
                    TRACE_TASK_TIMED_OUT,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={"attempt": attempt_idx, "timeout_ms": task.timeout_ms},
                    occurred_at=attempt_completed_at,
                )
                if accountant.usage_unavailable or not should_retry_result(
                    policy=retry_policy,
                    attempt_index=attempt_idx,
                    errors=[
                        AgentError(
                            error_code="task_timeout",
                            message="Task invocation timed out",
                            category=AgentErrorCategory.TIMEOUT,
                            retryable=True,
                        )
                    ],
                ):
                    final_status = "failed"
                    final_result = None
                    break
                if not accountant.has_time_for_attempt(time.monotonic()):
                    accountant.mark_deadline_exceeded()
                    final_status = "skipped"
                    skip_reason = "deadline_exhausted_after_timeout"
                    break
                trace.emit(
                    TRACE_TASK_RETRYING,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={"next_attempt": attempt_idx + 1, "reason": "timeout"},
                )
                backoff_result = await self._maybe_sleep(cfg, accountant, canc, plan)
                if backoff_result == "deadline_exceeded":
                    final_status = "skipped"
                    skip_reason = "deadline_exceeded_during_backoff"
                    break
                if backoff_result == "cancelled":
                    canc_state.cancelled = True
                    final_status = "cancelled"
                    skip_reason = "cancelled_during_backoff"
                    break
                continue

            # attempt_status == "failed" (raised exception).
            trace.emit(
                TRACE_TASK_FAILED,
                task_id=task.task_id,
                agent_id=task.agent_id,
                data={
                    "attempt": attempt_idx,
                    "error_code": error_code,
                    "retryable": isinstance(invocation_error, RetryableAgentError),
                },
                occurred_at=attempt_completed_at,
            )
            if accountant.usage_unavailable or not should_retry_result(
                policy=retry_policy,
                attempt_index=attempt_idx,
                errors=[
                    AgentError(
                        error_code=error_code or "unknown_error",
                        message=(
                            str(invocation_error)
                            if invocation_error
                            else "Invocation failed with no error"
                        ),
                        category=AgentErrorCategory.UNKNOWN,
                        retryable=isinstance(invocation_error, RetryableAgentError),
                    )
                ],
            ):
                final_status = "failed"
                final_result = None
                break
            trace.emit(
                TRACE_TASK_RETRYING,
                task_id=task.task_id,
                agent_id=task.agent_id,
                data={"next_attempt": attempt_idx + 1, "reason": "retryable_error"},
            )
            backoff_result = await self._maybe_sleep(cfg, accountant, canc, plan)
            if backoff_result == "deadline_exceeded":
                final_status = "skipped"
                skip_reason = "deadline_exceeded_during_backoff"
                break
            if backoff_result == "cancelled":
                canc_state.cancelled = True
                final_status = "cancelled"
                skip_reason = "cancelled_during_backoff"
                break

        return TaskOutcome(
            task_id=task.task_id,
            agent_id=task.agent_id,
            status=final_status,  # type: ignore[arg-type]
            attempts=attempts,
            result=final_result,
            skip_reason=skip_reason,
        )

    @staticmethod
    async def _maybe_sleep(
        cfg: SupervisorConfig,
        accountant: _BudgetAccountant,
        canc: ExecutionCancellation,
        plan: PlanDraft,
    ) -> str | None:
        """R2 P0-4: Deadline- and cancellation-aware retry backoff.

        Replaces the previous unconstrained ``asyncio.sleep`` that
        could outlive the Run deadline by several multiples.  The
        backoff duration is the minimum of:

        * ``cfg.retry_backoff_ms``
        * remaining run deadline

        If the remaining deadline is already exhausted, the method
        marks the accountant as ``deadline_exceeded`` and returns
        ``"deadline_exceeded"`` so the caller can stop retrying.

        The sleep is interruptible by cancellation: we poll the
        cancellation source at a small bounded interval (default 10ms
        capped at 100ms) so a cancel/kill-switch event wakes the
        retry loop promptly.  Tests should inject a
        :class:`FakeExecutionCancellation` rather than relying on
        wall-clock timing.

        Returns ``None`` on a clean sleep completion, or one of:

        * ``"deadline_exceeded"`` — the deadline was exhausted before
          or during the sleep; the caller must not start a new attempt.
        * ``"cancelled"`` — a cancellation signal interrupted the
          sleep; the caller must mark the task as cancelled.
        """
        if cfg.retry_backoff_ms <= 0:
            # No backoff configured — still respect cancellation/deadline.
            if await canc.is_cancelled(plan.run_id) or await canc.is_kill_switch_active(
                plan.tenant_id
            ):
                return "cancelled"
            now = time.monotonic()
            if accountant.remaining_deadline_ms(now) <= 0:
                accountant.mark_deadline_exceeded()
                return "deadline_exceeded"
            return None

        now = time.monotonic()
        remaining_ms = accountant.remaining_deadline_ms(now)
        if remaining_ms <= 0:
            accountant.mark_deadline_exceeded()
            return "deadline_exceeded"

        sleep_ms = min(cfg.retry_backoff_ms, remaining_ms)
        # Poll cancellation at a bounded small interval so a cancel
        # signal wakes us promptly without busy-looping.  The poll
        # interval is 10ms (or the remaining sleep, whichever is
        # smaller); capped at 100ms to avoid excessive wakeups on
        # long backoffs.
        poll_interval_ms = min(100, max(10, sleep_ms // 10))
        elapsed_ms = 0
        while elapsed_ms < sleep_ms:
            # Check cancellation first.
            if await canc.is_cancelled(plan.run_id) or await canc.is_kill_switch_active(
                plan.tenant_id
            ):
                return "cancelled"
            # Check deadline.
            now = time.monotonic()
            if accountant.remaining_deadline_ms(now) <= 0:
                accountant.mark_deadline_exceeded()
                return "deadline_exceeded"
            step = min(poll_interval_ms, sleep_ms - elapsed_ms)
            await asyncio.sleep(step / 1000.0)
            elapsed_ms += step
        return None

    # -- should_stop / wave callbacks -----------------------------------

    @staticmethod
    def _build_should_stop(
        accountant: _BudgetAccountant,
        canc_state: _CancellationState,
        canc: ExecutionCancellation,
        plan: PlanDraft,
    ):
        """Sync ``should_stop`` for the Scheduler.

        R2 P0-3: this callback handles *budget* and *local-state*
        cancellation only.  Async cancellation polling (calling the
        async :class:`ExecutionCancellation`) is handled by the
        :meth:`_build_before_wave` hook, which the Scheduler invokes
        *before* ``should_stop`` would gate a wave.  We deliberately
        do NOT attempt ``run_until_complete`` from inside the running
        event loop — that raises ``RuntimeError``.
        """

        def should_stop() -> bool:
            # R1 P0-2: check iteration budget *before* the scheduler
            # enters the next wave.  ``can_start_iteration`` marks the
            # accountant as exceeded (with reason ``max_iterations``)
            # when one more wave would breach the budget, so the run
            # finalises as ``budget_exceeded`` rather than propagating
            # a SupervisorError from ``reserve_iteration``.
            if accountant.exceeded or canc_state.cancelled:
                return True
            return not accountant.can_start_iteration()

        return should_stop

    @staticmethod
    def _build_pre_dispatch(
        accountant: _BudgetAccountant,
        permits: dict[str, AgentCallPermit],
    ) -> PreDispatch:
        """R5 P1-1 + P0-2 + P0-3: deterministic agent-call budget
        pre-allocation with permit-based dispatch.

        Returns a :class:`PreDispatch` filter that iterates the
        sorted ready list and issues one :class:`AgentCallPermit` per
        task in ``task_id`` order.  Tasks that cannot get a permit are
        denied — the Scheduler marks them ``skipped`` before any
        coroutine is created.

        R5 P0-3: permits are NOT actual agent calls.  The actual
        ``agent_calls`` counter is only incremented when
        :meth:`_BudgetAccountant.commit_agent_call` is called inside
        ``_execute_task`` — right before ``invoker.invoke()``.

        R5 P0-2: returns a :class:`DispatchDecision` so the Scheduler
        can distinguish budget-exhaustion denials from other reasons.
        When ``budget_exhausted=True``, the accountant is marked as
        exceeded so the run finalises as ``budget_exceeded``.

        R5 P1-1: this filter now runs BEFORE ``on_wave_started`` so
        a wave with no allowed tasks does not consume an iteration
        or emit ``task_ready``.

        Retries (``attempt_idx > 0`` inside ``_execute_task``) issue
        their own permits directly via :meth:`issue_permit`.
        """

        def pre_dispatch(ready: list[AgentTask]) -> DispatchDecision:
            allowed_ids: list[str] = []
            denied_ids: list[str] = []
            budget_exhausted = False

            for task in ready:  # already sorted by task_id
                if not accountant.has_agent_call_budget():
                    budget_exhausted = True
                    denied_ids.append(task.task_id)
                    continue
                permit = accountant.issue_permit(task.task_id)
                permits[task.task_id] = permit
                allowed_ids.append(task.task_id)

            if budget_exhausted:
                accountant.mark_budget_exhausted(
                    f"max_agent_calls exhausted during pre-dispatch: "
                    f"denied {len(denied_ids)} task(s)"
                )

            return DispatchDecision(
                allowed_task_ids=tuple(allowed_ids),
                denied_task_ids=tuple(denied_ids),
                denial_reason="agent_call budget exhausted" if denied_ids else None,
                budget_exhausted=budget_exhausted,
            )

        return pre_dispatch

    @staticmethod
    def _build_before_wave(
        canc: ExecutionCancellation,
        canc_state: _CancellationState,
        plan: PlanDraft,
    ):
        """R2 P0-3: async hook invoked before each wave is dispatched.

        Polls the async :class:`ExecutionCancellation` source and
        flips the shared :class:`_CancellationState` flag when a
        cancel or kill-switch signal is active.  Returning ``True``
        causes the Scheduler to cancel the wave's ready tasks (no
        Handler invoked) and stop the loop.
        """

        async def before_wave(ready_tasks: list[AgentTask]) -> bool:
            if await canc.is_cancelled(plan.run_id) or await canc.is_kill_switch_active(
                plan.tenant_id
            ):
                canc_state.cancelled = True
                return True
            return False

        return before_wave

    @staticmethod
    def _build_wave_callbacks(
        accountant: _BudgetAccountant,
        trace: _TraceBuilder,
    ):
        """R1 P0-2: split wave callbacks so iteration budget is only
        consumed by *real* Ready-Task waves.

        * ``on_wave_started`` — called *before* the wave is dispatched.
          Reserves an iteration slot; if the budget is exceeded the
          Supervisor's ``should_stop`` will have already halted the
          scheduler, but we double-check here for safety.
        * ``on_wave_completed`` — called after a real wave finishes
          with the records of tasks that reached a terminal status.
          Emits no trace events (per-task events already emitted).
        * ``on_tasks_skipped`` — called when tasks are skipped due to
          dependency failure propagation.  **Does NOT** consume an
          iteration — this is state propagation, not execution.
          Emits ``task_skipped`` trace events so the audit log shows
          *why* each descendant was skipped (R1 P1: TRACE_TASK_SKIPPED
          now covers dependency propagation, not just result.skipped).
        """

        def on_wave_started(ready_tasks: list[AgentTask]) -> None:
            accountant.reserve_iteration()
            for task in ready_tasks:
                trace.emit(
                    TRACE_TASK_READY,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={"wave_size": len(ready_tasks)},
                )

        def on_wave_completed(_records: list[TaskExecutionRecord]) -> None:
            # Iteration was reserved in on_wave_started.  Nothing to
            # do here — per-task trace events were already emitted by
            # run_task.
            pass

        def on_tasks_skipped(records: list[TaskExecutionRecord]) -> None:
            for rec in records:
                trace.emit(
                    TRACE_TASK_SKIPPED,
                    task_id=rec.task_id,
                    agent_id=rec.agent_id,
                    data={
                        "reason": rec.skip_reason or "dependency_failure",
                        "source": "dependency_propagation",
                    },
                )

        # Return as a simple namespace so call sites read clearly.
        class _Callbacks:
            __slots__ = (
                "on_wave_started",
                "on_wave_completed",
                "on_tasks_skipped",
            )

            def __init__(self, ws, wc, ts):
                self.on_wave_started = ws
                self.on_wave_completed = wc
                self.on_tasks_skipped = ts

        return _Callbacks(on_wave_started, on_wave_completed, on_tasks_skipped)

    # -- finalization ----------------------------------------------------

    @staticmethod
    def _seed_skipped_records(
        tasks: list[AgentTask], reason: str
    ) -> list[TaskExecutionRecord]:
        return [
            TaskExecutionRecord(
                task_id=t.task_id,
                agent_id=t.agent_id,
                status="skipped",
                skip_reason=reason,
            )
            for t in tasks
        ]

    async def _finalize(
        self,
        *,
        plan: PlanDraft,
        task_records: list[TaskExecutionRecord],
        valid_results: list[AgentResult],
        trace: _TraceBuilder,
        lease: RunLease,
        start_mono: float,
        accountant: _BudgetAccountant,
        forced_status: SupervisorRunStatus | None,
        canc: ExecutionCancellation,
        started_at: Any,
    ) -> SupervisorRunResult:
        cancelled_during_run = await canc.is_cancelled(
            plan.run_id
        ) or await canc.is_kill_switch_active(plan.tenant_id)

        computed_status = self._compute_final_status(
            task_records, accountant, plan.tasks
        )

        # R3 P1-2: Run-level Cancellation has the highest priority —
        # it must override ``forced_status`` (e.g. budget_exceeded)
        # and the computed status.  Spec §17 priority:
        #   cancelled > budget_exceeded > failed > needs_input >
        #   partial_success > completed
        # The previous order let a ``forced_status=BUDGET_EXCEEDED``
        # (from max_tasks check) mask an active cancellation, violating
        # the spec.
        if cancelled_during_run:
            final_status = SupervisorRunStatus.CANCELLED
            trace.emit(
                TRACE_RUN_CANCELLED,
                data={"reason": "cancellation active at finalize"},
            )
        elif forced_status is not None:
            final_status = forced_status
        elif accountant.exceeded:
            final_status = SupervisorRunStatus.BUDGET_EXCEEDED
            # R5 P0-2: emit TRACE_BUDGET_EXCEEDED when the accountant
            # was marked exceeded by pre_dispatch denial (or any other
            # non-forced path).  When forced_status=BUDGET_EXCEEDED
            # (max_tasks check), the trace event was already emitted
            # in execute() before _finalize was called, so we only
            # emit here — this branch is only reached when
            # forced_status is None.
            trace.emit(
                TRACE_BUDGET_EXCEEDED,
                data={
                    "reason": accountant.exceeded_reason or "budget exceeded",
                },
            )
        else:
            final_status = computed_status

        merged_state = merge_parallel_results(
            valid_results,
            expected_tenant_id=plan.tenant_id,
        )

        completed_at = utc_now()
        duration_ms = int((time.monotonic() - start_mono) * 1000)

        trace.emit(
            TRACE_RUN_COMPLETED,
            data={
                "status": final_status.value,
                "agent_calls": accountant.agent_calls,
                "tool_calls": accountant.tool_calls,
                "iterations": accountant.iterations,
                "evidence_count": len(merged_state.merged_evidence),
                "proposal_count": len(merged_state.merged_proposals),
            },
            occurred_at=completed_at,
        )

        # R2 P0-1: record plan.registry_version (frozen at plan time)
        # NOT the live registry version.  A cached result must remain
        # stable even if the live registry drifts — otherwise a future
        # cache lookup for the same (run_id, plan_hash) would see a
        # mismatched registry_version field despite being a valid hit.
        result = SupervisorRunResult(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            registry_version=plan.registry_version,
            status=final_status,
            task_records=task_records,
            merged_state=merged_state,
            usage=accountant.usage,
            trace=trace.events,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )

        await self._run_store.complete(lease, result)
        return result

    @staticmethod
    def _compute_final_status(
        task_records: list[TaskExecutionRecord],
        accountant: _BudgetAccountant,
        planned_tasks: list[PlannedTask],
    ) -> SupervisorRunStatus:
        """Compute the final run status from task outcomes.

        Spec §17 Failure Propagation (R2 P0-5 refined):

        * Required task failed → FAILED
        * Required task needs_input → NEEDS_INPUT
        * Required task cancelled → CANCELLED
        * Required task skipped (Handler-returned) → FAILED  (was:
          partial_success — a Required task cannot be skipped without
          an explicit failure cause; treating it as partial_success
          masked required root skips behind a benign-looking status)
        * Required task skipped (dependency propagation) →
          transparent; the ancestor's status drives the result
        * Optional task failed/skipped → PARTIAL_SUCCESS
        * All completed → COMPLETED

        Distinguishing Handler-returned ``skipped`` from
        dependency-propagation ``skipped``: the former has
        ``skip_reason is None`` (the Handler actively returned
        ``result.status == "skipped"``); the latter has a
        ``skip_reason`` set by the Scheduler (e.g.
        ``"dependency 'X' status='needs_input'"``).  Only
        Handler-returned skipped on a Required task constitutes a
        failure — dependency-propagation skipped is a *consequence*
        of an ancestor's non-completed status, not an independent
        failure.

        Priority (§17): cancelled > budget_exceeded > failed >
        needs_input > partial_success > completed.
        """
        if accountant.exceeded:
            return SupervisorRunStatus.BUDGET_EXCEEDED

        if not task_records:
            return SupervisorRunStatus.COMPLETED

        # Build task_id → required mapping from the plan.
        required_map = {pt.task.task_id: pt.required for pt in planned_tasks}

        statuses = {rec.status for rec in task_records}
        any_cancelled = "cancelled" in statuses
        any_required_failed = any(
            rec.status == "failed" and required_map.get(rec.task_id, True)
            for rec in task_records
        )
        # R2 P0-5: only Handler-returned skipped (skip_reason is None)
        # on a Required task triggers FAILED.  Dependency-propagation
        # skipped (skip_reason set by the Scheduler) is transparent —
        # the ancestor's status already drives the result, so counting
        # propagated skips as failures would incorrectly override a
        # parent's NEEDS_INPUT with FAILED.
        any_required_handler_skipped = any(
            rec.status == "skipped"
            and rec.skip_reason is None
            and required_map.get(rec.task_id, True)
            for rec in task_records
        )
        any_required_needs_input = any(
            rec.status == "needs_input" and required_map.get(rec.task_id, True)
            for rec in task_records
        )
        any_required_cancelled = any(
            rec.status == "cancelled" and required_map.get(rec.task_id, True)
            for rec in task_records
        )
        any_optional_non_completed = any(
            rec.status != "completed" and not required_map.get(rec.task_id, True)
            for rec in task_records
        )
        all_completed = statuses == {"completed"}

        if all_completed:
            return SupervisorRunStatus.COMPLETED

        # R2 P0-5: Required Handler-skipped is a failure (not
        # partial_success).  Dependency-propagation skipped does NOT
        # trigger this branch.
        if any_required_failed or any_required_handler_skipped:
            # cancelled has higher priority — preserve that.
            if any_cancelled or any_required_cancelled:
                return SupervisorRunStatus.CANCELLED
            return SupervisorRunStatus.FAILED

        candidates: list[SupervisorRunStatus] = []
        if any_cancelled or any_required_cancelled:
            candidates.append(SupervisorRunStatus.CANCELLED)
        if any_required_needs_input:
            candidates.append(SupervisorRunStatus.NEEDS_INPUT)
        if any_optional_non_completed:
            candidates.append(SupervisorRunStatus.PARTIAL_SUCCESS)
        if not candidates:
            candidates.append(SupervisorRunStatus.COMPLETED)

        candidates.sort(key=final_status_priority)
        return candidates[0]


__all__ = [
    "SupervisorRuntime",
]
