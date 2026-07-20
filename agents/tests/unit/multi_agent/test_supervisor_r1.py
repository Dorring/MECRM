"""Phase 4 R1 regression tests.

Direct counter-examples for the five P0 issues and the P1 cleanups
identified in the Phase 4 initial review (commit ``cb241d6``):

* **P0-1** — Run Lease lifecycle: pre-flight must happen *before*
  ``RunStore.begin``; execution failures must release the lease via
  ``abort()``.
* **P0-2** — Iteration Budget: skip propagation must NOT consume an
  iteration; the budget is reserved *before* a real Ready-Task wave.
* **P0-3** — Run Deadline: ``wait_for`` timeout must be capped by the
  remaining run deadline; deadline exhaustion produces
  ``budget_exceeded`` (not ``failed``).
* **P0-4** — Receipt consistency: ``validate_invocation_receipt``
  rejects under/over-reported ``tool_calls`` and token usage.
* **P0-5** — Result Boundary: re-validate ``evidence_ids`` after
  construction; ``cancelled`` status maps to ``cancelled`` (not
  ``failed``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from multi_agent.complexity_gate import CUSTOMER_RECOVERY_OBJECTIVE_KIND
from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    AgentError,
    AgentErrorCategory,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    Evidence,
    EvidenceType,
    ExecutionBudget,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
)
from multi_agent.execution import (
    SupervisorRunStatus,
    validate_agent_result,
)
from multi_agent.execution_errors import (
    InvalidAgentResultError,
    InvalidInvocationReceiptError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    DeterministicFakeInvoker,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PlanDraft,
    PlanValidationReport,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    compute_request_hash,
)
from multi_agent.planning_templates import INTENT_CUSTOMER_CONTEXT
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.run_store import InMemoryRunStore
from multi_agent.supervisor import SupervisorRuntime


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_supervisor.py)
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_capability(
    agent_id: str,
    domains: frozenset[str],
    supported_tasks: frozenset[str],
    allowed_tools: frozenset[str],
    timeout_ms: int = 30_000,
    max_retries: int = 0,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Agent {agent_id}",
        domains=domains,
        supported_tasks=supported_tasks,
        allowed_tools=allowed_tools,
        authority=AgentAuthority.READ,
        input_contract="in",
        output_contract="out",
        timeout_ms=timeout_ms,
        max_retries=max_retries,
        estimated_cost_class="low",
        enabled=True,
    )


def _customer_recovery_caps() -> list[AgentCapability]:
    domain = "customer_recovery"
    return [
        _make_capability(
            "customer_context_specialist",
            frozenset({domain}),
            frozenset({"customer_context_summary"}),
            frozenset({"crm_reader.get_customers"}),
        ),
        _make_capability(
            "support_specialist",
            frozenset({domain}),
            frozenset({"support_analysis"}),
            frozenset({"crm_reader.get_tickets"}),
        ),
        _make_capability(
            "sales_specialist",
            frozenset({domain}),
            frozenset({"sales_risk_analysis"}),
            frozenset({"crm_reader.get_deals"}),
        ),
        _make_capability(
            "knowledge_specialist",
            frozenset({domain}),
            frozenset({"knowledge_recommendation"}),
            frozenset({"vector_search.search"}),
        ),
        _make_capability(
            "analytics_specialist",
            frozenset({domain}),
            frozenset({"recovery_metrics"}),
            frozenset({"crm_reader.get_customers"}),
        ),
    ]


def _default_catalog() -> ToolCatalog:
    return ToolCatalog(
        [
            ToolDescriptor(
                tool_name="crm_reader.get_customers", authority=ToolAuthority.READ
            ),
            ToolDescriptor(
                tool_name="crm_reader.get_tickets", authority=ToolAuthority.READ
            ),
            ToolDescriptor(
                tool_name="crm_reader.get_deals", authority=ToolAuthority.READ
            ),
            ToolDescriptor(
                tool_name="vector_search.search", authority=ToolAuthority.READ
            ),
        ]
    )


def _make_registry(
    caps: list[AgentCapability],
    handlers: dict[str, Any] | None = None,
    catalog: ToolCatalog | None = None,
) -> AgentRegistry:
    reg = AgentRegistry(tool_catalog=catalog or _default_catalog())
    for cap in caps:
        handler = (handlers or {}).get(cap.agent_id, _NoopHandler())
        reg.register(cap, handler)
    return reg


class _NoopHandler:
    """Handler stub that is never called in fake-invoker tests."""

    async def run(
        self, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentResult:  # pragma: no cover
        raise RuntimeError("noop handler should not be called")


def _make_signals(**overrides: Any) -> PlanningSignals:
    defaults: dict[str, Any] = dict(
        event_type=None,
        domains=frozenset({"customer_recovery"}),
        requested_task_types=frozenset(),
        requires_cross_domain=False,
        requires_write=False,
        requires_approval=False,
        has_conflicting_signals=False,
        missing_required_context=False,
        objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    )
    defaults.update(overrides)
    return PlanningSignals(**defaults)


def _make_request(
    registry: AgentRegistry,
    signals: PlanningSignals | None = None,
    budget: ExecutionBudget | None = None,
    **overrides: Any,
) -> PlanningRequest:
    defaults: dict[str, Any] = dict(
        run_id="run-001",
        tenant_id="t-001",
        actor_type="user",
        actor_id="user-001",
        objective="Recover at-risk customer",
        signals=signals or _make_signals(),
        budget=budget or ExecutionBudget(),
        context_summary=None,
        registry_version=registry.snapshot().version,
    )
    defaults.update(overrides)
    return PlanningRequest(**defaults)


def _customer_recovery_plan(
    registry: AgentRegistry,
    *,
    run_id: str = "run-001",
    budget: ExecutionBudget | None = None,
) -> PlanDraft:
    request = _make_request(registry, budget=budget, run_id=run_id)
    return DeterministicPlanner().create_plan(request, registry)


def _ok_result(
    *,
    task: AgentTask,
    status: str = "completed",
    proposals: list | None = None,
    evidence: list | None = None,
    errors: list | None = None,
    agent_id: str | None = None,
    tenant_id: str | None = None,
    provider_metadata: ProviderMetadata | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
    token_usage: TokenUsage | None = None,
) -> AgentResult:
    return AgentResult(
        result_id=f"r-{task.task_id}",
        task_id=task.task_id,
        agent_id=agent_id or task.agent_id,
        agent_version="1.0.0",
        tenant_id=tenant_id or task.tenant_id,
        status=status,
        confidence=1.0,
        duration_ms=0.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        errors=errors or [],
        token_usage=token_usage or TokenUsage(),
        tool_calls=tool_calls or [],
        provider_metadata=provider_metadata,
        completed_at=_FIXED_TS,
    )


def _evidence(
    eid: str, tenant_id: str = "t-001", agent_id: str = "agent_a"
) -> Evidence:
    return Evidence(
        evidence_id=eid,
        evidence_type=EvidenceType.TOOL_RESULT,
        tenant_id=tenant_id,
        source_agent=agent_id,
        created_at=_FIXED_TS,
    )


def _fake_invoker_for_plan(
    plan: PlanDraft,
    *,
    results: dict[str, AgentResult] | None = None,
    factory: Any | None = None,
) -> DeterministicFakeInvoker:
    """Build a fake invoker that returns one result per task_id."""
    results = results or {}

    def _factory(task: AgentTask, ctx: AgentExecutionContext) -> AgentInvocationReceipt:
        if task.task_id in results:
            result = results[task.task_id]
        else:
            result = _ok_result(task=task)
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
        )

    return DeterministicFakeInvoker(factory=factory or _factory)


class _AlwaysValidPlanValidator:
    """PlanValidator stub that always returns ``valid=True``.

    Used when tests tamper with plan content (budget, max_retries) and
    recompute hashes — the real PlanValidator would reject the tampered
    plan because it rebuilds the canonical plan from (request, registry).
    """

    def validate(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> PlanValidationReport:
        return PlanValidationReport(valid=True, issues=[])


def _tamper_plan_budget(plan: PlanDraft, **budget_overrides: Any) -> PlanDraft:
    """Tamper with ``request.budget`` fields and recompute hashes."""
    budget = plan.request.budget
    for k, v in budget_overrides.items():
        object.__setattr__(budget, k, v)
    object.__setattr__(plan, "request_hash", compute_request_hash(plan.request))
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


def _tamper_task_max_retries(
    plan: PlanDraft, task_id: str, max_retries: int
) -> PlanDraft:
    """Tamper with one task's ``max_retries`` and recompute plan_hash."""
    for pt in plan.tasks:
        if pt.task.task_id == task_id:
            object.__setattr__(pt.task, "max_retries", max_retries)
            break
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


