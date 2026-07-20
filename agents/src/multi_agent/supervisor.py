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
from typing import Any

from multi_agent.contracts import (
    AgentResult,
    AgentTask,
    ExecutionBudget,
    ExecutionUsage,
)
from multi_agent.planning import PlannedTask
from multi_agent.execution import (
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
    RetryableAgentError,
    SupervisorError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    AgentInvoker,
    validate_invocation_receipt,
)
from multi_agent.plan_validator import PlanValidator
from multi_agent.planning import PlanDraft
from multi_agent.registry import AgentRegistry
from multi_agent.run_store import InMemoryRunStore, RunLease, RunStore
from multi_agent.scheduler import DagScheduler, TaskOutcome
from multi_agent.state import merge_parallel_results


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
        self._tool_calls = 0
        self._tokens_used = 0
        self._cost_usd = Decimal("0.00")
        self._iterations = 0
        self._exceeded: bool = False
        self._exceeded_reason: str | None = None

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
    def cost_usd(self) -> Decimal:
        return self._cost_usd

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
    def usage(self) -> ExecutionUsage:
        return ExecutionUsage(
            agent_calls=self._agent_calls,
            tool_calls=self._tool_calls,
            tokens_used=self._tokens_used,
            cost_usd=self._cost_usd,
            iterations=self._iterations,
        )

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
        if self._exceeded:
            return False
        if self._agent_calls + 1 > self._budget.max_agent_calls:
            self._exceeded = True
            self._exceeded_reason = (
                f"max_agent_calls exceeded: "
                f"{self._agent_calls + 1} > {self._budget.max_agent_calls}"
            )
            return False
        return True

    def reserve_agent_call(self) -> None:
        if not self.can_start_agent_call():
            raise SupervisorError(
                self._exceeded_reason or "agent_call budget exhausted"
            )
        self._agent_calls += 1

    def record_receipt(self, receipt: AgentInvocationReceipt) -> None:
        """Accumulate *actual* usage from a successful invocation."""
        self._tool_calls += receipt.tool_calls
        if self._tool_calls > self._budget.max_tool_calls:
            self._exceeded = True
            self._exceeded_reason = (
                f"max_tool_calls exceeded: "
                f"{self._tool_calls} > {self._budget.max_tool_calls}"
            )

        if self._budget.token_budget is not None:
            if receipt.tokens_used is None:
                raise ExecutionUsageUnavailableError(
                    "token_budget is configured but the invocation "
                    "receipt did not report tokens_used"
                )
            self._tokens_used += receipt.tokens_used
            if self._tokens_used > self._budget.token_budget:
                self._exceeded = True
                self._exceeded_reason = (
                    f"token_budget exceeded: "
                    f"{self._tokens_used} > {self._budget.token_budget}"
                )

        if self._budget.cost_budget_usd is not None:
            if receipt.cost_usd is None:
                raise ExecutionUsageUnavailableError(
                    "cost_budget_usd is configured but the invocation "
                    "receipt did not report cost_usd"
                )
            self._cost_usd += receipt.cost_usd
            if self._cost_usd > self._budget.cost_budget_usd:
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
        """
        if self._exceeded:
            return False
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
        invoker = self._invoker
        if invoker is None:
            from multi_agent.invocation import RegistryAgentInvoker

            invoker = RegistryAgentInvoker(registry)

        run_id = plan.run_id
        started_at = utc_now()
        start_mono = time.monotonic()
        trace = _TraceBuilder(run_id)

        # R1 P0-1: Pre-flight validation happens *before* the RunStore
        # lease is acquired.  All of these checks are side-effect-free
        # with respect to the RunStore — an invalid plan must not
        # poison the run_id for a later, valid attempt.
        self._validate_plan_integrity(plan)
        self._validate_registry_version(plan, registry)
        self._validate_plan_via_validator(plan, registry)
        self._validate_handlers_resolvable(plan, registry)

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
                    registry=registry,
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

            # Build the run_task closure.
            run_task = self._build_run_task(
                plan=plan,
                registry=registry,
                invoker=invoker,
                accountant=accountant,
                trace=trace,
                cfg=cfg,
                canc=canc,
                canc_state=canc_state,
            )

            # Schedule.
            scheduler = DagScheduler(cfg)
            should_stop = self._build_should_stop(accountant, canc_state)
            wave_callbacks = self._build_wave_callbacks(accountant, trace)

            task_records = await scheduler.execute(
                tasks=tasks,
                run_task=run_task,
                should_stop=should_stop,
                on_wave_started=wave_callbacks.on_wave_started,
                on_wave_completed=wave_callbacks.on_wave_completed,
                on_tasks_skipped=wave_callbacks.on_tasks_skipped,
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
                registry=registry,
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
                run_id,
                plan.plan_hash,
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

    # -- run_task closure ------------------------------------------------

    def _build_run_task(
        self,
        *,
        plan: PlanDraft,
        registry: AgentRegistry,
        invoker: AgentInvoker,
        accountant: _BudgetAccountant,
        trace: _TraceBuilder,
        cfg: SupervisorConfig,
        canc: ExecutionCancellation,
        canc_state: _CancellationState,
    ):
        async def run_task(task: AgentTask) -> TaskOutcome:
            return await self._execute_task(
                task=task,
                plan=plan,
                registry=registry,
                invoker=invoker,
                accountant=accountant,
                trace=trace,
                cfg=cfg,
                canc=canc,
                canc_state=canc_state,
            )

        return run_task

    async def _execute_task(
        self,
        *,
        task: AgentTask,
        plan: PlanDraft,
        registry: AgentRegistry,
        invoker: AgentInvoker,
        accountant: _BudgetAccountant,
        trace: _TraceBuilder,
        cfg: SupervisorConfig,
        canc: ExecutionCancellation,
        canc_state: _CancellationState,
    ) -> TaskOutcome:
        _cap, handler = registry.resolve(task.agent_id)

        attempts: list[TaskAttemptRecord] = []
        max_attempts = 1 + max(0, task.max_retries)
        final_status: str = "failed"
        final_result: AgentResult | None = None
        skip_reason: str | None = None

        for attempt_idx in range(max_attempts):
            # Pre-attempt cancellation check — flips the shared flag
            # so the Scheduler's ``should_stop`` stops dispatching
            # the next wave.
            if await canc.is_cancelled(plan.run_id) or await canc.is_kill_switch_active(
                plan.tenant_id
            ):
                canc_state.cancelled = True
                final_status = "cancelled"
                skip_reason = "cancelled before attempt"
                break

            # Pre-attempt budget checks.
            now_mono = time.monotonic()
            remaining_deadline_ms = accountant.remaining_deadline_ms(now_mono)
            if remaining_deadline_ms <= 0:
                # R1 P0-3: deadline exhausted before this attempt.
                # Mark the Run as budget_exceeded (reason=
                # deadline_exceeded) and skip the task — it cannot
                # run without time.
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
            if not accountant.can_start_agent_call():
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

            accountant.reserve_agent_call()

            attempt_started_at = utc_now()
            attempt_started_mono = time.monotonic()
            trace.emit(
                TRACE_TASK_STARTED,
                task_id=task.task_id,
                agent_id=task.agent_id,
                data={"attempt": attempt_idx},
                occurred_at=attempt_started_at,
            )

            attempt_status: str = "running"
            error_code: str | None = None
            receipt: AgentInvocationReceipt | None = None
            invocation_error: BaseException | None = None
            # R1 P0-3: cap the wait_for timeout by the *remaining run
            # deadline*, not just task.timeout_ms.  This prevents a
            # single Attempt from outliving the Run budget.
            effective_timeout_s = min(task.timeout_ms, remaining_deadline_ms) / 1000.0
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
                # If the remaining deadline is now exhausted, the
                # timeout was caused by the Run budget.
                post_mono = time.monotonic()
                if accountant.remaining_deadline_ms(post_mono) <= 0:
                    error_code = "run_deadline_exceeded"
                    deadline_caused_timeout = True
                else:
                    error_code = "task_timeout"
                invocation_error = exc
            except RetryableAgentError as exc:
                attempt_status = "failed"
                error_code = "retryable_error"
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
            except Exception as exc:  # noqa: BLE001
                attempt_status = "failed"
                error_code = type(exc).__name__
                invocation_error = exc

            attempt_completed_at = utc_now()
            attempt_completed_mono = time.monotonic()
            duration_ms = int((attempt_completed_mono - attempt_started_mono) * 1000)

            # R1 P0-3: if the attempt timed out because the run
            # deadline was exhausted, mark the accountant and stop.
            if deadline_caused_timeout:
                accountant.mark_deadline_exceeded()

            if receipt is not None:
                # R1 P0-4: validate receipt consistency before
                # recording usage.  A mismatched receipt is treated
                # as a non-retryable failure.
                try:
                    validate_invocation_receipt(receipt)
                except InvalidInvocationReceiptError as exc:
                    attempt_status = "failed"
                    error_code = "invalid_receipt"
                    invocation_error = exc
                    receipt_for_record = receipt
                    receipt = None
                else:
                    receipt_for_record = receipt
                    try:
                        accountant.record_receipt(receipt)
                    except ExecutionUsageUnavailableError as exc:
                        attempt_status = "failed"
                        error_code = "usage_unavailable"
                        invocation_error = exc
                        receipt = None
            else:
                receipt_for_record = None

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
                tool_calls=(receipt_for_record.tool_calls if receipt_for_record else 0),
                tokens_used=(
                    receipt_for_record.tokens_used if receipt_for_record else None
                ),
                cost_usd=(receipt_for_record.cost_usd if receipt_for_record else None),
            )
            attempts.append(attempt_record)

            # Successful Handler return.
            if attempt_status == "running" and receipt is not None:
                try:
                    validate_agent_result(receipt.result, task=task, plan=plan)
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
                        tool_calls=receipt.tool_calls,
                        tokens_used=receipt.tokens_used,
                        cost_usd=receipt.cost_usd,
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
                        tokens_used=receipt.tokens_used,
                        cost_usd=receipt.cost_usd,
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
                        tokens_used=receipt.tokens_used,
                        cost_usd=receipt.cost_usd,
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
                        status="cancelled",
                        duration_ms=duration_ms,
                        agent_calls=1,
                        tool_calls=receipt.tool_calls,
                        tokens_used=receipt.tokens_used,
                        cost_usd=receipt.cost_usd,
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
                        tokens_used=receipt.tokens_used,
                        cost_usd=receipt.cost_usd,
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
                    tokens_used=receipt.tokens_used,
                    cost_usd=receipt.cost_usd,
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
                if not result_retryable or attempt_idx + 1 >= max_attempts:
                    final_status = "failed"
                    final_result = result
                    break
                trace.emit(
                    TRACE_TASK_RETRYING,
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    data={"next_attempt": attempt_idx + 1},
                )
                await self._maybe_sleep(cfg)
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
                if attempt_idx + 1 >= max_attempts:
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
                await self._maybe_sleep(cfg)
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
            if not isinstance(invocation_error, RetryableAgentError):
                final_status = "failed"
                final_result = None
                break
            if attempt_idx + 1 >= max_attempts:
                final_status = "failed"
                final_result = None
                break
            trace.emit(
                TRACE_TASK_RETRYING,
                task_id=task.task_id,
                agent_id=task.agent_id,
                data={"next_attempt": attempt_idx + 1, "reason": "retryable_error"},
            )
            await self._maybe_sleep(cfg)

        return TaskOutcome(
            task_id=task.task_id,
            agent_id=task.agent_id,
            status=final_status,  # type: ignore[arg-type]
            attempts=attempts,
            result=final_result,
            skip_reason=skip_reason,
        )

    @staticmethod
    async def _maybe_sleep(cfg: SupervisorConfig) -> None:
        if cfg.retry_backoff_ms > 0:
            await asyncio.sleep(cfg.retry_backoff_ms / 1000.0)

    # -- should_stop / wave callbacks -----------------------------------

    @staticmethod
    def _build_should_stop(
        accountant: _BudgetAccountant, canc_state: _CancellationState
    ):
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
        registry: AgentRegistry,
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

        if forced_status is not None:
            final_status = forced_status
        elif cancelled_during_run:
            final_status = SupervisorRunStatus.CANCELLED
            trace.emit(
                TRACE_RUN_CANCELLED,
                data={"reason": "cancellation active at finalize"},
            )
        elif accountant.exceeded:
            final_status = SupervisorRunStatus.BUDGET_EXCEEDED
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

        snapshot = registry.snapshot()
        result = SupervisorRunResult(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            registry_version=snapshot.version,
            status=final_status,
            task_records=task_records,
            merged_state=merged_state,
            usage=accountant.usage,
            trace=trace.events,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )

        await self._run_store.complete(result)
        return result

    @staticmethod
    def _compute_final_status(
        task_records: list[TaskExecutionRecord],
        accountant: _BudgetAccountant,
        planned_tasks: list[PlannedTask],
    ) -> SupervisorRunStatus:
        """Compute the final run status from task outcomes.

        Spec §17 Failure Propagation:

        * Required task failed → FAILED
        * Required task needs_input → NEEDS_INPUT
        * Optional task failed/skipped → PARTIAL_SUCCESS
        * All completed → COMPLETED

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
        any_required_needs_input = any(
            rec.status == "needs_input" and required_map.get(rec.task_id, True)
            for rec in task_records
        )
        any_optional_non_completed = any(
            rec.status != "completed" and not required_map.get(rec.task_id, True)
            for rec in task_records
        )
        any_skipped = "skipped" in statuses
        all_completed = statuses == {"completed"}

        if all_completed:
            return SupervisorRunStatus.COMPLETED

        candidates: list[SupervisorRunStatus] = []
        if any_cancelled:
            candidates.append(SupervisorRunStatus.CANCELLED)
        if any_required_failed:
            candidates.append(SupervisorRunStatus.FAILED)
        if any_required_needs_input:
            candidates.append(SupervisorRunStatus.NEEDS_INPUT)
        if any_optional_non_completed or any_skipped:
            candidates.append(SupervisorRunStatus.PARTIAL_SUCCESS)
        if not candidates:
            candidates.append(SupervisorRunStatus.COMPLETED)

        candidates.sort(key=final_status_priority)
        return candidates[0]


__all__ = [
    "SupervisorRuntime",
]
