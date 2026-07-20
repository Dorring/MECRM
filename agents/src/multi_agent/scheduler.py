"""Phase 4 dependency-aware DAG Scheduler.

The Scheduler is intentionally thin: it owns the DAG wave loop, the
ready-queue ordering, and the bounded-concurrency primitive.  It does
**not** own retry, timeout, budget accounting, or result validation —
those live in :class:`multi_agent.supervisor.SupervisorRuntime`, which
hands the Scheduler a ``run_task`` callable.

Contract
--------

* Input: a list of :class:`AgentTask` execution copies (from
  :meth:`PlanDraft.build_execution_tasks`).
* Output: a list of :class:`TaskExecutionRecord`, one per task.
* Ordering: the ready queue is sorted by ``task_id`` so the same plan
  + the same deterministic ``run_task`` produces a repeatable trace.
* Concurrency: ``asyncio.Semaphore(config.max_concurrency)`` bounds the
  in-flight tasks.  No unbounded ``asyncio.gather``.
* Failure propagation: a task whose any dependency did not reach
  ``completed`` is marked ``skipped``.  Independent branches continue.
* No mutation: the Scheduler never mutates the input tasks or the
  :class:`PlanDraft`.  All state lives in :class:`TaskExecutionRecord`.

R1 P0-2 callback split
----------------------

The Scheduler exposes three distinct callbacks so the Supervisor can
distinguish *real* Ready-Task waves (which consume an iteration slot)
from *state propagation* (which must not):

* ``on_wave_started(ready_tasks)`` — called *before* a wave of Ready
  Tasks is dispatched.  The Supervisor reserves an iteration here.
* ``on_wave_completed(records)`` — called *after* a wave of Ready
  Tasks reaches a terminal status.  No-op for accounting.
* ``on_tasks_skipped(records)`` — called when tasks are skipped due
  to dependency failure propagation.  Does NOT consume an iteration.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from multi_agent.contracts import AgentTask
from multi_agent.execution import (
    SupervisorConfig,
    TaskExecutionRecord,
)


# ---------------------------------------------------------------------------
# Outcome — what run_task returns to the Scheduler
# ---------------------------------------------------------------------------


class TaskOutcome(TaskExecutionRecord):
    """Result of executing a single task.

    Identical to :class:`TaskExecutionRecord` but semantically a
    *return value* — the Scheduler copies it into its records map.
    Kept as a subclass so callers can construct either interchangeably.
    """


TaskRunner = Callable[[AgentTask], Awaitable[TaskOutcome]]
ShouldStop = Callable[[], bool]
WaveCallback = Callable[[list[TaskExecutionRecord]], None]
WaveStartedCallback = Callable[[list[AgentTask]], None]


# ---------------------------------------------------------------------------
# Terminal-status helpers
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "needs_input", "skipped", "cancelled"}
)


def _is_terminal(status: str) -> bool:
    return status in _TERMINAL_STATUSES


def _dependencies_terminal(
    task: AgentTask,
    records: dict[str, TaskExecutionRecord],
) -> bool:
    """True when every dependency has reached a terminal status."""
    for dep_id in task.dependencies:
        dep = records.get(dep_id)
        if dep is None or not _is_terminal(dep.status):
            return False
    return True


def _dependencies_all_completed(
    task: AgentTask,
    records: dict[str, TaskExecutionRecord],
) -> bool:
    """True when every dependency reached ``completed``."""
    for dep_id in task.dependencies:
        dep = records.get(dep_id)
        if dep is None or dep.status != "completed":
            return False
    return True


def _skip_reason_for(task: AgentTask, records: dict[str, TaskExecutionRecord]) -> str:
    """Human-readable reason why *task* is being skipped."""
    for dep_id in sorted(task.dependencies):
        dep = records.get(dep_id)
        if dep is None:
            return f"dependency {dep_id!r} missing"
        if dep.status != "completed":
            return f"dependency {dep_id!r} status={dep.status!r}"
    return "no ready dependencies"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class DagScheduler:
    """Wave-based DAG scheduler with bounded concurrency."""

    def __init__(self, config: SupervisorConfig) -> None:
        self._config = config

    @property
    def config(self) -> SupervisorConfig:
        return self._config

    async def execute(
        self,
        tasks: list[AgentTask],
        run_task: TaskRunner,
        *,
        should_stop: ShouldStop | None = None,
        on_wave_started: WaveStartedCallback | None = None,
        on_wave_completed: WaveCallback | None = None,
        on_tasks_skipped: WaveCallback | None = None,
    ) -> list[TaskExecutionRecord]:
        """Execute *tasks* in dependency order.

        Parameters
        ----------
        tasks
            Execution copies from :meth:`PlanDraft.build_execution_tasks`.
            The Scheduler does not mutate them.
        run_task
            Async callable that executes a single task (retry, timeout,
            invocation, result validation).  Returns the final
            :class:`TaskOutcome`.
        should_stop
            Sync callable checked before every wave.  When it returns
            ``True`` the Scheduler stops dispatching new tasks; every
            still-pending task is marked ``cancelled``.
        on_wave_started
            R1 P0-2: sync callback invoked *before* a wave of Ready
            Tasks is dispatched.  The Supervisor reserves an iteration
            slot here.  Receives the list of :class:`AgentTask` about
            to run.  Not invoked for skip propagation or cancellation
            cleanup.
        on_wave_completed
            Sync callback invoked *after* a wave of Ready Tasks
            reaches a terminal status.  Receives the records of tasks
            that ran in this wave.  Used by the Supervisor to emit
            per-wave trace events.
        on_tasks_skipped
            R1 P0-2: sync callback invoked when tasks are skipped due
            to dependency failure propagation.  Does NOT consume an
            iteration — state propagation is not execution.
        """
        # Build the initial records map.  We never mutate the input
        # tasks; all state lives in records.
        records: dict[str, TaskExecutionRecord] = {
            t.task_id: TaskExecutionRecord(
                task_id=t.task_id,
                agent_id=t.agent_id,
                status="pending",
            )
            for t in tasks
        }
        pending_ids: set[str] = set(records.keys())

        while pending_ids:
            # 1. Resolve failure propagation FIRST: any pending task
            #    whose dependencies are all terminal but not all
            #    completed is skipped immediately (independent branches
            #    continue).  R1 P0-2: this is state propagation, NOT a
            #    real wave — invoke on_tasks_skipped rather than
            #    on_wave_completed.  Moving this before the should_stop
            #    check ensures that dependency-failed descendants are
            #    marked as ``skipped`` (not ``cancelled``) even when the
            #    iteration budget is exhausted.
            newly_skipped: list[str] = []
            for tid in sorted(pending_ids):
                task = self._find_task(tasks, tid)
                if task is None:
                    # Defensive — should never happen.
                    continue
                if _dependencies_terminal(
                    task, records
                ) and not _dependencies_all_completed(task, records):
                    rec = records[tid]
                    reason = _skip_reason_for(task, records)
                    records[tid] = TaskExecutionRecord(
                        task_id=rec.task_id,
                        agent_id=rec.agent_id,
                        status="skipped",
                        attempts=rec.attempts,
                        result=rec.result,
                        skip_reason=reason,
                    )
                    newly_skipped.append(tid)

            for tid in newly_skipped:
                pending_ids.discard(tid)

            if newly_skipped and on_tasks_skipped is not None:
                on_tasks_skipped([records[tid] for tid in newly_skipped])

            if not pending_ids:
                break

            # 2. Cancellation / budget stop check.
            if should_stop is not None and should_stop():
                for tid in sorted(pending_ids):
                    rec = records[tid]
                    records[tid] = TaskExecutionRecord(
                        task_id=rec.task_id,
                        agent_id=rec.agent_id,
                        status="cancelled",
                        attempts=rec.attempts,
                        result=rec.result,
                        skip_reason="run stopped before dispatch",
                    )
                pending_ids.clear()
                break

            # 3. Find ready tasks: pending + dependencies all completed.
            ready: list[AgentTask] = []
            for tid in sorted(pending_ids):
                task = self._find_task(tasks, tid)
                if task is None:
                    continue
                if _dependencies_all_completed(task, records):
                    ready.append(task)

            if not ready:
                # No ready task but pending remain — every pending task
                # is blocked by a non-terminal dependency.  This should
                # not happen because step 2 skips blocked tasks, but if
                # it does we break to avoid a busy loop.
                break

            # 4. R1 P0-2: notify the Supervisor that a real Ready-Task
            #    wave is about to be dispatched.  This is where the
            #    iteration budget is reserved.
            if on_wave_started is not None:
                on_wave_started(ready)

            # 5. Execute the wave with bounded concurrency.
            semaphore = asyncio.Semaphore(self._config.max_concurrency)
            outcomes = await asyncio.gather(
                *(self._run_with_semaphore(semaphore, run_task, task) for task in ready)
            )

            # 6. Commit outcomes in task_id order so the wave callback
            #    sees a stable sequence.
            wave_terminal: list[TaskExecutionRecord] = []
            for task in ready:
                outcome = next(o for o in outcomes if o.task_id == task.task_id)
                records[task.task_id] = outcome
                pending_ids.discard(task.task_id)
                wave_terminal.append(outcome)

            if on_wave_completed is not None and wave_terminal:
                on_wave_completed(wave_terminal)

        # Return records in stable task_id order.
        return [records[t.task_id] for t in tasks if t.task_id in records]

    @staticmethod
    def _find_task(tasks: list[AgentTask], task_id: str) -> AgentTask | None:
        for t in tasks:
            if t.task_id == task_id:
                return t
        return None

    @staticmethod
    async def _run_with_semaphore(
        semaphore: asyncio.Semaphore,
        run_task: TaskRunner,
        task: AgentTask,
    ) -> TaskOutcome:
        async with semaphore:
            return await run_task(task)


__all__ = [
    "DagScheduler",
    "TaskOutcome",
    "TaskRunner",
    "WaveCallback",
    "WaveStartedCallback",
    "ShouldStop",
]
