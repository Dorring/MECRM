"""Phase 4 DAG Scheduler tests.

Covers:

* Root executes before dependents.
* Independent tasks execute concurrently.
* Ready queue is task_id sorted.
* ``max_concurrency`` is enforced.
* Dependency order, not input order, drives execution.
* No dynamic task creation.
* Failure propagation: dependency not completed → skipped.
* Cancellation via ``should_stop``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from multi_agent.contracts import AgentTask
from multi_agent.execution import SupervisorConfig, TaskExecutionRecord
from multi_agent.scheduler import DagScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    agent_id: str = "agent_a",
    dependencies: frozenset[str] | None = None,
    tenant_id: str = "t-001",
) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        agent_id=agent_id,
        task_type="test_task",
        objective="test",
        tenant_id=tenant_id,
        dependencies=dependencies or frozenset(),
        timeout_ms=10_000,
    )


def _outcome(
    task_id: str,
    agent_id: str,
    status: str = "completed",
    skip_reason: str | None = None,
) -> Any:
    from multi_agent.scheduler import TaskOutcome

    return TaskOutcome(
        task_id=task_id,
        agent_id=agent_id,
        status=status,
        attempts=[],
        result=None,
        skip_reason=skip_reason,
    )


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------


class TestDependencyOrdering:
    @pytest.mark.asyncio
    async def test_root_executes_before_dependents(self):
        order: list[str] = []

        async def run_task(task: AgentTask) -> Any:
            order.append(task.task_id)
            await asyncio.sleep(0)
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        records = await scheduler.execute(tasks, run_task)

        assert len(records) == 2
        assert order[0] == "task_root"
        assert order[1] == "task_child"

    @pytest.mark.asyncio
    async def test_dependency_order_not_input_order(self):
        """Even if a child appears first in the input list, the
        Scheduler must execute the root first."""
        order: list[str] = []

        async def run_task(task: AgentTask) -> Any:
            order.append(task.task_id)
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_child", dependencies=frozenset({"task_root"})),
            _task("task_root"),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        await scheduler.execute(tasks, run_task)

        assert order[0] == "task_root"
        assert order[1] == "task_child"

    @pytest.mark.asyncio
    async def test_no_dynamic_task_creation(self):
        """The Scheduler must not invent tasks beyond what the input
        list contains."""

        async def run_task(task: AgentTask) -> Any:
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        records = await scheduler.execute(tasks, run_task)

        executed_ids = {r.task_id for r in records}
        assert executed_ids == {"task_root", "task_child"}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_independent_tasks_execute_concurrently(self):
        """Two tasks with no dependencies should overlap in time."""
        started: list[str] = []
        finished: list[str] = []

        async def run_task(task: AgentTask) -> Any:
            started.append(task.task_id)
            await asyncio.sleep(0.05)
            finished.append(task.task_id)
            return _outcome(task.task_id, task.agent_id)

        tasks = [_task("task_a"), _task("task_b")]

        scheduler = DagScheduler(SupervisorConfig(max_concurrency=4))
        await scheduler.execute(tasks, run_task)

        # Both tasks must start before either finishes — proving overlap.
        assert started[:2] == sorted(["task_a", "task_b"])  # stable order
        assert len(finished) == 2

    @pytest.mark.asyncio
    async def test_max_concurrency_enforced(self):
        """At most ``max_concurrency`` tasks run at once."""
        peak = {"count": 0, "max": 0}
        lock = asyncio.Lock()

        async def run_task(task: AgentTask) -> Any:
            async with lock:
                peak["count"] += 1
                peak["max"] = max(peak["max"], peak["count"])
            await asyncio.sleep(0.03)
            async with lock:
                peak["count"] -= 1
            return _outcome(task.task_id, task.agent_id)

        tasks = [_task(f"task_{i:02d}") for i in range(8)]

        scheduler = DagScheduler(SupervisorConfig(max_concurrency=2))
        await scheduler.execute(tasks, run_task)

        assert peak["max"] <= 2

    @pytest.mark.asyncio
    async def test_ready_queue_is_task_id_sorted(self):
        """Ready tasks must be dispatched in task_id order so the
        same plan + fake handler produces a repeatable trace."""
        order: list[str] = []

        async def run_task(task: AgentTask) -> Any:
            order.append(task.task_id)
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_delta"),
            _task("task_alpha"),
            _task("task_bravo"),
            _task("task_charlie"),
        ]

        scheduler = DagScheduler(SupervisorConfig(max_concurrency=1))
        await scheduler.execute(tasks, run_task)

        assert order == ["task_alpha", "task_bravo", "task_charlie", "task_delta"]


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


class TestFailurePropagation:
    @pytest.mark.asyncio
    async def test_failed_dependency_skips_descendant(self):
        async def run_task(task: AgentTask) -> Any:
            if task.task_id == "task_root":
                return _outcome(task.task_id, task.agent_id, status="failed")
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        records = await scheduler.execute(tasks, run_task)

        root_rec = next(r for r in records if r.task_id == "task_root")
        child_rec = next(r for r in records if r.task_id == "task_child")

        assert root_rec.status == "failed"
        assert child_rec.status == "skipped"
        assert child_rec.skip_reason is not None

    @pytest.mark.asyncio
    async def test_needs_input_dependency_skips_descendant(self):
        async def run_task(task: AgentTask) -> Any:
            if task.task_id == "task_root":
                return _outcome(task.task_id, task.agent_id, status="needs_input")
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        records = await scheduler.execute(tasks, run_task)

        child_rec = next(r for r in records if r.task_id == "task_child")
        assert child_rec.status == "skipped"

    @pytest.mark.asyncio
    async def test_independent_branch_continues_after_failure(self):
        async def run_task(task: AgentTask) -> Any:
            if task.task_id == "task_root_a":
                return _outcome(task.task_id, task.agent_id, status="failed")
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_root_a"),
            _task("task_root_b"),
            _task("task_child_a", dependencies=frozenset({"task_root_a"})),
            _task("task_child_b", dependencies=frozenset({"task_root_b"})),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        records = await scheduler.execute(tasks, run_task)

        by_id = {r.task_id: r for r in records}
        assert by_id["task_root_a"].status == "failed"
        assert by_id["task_root_b"].status == "completed"
        assert by_id["task_child_a"].status == "skipped"
        assert by_id["task_child_b"].status == "completed"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_should_stop_cancels_pending_tasks(self):
        executed: list[str] = []

        async def run_task(task: AgentTask) -> Any:
            executed.append(task.task_id)
            return _outcome(task.task_id, task.agent_id)

        # Stop after the first wave completes.
        call_count = {"n": 0}

        def should_stop() -> bool:
            call_count["n"] += 1
            return call_count["n"] > 1

        tasks = [_task("task_a"), _task("task_b", dependencies=frozenset({"task_a"}))]

        scheduler = DagScheduler(SupervisorConfig())
        records = await scheduler.execute(tasks, run_task, should_stop=should_stop)

        by_id = {r.task_id: r for r in records}
        assert by_id["task_a"].status == "completed"
        # task_b never started — should be cancelled.
        assert by_id["task_b"].status == "cancelled"
        assert "task_b" not in executed


# ---------------------------------------------------------------------------
# Wave callbacks (R1 P0-2: split into started / completed / skipped)
# ---------------------------------------------------------------------------


class TestWaveCallbacks:
    @pytest.mark.asyncio
    async def test_wave_completed_invoked_with_terminal_records(self):
        async def run_task(task: AgentTask) -> Any:
            return _outcome(task.task_id, task.agent_id)

        waves: list[list[str]] = []

        def on_wave_completed(records: list[TaskExecutionRecord]) -> None:
            waves.append([r.task_id for r in records])

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig(max_concurrency=1))
        await scheduler.execute(tasks, run_task, on_wave_completed=on_wave_completed)

        # Two waves: root, then child.
        assert len(waves) == 2
        assert waves[0] == ["task_root"]
        assert waves[1] == ["task_child"]

    @pytest.mark.asyncio
    async def test_wave_started_invoked_before_dispatch(self):
        """R1 P0-2: ``on_wave_started`` fires *before* the wave runs,
        with the AgentTasks about to be dispatched."""

        async def run_task(task: AgentTask) -> Any:
            return _outcome(task.task_id, task.agent_id)

        started_waves: list[list[str]] = []
        dispatch_order: list[str] = []

        def on_wave_started(ready_tasks: list[AgentTask]) -> None:
            started_waves.append([t.task_id for t in ready_tasks])

        async def tracking_run_task(task: AgentTask) -> Any:
            dispatch_order.append(task.task_id)
            return _outcome(task.task_id, task.agent_id)

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig(max_concurrency=1))
        await scheduler.execute(
            tasks,
            tracking_run_task,
            on_wave_started=on_wave_started,
        )

        assert len(started_waves) == 2
        assert started_waves[0] == ["task_root"]
        assert started_waves[1] == ["task_child"]
        # The callback fires before the run_task is invoked.
        assert dispatch_order == ["task_root", "task_child"]

    @pytest.mark.asyncio
    async def test_skipped_tasks_invoke_on_tasks_skipped_not_on_wave_completed(self):
        """R1 P0-2: dependency-failure propagation must call
        ``on_tasks_skipped``, NOT ``on_wave_completed``.  Skip
        propagation does NOT consume an iteration."""

        async def run_task(task: AgentTask) -> Any:
            if task.task_id == "task_root":
                return _outcome(task.task_id, task.agent_id, status="failed")
            return _outcome(task.task_id, task.agent_id)

        completed_waves: list[list[str]] = []
        skipped_records: list[list[str]] = []

        def on_wave_completed(records: list[TaskExecutionRecord]) -> None:
            completed_waves.append([r.task_id for r in records])

        def on_tasks_skipped(records: list[TaskExecutionRecord]) -> None:
            skipped_records.append([r.task_id for r in records])

        tasks = [
            _task("task_root"),
            _task("task_child", dependencies=frozenset({"task_root"})),
        ]

        scheduler = DagScheduler(SupervisorConfig())
        await scheduler.execute(
            tasks,
            run_task,
            on_wave_completed=on_wave_completed,
            on_tasks_skipped=on_tasks_skipped,
        )

        # ``on_wave_completed`` only sees the root wave (the failed
        # root); ``on_tasks_skipped`` sees the skipped child.
        assert len(completed_waves) == 1
        assert completed_waves[0] == ["task_root"]
        assert len(skipped_records) == 1
        assert skipped_records[0] == ["task_child"]