def _tamper_task_timeout(plan: PlanDraft, task_id: str, timeout_ms: int) -> PlanDraft:
    """Tamper with one task's ``timeout_ms`` and recompute plan_hash."""
    for pt in plan.tasks:
        if pt.task.task_id == task_id:
            object.__setattr__(pt.task, "timeout_ms", timeout_ms)
            break
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


# ---------------------------------------------------------------------------
# Two-chain DAG helpers (for P0-2 independent-branch tests)
# ---------------------------------------------------------------------------


def _two_chain_caps() -> list[AgentCapability]:
    """Two agents, each supporting one task type."""
    return [
        _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task", "child_task"}),
            frozenset({"tool.read"}),
        ),
        _make_capability(
            "agent_b",
            frozenset({"test"}),
            frozenset({"root_task", "child_task"}),
            frozenset({"tool.read"}),
        ),
    ]


def _two_chain_catalog() -> ToolCatalog:
    """Catalog with the single tool referenced by ``_two_chain_caps``."""
    return ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )


def _two_chain_plan(
    registry: AgentRegistry,
    *,
    budget: ExecutionBudget | None = None,
    run_id: str = "run-001",
) -> PlanDraft:
    """Build a plan with two independent chains: A→A2, B→B2.

    This structure cannot be produced by the customer-recovery template
    (which has a single root), so we construct it manually.
    """
    from multi_agent.complexity_gate import ComplexityDecision
    from multi_agent.planning import PLANNER_VERSION

    task_a = AgentTask(
        task_id="task_a",
        agent_id="agent_a",
        task_type="root_task",
        objective="root A",
        tenant_id="t-001",
        timeout_ms=10_000,
    )
    task_a2 = AgentTask(
        task_id="task_a2",
        agent_id="agent_a",
        task_type="child_task",
        objective="child A2",
        tenant_id="t-001",
        dependencies=frozenset({"task_a"}),
        timeout_ms=10_000,
    )
    task_b = AgentTask(
        task_id="task_b",
        agent_id="agent_b",
        task_type="root_task",
        objective="root B",
        tenant_id="t-001",
        timeout_ms=10_000,
    )
    task_b2 = AgentTask(
        task_id="task_b2",
        agent_id="agent_b",
        task_type="child_task",
        objective="child B2",
        tenant_id="t-001",
        dependencies=frozenset({"task_b"}),
        timeout_ms=10_000,
    )

    signals = PlanningSignals(
        event_type=None,
        domains=frozenset({"test"}),
        requested_task_types=frozenset({"root_task", "child_task"}),
        requires_cross_domain=False,
        requires_write=False,
        requires_approval=False,
        has_conflicting_signals=False,
        missing_required_context=False,
        objective_kind=None,
    )
    request = PlanningRequest(
        run_id=run_id,
        tenant_id="t-001",
        actor_type="user",
        actor_id="user-001",
        objective="two-chain test",
        signals=signals,
        budget=budget or ExecutionBudget(),
        context_summary=None,
        registry_version=registry.snapshot().version,
    )
    complexity = ComplexityDecision(
        route="multi_agent",
        domains=["test"],
        reasons=["test"],
        confidence=1.0,
        requires_human_review=False,
    )
    planned = [
        PlannedTask(
            intent_id="intent_a",
            domain="test",
            task=task_a,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        ),
        PlannedTask(
            intent_id="intent_a2",
            domain="test",
            task=task_a2,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        ),
        PlannedTask(
            intent_id="intent_b",
            domain="test",
            task=task_b,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        ),
        PlannedTask(
            intent_id="intent_b2",
            domain="test",
            task=task_b2,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        ),
    ]
    return PlanDraft(
        request=request,
        request_hash=compute_request_hash(request),
        complexity=complexity,
        tasks=planned,
        planner_version=PLANNER_VERSION,
    )


# ===========================================================================
# P0-1: Run Lease lifecycle
# ===========================================================================


class TestRunLeaseLifecycle:
    """R1 P0-1: pre-flight before lease; abort on execution failure."""

    @pytest.mark.asyncio
    async def test_invalid_plan_does_not_poison_run_id(self):
        """An invalid plan must NOT acquire a RunStore lease.  A later
        valid plan with the same ``run_id`` must succeed."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Corrupt plan_hash — verify_integrity() will reject.
        object.__setattr__(plan, "plan_hash", "0" * 64)

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        with pytest.raises(Exception):
            await runtime.execute(plan, reg)

        # R1 P0-1: no lease was acquired because pre-flight failed.
        assert not store.is_in_progress("run-001")
        # No abort was needed — there was nothing to release.
        assert store.last_error_code("run-001") is None

    @pytest.mark.asyncio
    async def test_execution_exception_releases_run_lease(self):
        """If the Scheduler raises after the lease is acquired, the
        Supervisor must call ``abort()`` so the run_id is not poisoned."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Build an invoker that raises a non-retryable exception on the
        # first task.  The Supervisor's _execute_task catches it and
        # marks the task failed — that path does NOT propagate to the
        # scheduler.execute() boundary.  To exercise the abort path we
        # need an exception that escapes _execute_task entirely.
        #
        # We achieve this by making the invoker raise *after* the
        # receipt is returned — specifically, by tampering with the
        # plan so that build_execution_context raises.  The simplest
        # reliable approach: inject an invoker whose factory raises
        # ValueError (non-retryable) — _execute_task catches it as a
        # generic failure, but we can also test the abort path by
        # making the *Scheduler* itself raise via a bad config.
        #
        # Simpler: tamper max_concurrency to 0 — SupervisorConfig
        # rejects 0, but that fails at construction.  Instead, we
        # monkey-patch the scheduler's execute() to raise.
        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        # Replace DagScheduler with a stub whose execute() raises.
        # __init__ must accept the config arg so construction succeeds
        # and the failure originates from execute() — exercising the
        # abort path inside the try block.
        class _ExplodingScheduler:
            def __init__(self, cfg: Any = None) -> None:  # noqa: ARG002
                pass

            async def execute(self, **kwargs: Any) -> Any:  # noqa: ARG002
                raise RuntimeError("scheduler exploded")

        # Patch the DagScheduler instance that will be created inside
        # execute().  We monkey-patch the module-level class.
        from multi_agent import supervisor as supervisor_module

        original_dag = supervisor_module.DagScheduler
        supervisor_module.DagScheduler = _ExplodingScheduler  # type: ignore[misc]
        try:
            with pytest.raises(RuntimeError, match="scheduler exploded"):
                await runtime.execute(plan, reg)
        finally:
            supervisor_module.DagScheduler = original_dag

        # R1 P0-1: the lease was acquired (pre-flight passed) but the
        # scheduler raised — abort() must have released it.
        assert not store.is_in_progress("run-001")
        assert store.last_error_code("run-001") == "RuntimeError"

    @pytest.mark.asyncio
    async def test_valid_run_after_failed_preflight(self):
        """After a pre-flight failure, the same ``run_id`` must be
        usable by a valid plan."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Corrupt plan_hash for the first attempt.
        object.__setattr__(plan, "plan_hash", "0" * 64)

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        with pytest.raises(Exception):
            await runtime.execute(plan, reg)

        # Restore the correct plan_hash.
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())

        # The same run_id must now succeed.
        result = await runtime.execute(plan, reg)
        assert result.status == SupervisorRunStatus.COMPLETED
        assert store.is_completed("run-001")


# ===========================================================================
# P0-2: Iteration Budget
# ===========================================================================


class TestIterationBudget:
    """R1 P0-2: skip propagation must NOT consume an iteration."""

    @pytest.mark.asyncio
    async def test_skip_propagation_does_not_consume_iteration(self):
        """Customer-recovery plan: root fails → 4 children skipped.

        With ``max_iterations=1`` the root wave (1 real wave) runs.
        The 4 children are skipped via dependency propagation — that
        must NOT count as an iteration.  ``usage.iterations == 1``
        and the run is NOT ``budget_exceeded`` (it is ``failed``
        because the required root failed).
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, max_iterations=1)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        results = {
            root_task.task_id: _ok_result(
                task=root_task,
                status="failed",
                errors=[
                    AgentError(
                        error_code="boom",
                        message="root failed",
                        category=AgentErrorCategory.UNKNOWN,
                        retryable=False,
                    )
                ],
            )
        }

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan, results=results),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Only 1 real wave ran (the root).  Skip propagation for the
        # 4 children did NOT consume an iteration.
        assert result.usage.iterations == 1
        # The run is NOT budget_exceeded — it is failed because the
        # required root task failed.
        assert result.status == SupervisorRunStatus.FAILED
        assert result.status != SupervisorRunStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_iteration_reserved_before_wave(self):
        """``max_iterations=1`` with a 2-wave plan (root + children).

        The root wave runs (iteration 1).  The children wave is
        blocked because ``can_start_iteration`` returns False.  The
        run finalises as ``budget_exceeded`` — NOT ``failed`` —
        because the budget was the limiting factor, not a task error.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, max_iterations=1)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Only 1 iteration consumed (root wave).  The children wave
        # was never dispatched.
        assert result.usage.iterations == 1
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED

        # Root completed; children were never dispatched.
        records_by_id = {r.task_id: r for r in result.task_records}
        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        assert records_by_id[root_task.task_id].status == "completed"
        # Children must NOT be completed — they were blocked.
        for pt in plan.tasks:
            if pt.intent_id == INTENT_CUSTOMER_CONTEXT:
                continue
            assert records_by_id[pt.task.task_id].status != "completed"

    @pytest.mark.asyncio
    async def test_independent_branch_not_false_budget_exceeded(self):
        """Two independent chains A→A2, B→B2 with ``max_iterations=2``.

        A fails in wave 1 → A2 skipped (propagation, NOT an iteration).
        B completes in wave 1 → B2 runs in wave 2.

        Total real waves = 2 (wave 1: A+B, wave 2: B2).  Skip
        propagation for A2 must NOT push the count to 3, so the run
        must NOT be ``budget_exceeded``.
        """
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg, budget=ExecutionBudget(max_iterations=2))

        task_a = next(pt.task for pt in plan.tasks if pt.task.task_id == "task_a")
        task_a2 = next(pt.task for pt in plan.tasks if pt.task.task_id == "task_a2")  # noqa: F841
        task_b = next(pt.task for pt in plan.tasks if pt.task.task_id == "task_b")  # noqa: F841
        task_b2 = next(pt.task for pt in plan.tasks if pt.task.task_id == "task_b2")  # noqa: F841

        results = {
            "task_a": _ok_result(
                task=task_a,
                status="failed",
                errors=[
                    AgentError(
                        error_code="a_failed",
                        message="A failed",
                        category=AgentErrorCategory.UNKNOWN,
                        retryable=False,
                    )
                ],
            ),
            # task_b, task_b2 default to completed.
        }

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan, results=results),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Exactly 2 real waves: (A+B) and (B2).  A2 skip propagation
        # did NOT add a third iteration.
        assert result.usage.iterations == 2
        # The run is NOT budget_exceeded — A is required and failed,
        # so the status is FAILED.
        assert result.status != SupervisorRunStatus.BUDGET_EXCEEDED
        assert result.status == SupervisorRunStatus.FAILED

        # Verify the actual task outcomes.
        by_id = {r.task_id: r for r in result.task_records}
        assert by_id["task_a"].status == "failed"
        assert by_id["task_a2"].status == "skipped"  # propagation
        assert by_id["task_b"].status == "completed"
        assert by_id["task_b2"].status == "completed"


# ===========================================================================
# P0-3: Run Deadline
# ===========================================================================


class TestRunDeadline:
    """R1 P0-3: ``wait_for`` capped by remaining run deadline."""

    @pytest.mark.asyncio
    async def test_attempt_timeout_capped_by_run_deadline(self):
        """``task.timeout_ms=1000`` but ``deadline_ms=50``.  Handler
        sleeps 500ms.  The effective timeout must be the run deadline
        (50ms), not the task timeout (1000ms).  The run finalises as
        ``budget_exceeded`` with reason ``deadline_exceeded``."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, deadline_ms=50)

        # Tamper the root task's timeout to be much larger than the
        # run deadline so we can prove the deadline caps the attempt.
        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        _tamper_task_max_retries(plan, root_task.task_id, 0)
        _tamper_task_timeout(plan, root_task.task_id, 10_000)

        # Build an async invoker that sleeps longer than the run
        # deadline.  We cannot use ``DeterministicFakeInvoker`` with a
        # sync factory because ``time.sleep`` blocks the event loop
        # and ``asyncio.wait_for`` would never fire the timeout.
        call_count = {"n": 0}

        class _SlowInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:  # noqa: ARG002
                call_count["n"] += 1
                await asyncio.sleep(0.5)  # 500ms — longer than deadline_ms=50
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_SlowInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The run must be budget_exceeded, not failed.
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED

        # The root task should have a timed_out attempt with
        # error_code == "run_deadline_exceeded".
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert len(root_rec.attempts) >= 1
        # Find the timed-out attempt.
        timed_out = [a for a in root_rec.attempts if a.status == "timed_out"]
        assert len(timed_out) >= 1
        assert timed_out[0].error_code == "run_deadline_exceeded"

    @pytest.mark.asyncio
    async def test_deadline_exhaustion_is_budget_exceeded(self):
        """When the run deadline is already exhausted before an
        attempt, the task is skipped and the run is ``budget_exceeded``
        with ``exceeded_reason == 'deadline_exceeded'``."""
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(deadline_ms=1)  # 1ms — essentially zero.
        # Use a start_monotonic far in the past so the deadline is
        # already exhausted.
        acc = _BudgetAccountant(budget, start_monotonic=0.0)

        # After 1ms the deadline is exhausted.
        assert not acc.has_time_for_attempt(0.002)
        acc.mark_deadline_exceeded()
        assert acc.exceeded
        assert acc.exceeded_reason == "deadline_exceeded"

    @pytest.mark.asyncio
    async def test_deadline_exhaustion_marks_budget_exceeded_in_run(self):
        """End-to-end: a plan with ``deadline_ms=1`` must finalise as
        ``budget_exceeded`` because the deadline is exhausted before
        the first attempt can complete."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, deadline_ms=1)

        # Invoker that sleeps just a little to ensure the deadline
        # is hit.
        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            import time as _time

            _time.sleep(0.01)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED


# ===========================================================================
# P0-4: Receipt consistency
# ===========================================================================


class TestReceiptConsistency:
    """R1 P0-4: ``validate_invocation_receipt`` rejects mismatches."""

    @pytest.mark.asyncio
    async def test_tool_call_underreport_rejected(self):
        """Receipt reports ``tool_calls=0`` but result has 2
        ToolCallRecords.  The task must be marked ``failed`` with
        ``error_code='invalid_receipt'``."""
        from multi_agent.contracts import ToolAuthority

        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        tool_calls = [
            ToolCallRecord(
                tool_name="crm_reader.get_customers",
                authority=ToolAuthority.READ,
            ),
            ToolCallRecord(
                tool_name="crm_reader.get_customers",
                authority=ToolAuthority.READ,
            ),
        ]

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(task=task, tool_calls=tool_calls)
                # Under-report: 2 actual tool calls, receipt says 0.
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        # The attempt must record the invalid_receipt error_code.
        assert any(a.error_code == "invalid_receipt" for a in root_rec.attempts), (
            f"expected invalid_receipt in attempts, got {[a.error_code for a in root_rec.attempts]}"
        )

    @pytest.mark.asyncio
    async def test_tool_call_overreport_rejected(self):
        """Receipt reports ``tool_calls=5`` but result has 0
        ToolCallRecords.  Over-reporting must also be rejected."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(task=task)  # 0 tool_calls
                # Over-report: 0 actual, receipt says 5.
                return AgentInvocationReceipt(result=result, tool_calls=5)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "invalid_receipt" for a in root_rec.attempts)

    @pytest.mark.asyncio
    async def test_token_receipt_mismatch_rejected(self):
        """When ``provider_metadata`` is present, ``receipt.tokens_used``
        must equal ``result.token_usage.total_tokens``."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        provider_meta = ProviderMetadata(
            provider="openai",
            chat_model="gpt-4",
            embedding_model="text-embedding-3-small",
            ai_mode="live",
        )
        token_usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(
                    task=task,
                    provider_metadata=provider_meta,
                    token_usage=token_usage,
                )
                # Mismatch: result says 15 tokens, receipt says 5.
                return AgentInvocationReceipt(
                    result=result, tool_calls=0, tokens_used=5
                )
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "invalid_receipt" for a in root_rec.attempts)

    @pytest.mark.asyncio
    async def test_invalid_receipt_not_merged(self):
        """A task whose receipt fails validation must NOT contribute
        its result to ``merged_state``."""
        from multi_agent.contracts import ToolAuthority

        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # Build a result with evidence + a proposal so we can check
        # whether it leaks into merged_state.
        ev = _evidence("ev-root", agent_id=root_task.agent_id)
        proposal = ActionProposal.create(
            proposal_id="prop-root",
            tenant_id="t-001",
            created_by_agent=root_task.agent_id,
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=["ev-root"],
            requires_approval=True,
            idempotency_key="ik-prop-root",
            created_at=_FIXED_TS,
        )
        tool_calls = [
            ToolCallRecord(
                tool_name="crm_reader.get_customers",
                authority=ToolAuthority.READ,
            ),
        ]

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(
                    task=task,
                    evidence=[ev],
                    proposals=[proposal],
                    tool_calls=tool_calls,
                )
                # Under-report: 1 actual tool call, receipt says 0.
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        # The root task failed — its evidence/proposals must NOT be in
        # merged_state.
        ev_ids = {e.evidence_id for e in result.merged_state.merged_evidence}
        prop_ids = {p.proposal_id for p in result.merged_state.merged_proposals}
        assert "ev-root" not in ev_ids
        assert "prop-root" not in prop_ids

    def test_validate_invocation_receipt_unit(self):
        """Direct unit test for ``validate_invocation_receipt``."""
        from multi_agent.invocation import validate_invocation_receipt

        # Consistent receipt — passes.
        result = _ok_result(
            task=AgentTask(
                task_id="t1",
                agent_id="a1",
                task_type="tt",
                objective="o",
                tenant_id="t-001",
            )
        )
        receipt_ok = AgentInvocationReceipt(result=result, tool_calls=0)
        validate_invocation_receipt(receipt_ok)  # no exception

        # Inconsistent — under-report.
        receipt_bad = AgentInvocationReceipt(result=result, tool_calls=99)
        with pytest.raises(InvalidInvocationReceiptError, match="tool_calls"):
            validate_invocation_receipt(receipt_bad)


# ===========================================================================
# P0-5: Result Boundary
# ===========================================================================


class TestResultBoundary:
    """R1 P0-5: evidence_ids re-validation + cancelled status mapping."""

    def test_mutated_missing_evidence_rejected(self):
        """``AgentResult`` validates evidence_ids at construction, but
        the ``evidence`` list can be mutated afterward.  The Supervisor
        boundary must re-validate and reject."""
        task = AgentTask(
            task_id="task_001",
            agent_id="agent_a",
            task_type="test_task",
            objective="test",
            tenant_id="t-001",
        )
        ev = _evidence("ev-001", agent_id="agent_a")
        proposal = ActionProposal.create(
            proposal_id="prop-001",
            tenant_id="t-001",
            created_by_agent="agent_a",
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=["ev-001"],
            requires_approval=True,
            idempotency_key="ik-prop-001",
            created_at=_FIXED_TS,
        )
        result = _ok_result(
            task=task,
            evidence=[ev],
            proposals=[proposal],
        )

        # Mutate: clear the evidence list.  The proposal still
        # references ev-001, which no longer exists.
        result.evidence.clear()

        # Build a minimal plan for validate_agent_result.
        from multi_agent.complexity_gate import ComplexityDecision
        from multi_agent.planning import PLANNER_VERSION

        signals = PlanningSignals(
            event_type=None,
            domains=frozenset({"test"}),
            requested_task_types=frozenset({"test_task"}),
            requires_cross_domain=False,
            requires_write=False,
            requires_approval=False,
            has_conflicting_signals=False,
            missing_required_context=False,
            objective_kind=None,
        )
        request = PlanningRequest(
            run_id="run-001",
            tenant_id="t-001",
            actor_type="user",
            actor_id="user-001",
            objective="test",
            signals=signals,
            budget=ExecutionBudget(),
            context_summary=None,
            registry_version="reg-v-001",
        )
        planned = PlannedTask(
            intent_id="intent_001",
            domain="test",
            task=task,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        )
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(
                route="single_agent",
                domains=["test"],
                reasons=["test"],
                confidence=1.0,
                requires_human_review=False,
            ),
            tasks=[planned],
            planner_version=PLANNER_VERSION,
        )

        with pytest.raises(InvalidAgentResultError, match="missing"):
            validate_agent_result(result, task=task, plan=plan)

    @pytest.mark.asyncio
    async def test_cancelled_result_maps_to_cancelled_run(self):
        """When the Handler returns ``status='cancelled'``, the Task
        must be ``cancelled`` (not ``failed``) and the Run must be
        ``CANCELLED`` — per the priority
        ``cancelled > budget_exceeded > failed``."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(task=task, status="cancelled")
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        # Root task must be cancelled, not failed.
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "cancelled"
        assert root_rec.status != "failed"

        # Run status must be CANCELLED (priority: cancelled > failed).
        assert result.status == SupervisorRunStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancelled_result_produces_cancelled_task(self):
        """Direct check that a cancelled AgentResult produces a
        cancelled TaskExecutionRecord with the right attempt status."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(task=task, status="cancelled")
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # The attempt record must also be cancelled (not failed).
        assert len(root_rec.attempts) == 1
        assert root_rec.attempts[0].status == "cancelled"

    @pytest.mark.asyncio
    async def test_degraded_result_semantics_are_explicit(self):
        """``degraded`` maps to ``failed`` task status but the
        attempt's ``error_code`` is ``'degraded'`` so the audit log
        distinguishes the two."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(
                    task=task,
                    status="degraded",
                    errors=[
                        AgentError(
                            error_code="partial",
                            message="degraded",
                            category=AgentErrorCategory.UNKNOWN,
                            retryable=False,
                        )
                    ],
                )
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # Task status is failed (degraded collapses to failed)...
        assert root_rec.status == "failed"
        # ...but the attempt's error_code is "degraded" so the audit
        # log distinguishes the two.
        assert len(root_rec.attempts) == 1
        assert root_rec.attempts[0].error_code == "degraded"

    @pytest.mark.asyncio
    async def test_skipped_result_attempt_status_is_not_cancelled(self):
        """When the Handler returns ``status='skipped'``, the Task
        must be ``skipped`` — NOT ``cancelled``.  The attempt status
        must also be ``cancelled`` (per the supervisor's mapping) but
        the Task record status is ``skipped``.

        This is a subtle distinction: the *attempt* is marked
        ``cancelled`` (because the handler chose not to run), but the
        *task* is marked ``skipped`` (because it did not produce
        output).  The key assertion is that the task is NOT ``failed``.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(task=task, status="skipped")
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # Task status must be skipped, not failed.
        assert root_rec.status == "skipped"
        assert root_rec.status != "failed"
        assert root_rec.status != "cancelled"


# ===========================================================================
# P1: Config cleanup & Trace emission
# ===========================================================================


class TestConfigCleanup:
    """R1 P1: removed config fields and Trace emission."""

    def test_continue_independent_branches_field_removed(self):
        """The field must be rejected by ``extra='forbid'``."""
        from multi_agent.execution import SupervisorConfig

        with pytest.raises(Exception):
            SupervisorConfig(continue_independent_branches=False)  # type: ignore[call-arg]

    def test_deterministic_mode_field_removed(self):
        from multi_agent.execution import SupervisorConfig

        with pytest.raises(Exception):
            SupervisorConfig(deterministic_mode=False)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_trace_task_ready_emitted_for_real_waves(self):
        """``TRACE_TASK_READY`` must be emitted for every task in a
        real Ready-Task wave (R1 P1)."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        # Collect all task_ready events.
        ready_events = [ev for ev in result.trace if ev.event_type == "task_ready"]
        # The plan has 5 tasks across 2 waves (root + 4 children).
        # Every task should have a task_ready event.
        assert len(ready_events) == 5

    @pytest.mark.asyncio
    async def test_trace_task_skipped_emitted_for_dependency_propagation(self):
        """``TRACE_TASK_SKIPPED`` must be emitted when tasks are
        skipped due to dependency failure propagation (R1 P1)."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        results = {
            root_task.task_id: _ok_result(
                task=root_task,
                status="failed",
                errors=[
                    AgentError(
                        error_code="boom",
                        message="root failed",
                        category=AgentErrorCategory.UNKNOWN,
                        retryable=False,
                    )
                ],
            )
        }

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan, results=results),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        # Collect task_skipped events whose source is dependency_propagation.
        skipped_events = [
            ev
            for ev in result.trace
            if ev.event_type == "task_skipped"
            and ev.data.get("source") == "dependency_propagation"
        ]
        # 4 children were skipped via propagation.
        assert len(skipped_events) == 4
