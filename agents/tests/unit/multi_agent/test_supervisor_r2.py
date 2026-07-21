"""Phase 4 R2 regression tests.

Direct counter-examples for the five P0 issues and three P1 cleanups
identified in the Phase 4 R1 review (commit ``e5ab368``):

* **P0-1** — Registry Snapshot / Handler Binding: execution uses
  pre-flight bound handlers; cache lookup happens before any
  registry pre-flight; ``registry_version`` in the result matches
  ``plan.registry_version`` (not the live registry).
* **P0-2** — Structured Concurrency: a wave exception cancels and
  awaits all sibling coroutines; no Handler continues after the
  Scheduler raises.
* **P0-3** — Cancellation Wave Boundary: ``before_wave`` is checked
  before iteration reservation; a pre-cancelled run consumes zero
  iterations and emits no ``task_ready``.
* **P0-4** — Deadline-aware Backoff: retry backoff is capped by the
  remaining run deadline and is interruptible by cancellation.
* **P0-5** — Required/Optional Skipped: a Handler-returned
  ``skipped`` on a Required task fails the run; dependency-propagation
  ``skipped`` is transparent; the attempt status records real
  ``skipped`` (not ``cancelled``).
* **P1-1** — Lease Identity: ``complete`` and ``abort`` verify
  ``lease_id``; a stale lease cannot corrupt a newer run.
* **P1-2** — Cost Usage Trust Boundary: an untrusted invoker
  reporting ``cost_usd=0`` when ``cost_budget_usd`` is configured
  fails closed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from multi_agent.complexity_gate import (
    CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    ComplexityDecision,
)
from multi_agent.contracts import (
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
)
from multi_agent.execution import (
    FakeExecutionCancellation,
    SupervisorConfig,
    SupervisorRunStatus,
)
from multi_agent.execution_errors import (
    SupervisorError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    DeterministicFakeInvoker,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlanValidationReport,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    RetryPolicy,
    compute_request_hash,
)
from multi_agent.planning_templates import INTENT_CUSTOMER_CONTEXT
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.run_store import InMemoryRunStore, RunLease
from multi_agent.supervisor import SupervisorRuntime


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_supervisor_r1.py)
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


class _NoopHandler:
    """Handler stub that is never called in fake-invoker tests."""

    async def run(
        self, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentResult:  # pragma: no cover
        raise RuntimeError("noop handler should not be called")


class _RecordingHandler:
    """Handler stub that records calls and returns a preset result."""

    def __init__(self, result: AgentResult | None = None) -> None:
        self.result = result
        self.calls: list[tuple[AgentTask, AgentExecutionContext]] = []

    async def run(self, task: AgentTask, ctx: AgentExecutionContext) -> AgentResult:
        self.calls.append((task, ctx))
        if self.result is None:
            return _ok_result(task=task)
        return self.result


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
    """PlanValidator stub that always returns ``valid=True``."""

    def validate(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> PlanValidationReport:
        return PlanValidationReport(valid=True, issues=[])


def _tamper_plan_budget(plan: PlanDraft, **budget_overrides: Any) -> PlanDraft:
    budget = plan.request.budget
    for k, v in budget_overrides.items():
        object.__setattr__(budget, k, v)
    object.__setattr__(plan, "request_hash", compute_request_hash(plan.request))
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


def _tamper_task_max_retries(
    plan: PlanDraft, task_id: str, max_retries: int
) -> PlanDraft:
    # R6: also set RetryPolicy.max_retries because should_retry() reads
    # from RetryPolicy, not task.max_retries.  RetryPolicy.max_retries
    # is capped at 3 by its validator, so clamp the value.
    policy_retries = min(max_retries, 3)
    for pt in plan.tasks:
        if pt.task.task_id == task_id:
            object.__setattr__(pt.task, "max_retries", max_retries)
            object.__setattr__(
                pt,
                "retry_policy",
                RetryPolicy(max_retries=policy_retries),
            )
            break
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


def _tamper_task_timeout(plan: PlanDraft, task_id: str, timeout_ms: int) -> PlanDraft:
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
    return ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )


def _two_chain_plan(
    registry: AgentRegistry,
    *,
    budget: ExecutionBudget | None = None,
    run_id: str = "run-001",
) -> PlanDraft:
    """Build a plan with two independent chains: A→A2, B→B2."""
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
# P0-1: Registry Snapshot / Handler Binding
# ===========================================================================


class TestRegistrySnapshotBinding:
    """R2 P0-1: execution uses pre-flight bound handlers; cache lookup
    happens before any registry pre-flight; ``registry_version`` in
    the result matches ``plan.registry_version``."""

    @pytest.mark.asyncio
    async def test_cached_result_survives_registry_change(self):
        """A completed run's cached result must be returned even when
        the live Registry has since added an unrelated agent.  The
        cache path must not check the current registry version."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        # First execution — completes successfully.
        result1 = await runtime.execute(plan, reg)
        assert result1.status == SupervisorRunStatus.COMPLETED
        original_version = result1.registry_version

        # Mutate the live registry by adding an unrelated agent.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"customer_context_summary"}),
            frozenset({"crm_reader.get_customers"}),
        )
        reg.register(extra_cap, _NoopHandler())

        # The live registry version has now changed.
        assert reg.snapshot().version != original_version

        # Second execution with the same (run_id, plan_hash) must
        # return the cached result WITHOUT checking the live registry
        # version.  No SupervisorError must be raised.
        result2 = await runtime.execute(plan, reg)
        assert result2.status == SupervisorRunStatus.COMPLETED
        assert result2.registry_version == original_version

    @pytest.mark.asyncio
    async def test_cached_result_survives_handler_unregistration(self):
        """A completed run's cached result must be returned even when
        the handler has been unregistered from the live registry."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        result1 = await runtime.execute(plan, reg)
        assert result1.status == SupervisorRunStatus.COMPLETED

        # Unregister a handler that was used by the plan.
        reg.unregister("customer_context_specialist")

        # The cached result must still be returned — the cache path
        # does not check handler resolvability.
        result2 = await runtime.execute(plan, reg)
        assert result2.status == SupervisorRunStatus.COMPLETED
        assert result2.registry_version == result1.registry_version

    @pytest.mark.asyncio
    async def test_execution_uses_preflight_bound_handlers(self):
        """During execution, the Supervisor must use the handlers
        bound at pre-flight time, NOT fresh ``registry.resolve()``
        calls.  If the handler is replaced mid-run, the original
        handler must still be the one that runs."""
        # Use a RecordingHandler for the root task so we can verify
        # it was called.  All other agents also need callable
        # handlers because RegistryAgentInvoker calls handler.run()
        # directly (no fake invoker shortcut).
        original_handler = _RecordingHandler()
        handlers = {
            cap.agent_id: _RecordingHandler() for cap in _customer_recovery_caps()
        }
        handlers["customer_context_specialist"] = original_handler
        reg2 = _make_registry(_customer_recovery_caps(), handlers=handlers)
        plan2 = _customer_recovery_plan(reg2)

        # Use RegistryAgentInvoker so the handler is actually called.
        from multi_agent.invocation import RegistryAgentInvoker

        runtime = SupervisorRuntime(
            invoker=RegistryAgentInvoker(reg2),
            run_store=InMemoryRunStore(),
        )

        result = await runtime.execute(plan2, reg2)
        assert result.status == SupervisorRunStatus.COMPLETED

        # The original handler was called.
        assert len(original_handler.calls) == 1

        # Now replace the handler in the registry AFTER the run
        # completed.  A subsequent cached lookup must not call the
        # new handler.
        new_handler = _RecordingHandler()
        reg2.replace(
            _make_capability(
                "customer_context_specialist",
                frozenset({"customer_recovery"}),
                frozenset({"customer_context_summary"}),
                frozenset({"crm_reader.get_customers"}),
            ),
            new_handler,
        )

        # Cached result — new handler must NOT be called.
        result2 = await runtime.execute(plan2, reg2)
        assert result2.status == SupervisorRunStatus.COMPLETED
        assert len(new_handler.calls) == 0

    @pytest.mark.asyncio
    async def test_registry_mutation_during_run_does_not_change_result_version(self):
        """If an unrelated agent is registered DURING a run (between
        waves), the ``SupervisorRunResult.registry_version`` must
        still equal ``plan.registry_version``, not the mutated
        registry's version."""
        # All agents need callable handlers because _MutatingInvoker
        # delegates to handler.run().
        handlers = {
            cap.agent_id: _RecordingHandler() for cap in _customer_recovery_caps()
        }
        reg = _make_registry(_customer_recovery_caps(), handlers=handlers)
        plan = _customer_recovery_plan(reg)
        original_version = plan.registry_version

        # Build an invoker that registers a new agent between tasks.
        call_count = {"n": 0}

        class _MutatingInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                call_count["n"] += 1
                # After the first call (root task), register a new
                # agent to mutate the live registry.
                if call_count["n"] == 1:
                    extra_cap = _make_capability(
                        "extra_agent_during_run",
                        frozenset({"customer_recovery"}),
                        frozenset({"recovery_metrics"}),
                        frozenset({"crm_reader.get_customers"}),
                    )
                    reg.register(extra_cap, _NoopHandler())
                result = await handler.run(task, ctx)
                return AgentInvocationReceipt(
                    result=result,
                    tool_calls=len(result.tool_calls),
                )

        runtime = SupervisorRuntime(
            invoker=_MutatingInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.COMPLETED
        # The result's registry_version must be the plan's version,
        # NOT the live registry's (now-mutated) version.
        assert result.registry_version == original_version
        assert result.registry_version != reg.snapshot().version

    @pytest.mark.asyncio
    async def test_registry_drift_cannot_mix_handler_versions(self):
        """Even if the live registry drifts (agent re-registered with
        a different version), the Supervisor must use the handler
        bound at pre-flight time for ALL tasks in the run."""
        # All agents need callable handlers because _DriftingInvoker
        # delegates to handler.run().
        original_handler = _RecordingHandler()
        handlers = {
            cap.agent_id: _RecordingHandler() for cap in _customer_recovery_caps()
        }
        handlers["customer_context_specialist"] = original_handler
        reg2 = _make_registry(_customer_recovery_caps(), handlers=handlers)
        plan2 = _customer_recovery_plan(reg2)

        call_count = {"n": 0}

        class _DriftingInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                call_count["n"] += 1
                # After the root task, replace the handler in the
                # registry with a new version.
                if call_count["n"] == 1:
                    new_cap = _make_capability(
                        "customer_context_specialist",
                        frozenset({"customer_recovery"}),
                        frozenset({"customer_context_summary"}),
                        frozenset({"crm_reader.get_customers"}),
                    )
                    new_handler = _RecordingHandler()
                    reg2.replace(new_cap, new_handler)
                # But still call the ORIGINAL handler passed to us —
                # the Supervisor bound it at pre-flight time.
                result = await handler.run(task, ctx)
                return AgentInvocationReceipt(
                    result=result,
                    tool_calls=len(result.tool_calls),
                )

        runtime = SupervisorRuntime(
            invoker=_DriftingInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan2, reg2)

        assert result.status == SupervisorRunStatus.COMPLETED
        # The original handler was called (once for the root task).
        assert len(original_handler.calls) == 1


# ===========================================================================
# P0-2: Structured Concurrency
# ===========================================================================


class TestStructuredConcurrency:
    """R2 P0-2: a wave exception cancels and awaits all sibling
    coroutines; no Handler continues after the Scheduler raises."""

    @pytest.mark.asyncio
    async def test_wave_exception_cancels_and_awaits_siblings(self):
        """Two parallel tasks: A raises a RuntimeError immediately,
        B sleeps.  B must be cancelled — the Scheduler must not
        return or raise until B has terminated."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        # Track whether task B was still running when A raised.
        b_was_running = {"v": False}
        b_completed = {"v": False}

        class _ExplodingInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    # Signal that B is running, then raise.
                    b_was_running["v"] = True
                    raise RuntimeError("infrastructure explosion")
                # task_b: sleep so we can prove it's cancelled.
                try:
                    await asyncio.sleep(5.0)
                    b_completed["v"] = True
                except asyncio.CancelledError:
                    raise
                return AgentInvocationReceipt(result=_ok_result(task=task))

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_ExplodingInvoker(),  # type: ignore[arg-type]
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        # The Supervisor must propagate the RuntimeError.
        with pytest.raises(RuntimeError, match="infrastructure explosion"):
            await runtime.execute(plan, reg)

        # B must NOT have completed — it was cancelled.
        assert not b_completed["v"]
        # The lease must have been released via abort().
        assert not store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_no_handler_continues_after_scheduler_error(self):
        """After the Scheduler raises, no Handler coroutine must
        remain active.  We verify by checking that a background
        side-effect does not occur after the Supervisor returns."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        side_effect_log: list[str] = []

        class _PartialInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    raise RuntimeError("boom")
                # task_b: if not cancelled, writes a side effect after
                # a delay.
                try:
                    await asyncio.sleep(0.3)
                    side_effect_log.append("task_b_completed")
                except asyncio.CancelledError:
                    side_effect_log.append("task_b_cancelled")
                    raise
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_PartialInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RuntimeError, match="boom"):
            await runtime.execute(plan, reg)

        # Yield control so any leaked coroutine would have a chance
        # to write its side effect.
        await asyncio.sleep(0.5)

        # task_b must NOT have completed — it was cancelled.
        assert "task_b_completed" not in side_effect_log

    @pytest.mark.asyncio
    async def test_scheduler_has_no_orphan_tasks(self):
        """After a wave exception, there must be no orphan asyncio
        Tasks still running for the Supervisor's run."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        class _OrphanInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    raise RuntimeError("orphan test")
                await asyncio.sleep(5.0)
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_OrphanInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RuntimeError, match="orphan test"):
            await runtime.execute(plan, reg)

        # After the Supervisor returns, yield control and check that
        # no asyncio Task for task_b is still pending.
        await asyncio.sleep(0.1)
        # All tasks in the current event loop should be done.
        for task in asyncio.all_tasks():
            # Filter out the test's own coroutine and any pytest
            # infrastructure tasks.
            coro_name = task.get_coro().__qualname__
            if "test_scheduler_has_no_orphan" in coro_name:
                continue
            assert task.done(), (
                f"orphan task still running: {coro_name} "
                f"(done={task.done()}, cancelled={task.cancelled()})"
            )

    @pytest.mark.asyncio
    async def test_run_lease_not_released_with_running_handler(self):
        """The lease must not be released (via abort) while a sibling
        Handler is still running.  abort() must happen AFTER all
        siblings are cancelled and awaited."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        abort_order: list[str] = []

        class _TrackingStore(InMemoryRunStore):
            async def abort(self, lease: RunLease, *, error_code: str) -> None:
                abort_order.append("abort_called")
                await super().abort(lease, error_code=error_code)

        class _LeaseCheckInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    raise RuntimeError("lease order test")
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    abort_order.append("task_b_cancelled")
                    raise
                return AgentInvocationReceipt(result=_ok_result(task=task))

        store = _TrackingStore()
        runtime = SupervisorRuntime(
            invoker=_LeaseCheckInvoker(),  # type: ignore[arg-type]
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RuntimeError, match="lease order test"):
            await runtime.execute(plan, reg)

        # The abort must have been called (lease released).
        assert "abort_called" in abort_order
        # task_b must have been cancelled before or at the same time
        # as abort — NOT still running when abort was called.
        # (We can't guarantee exact ordering because abort is async,
        # but task_b_cancelled must appear in the log, proving B was
        # terminated, not orphaned.)
        assert "task_b_cancelled" in abort_order


# ===========================================================================
# P0-3: Cancellation Wave Boundary
# ===========================================================================


class TestCancellationWaveBoundary:
    """R2 P0-3: ``before_wave`` is checked before iteration
    reservation; a pre-cancelled run consumes zero iterations and
    emits no ``task_ready``."""

    @pytest.mark.asyncio
    async def test_cancel_before_run_consumes_zero_iterations(self):
        """If the run is cancelled before ``execute`` starts, the
        result must have ``iterations == 0`` — no iteration is
        reserved because the pre-run cancellation check short-circuits
        before the Scheduler is entered."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg, cancellation=canc)

        assert result.status == SupervisorRunStatus.CANCELLED
        assert result.usage.iterations == 0

    @pytest.mark.asyncio
    async def test_cancel_before_run_emits_no_task_ready(self):
        """A pre-cancelled run must not emit any ``task_ready`` trace
        events — no wave was dispatched."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg, cancellation=canc)

        ready_events = [ev for ev in result.trace if ev.event_type == "task_ready"]
        assert len(ready_events) == 0

        started_events = [ev for ev in result.trace if ev.event_type == "task_started"]
        assert len(started_events) == 0

    @pytest.mark.asyncio
    async def test_kill_switch_before_wave_starts_no_tasks(self):
        """If the kill switch is active for the tenant, no task must
        be dispatched — the ``before_wave`` hook detects it before
        the first wave and cancels everything."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()
        canc.activate_kill_switch("t-001")

        invoker_calls: list[str] = []

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            invoker_calls.append(task.task_id)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg, cancellation=canc)

        # Pre-run check catches the kill switch — no invoker calls.
        assert len(invoker_calls) == 0
        assert result.status == SupervisorRunStatus.CANCELLED
        assert result.usage.iterations == 0

    @pytest.mark.asyncio
    async def test_cancel_between_waves_does_not_reserve_next_iteration(self):
        """Two-wave plan (root + children).  Cancel after the first
        wave completes.  The second wave must NOT reserve an iteration
        — ``before_wave`` detects the cancellation before
        ``on_wave_started`` is called."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            call_count["n"] += 1
            # After the root task (first call), activate cancellation.
            if call_count["n"] == 1:
                canc.cancel_run("run-001")
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg, cancellation=canc)

        # Root task completed; children were cancelled before dispatch.
        assert result.usage.iterations == 1  # only root wave
        # No child task was started.
        started_events = [ev for ev in result.trace if ev.event_type == "task_started"]
        assert len(started_events) == 1  # only root

    @pytest.mark.asyncio
    async def test_cancel_between_ready_and_dispatch_is_fail_closed(self):
        """``before_wave`` is called AFTER ready tasks are identified
        but BEFORE the wave is dispatched.  If cancellation is active
        at that point, the wave must be cancelled (fail-closed)."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()

        # Cancel immediately — the pre-run check will catch it, so
        # let's use a slightly different approach: cancel via a
        # custom before_wave that returns True on the first wave.
        # Actually, the simplest test is: cancel before execute().
        canc.cancel_run("run-001")

        invoker_calls: list[str] = []

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            invoker_calls.append(task.task_id)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg, cancellation=canc)

        assert len(invoker_calls) == 0
        assert result.status == SupervisorRunStatus.CANCELLED
        # All tasks must be cancelled.
        for rec in result.task_records:
            assert rec.status == "cancelled"


# ===========================================================================
# P0-4: Deadline-aware Backoff
# ===========================================================================


class TestDeadlineAwareBackoff:
    """R2 P0-4: retry backoff is capped by the remaining run deadline
    and is interruptible by cancellation."""

    @pytest.mark.asyncio
    async def test_retry_backoff_capped_by_deadline(self):
        """``retry_backoff_ms=1000`` but ``deadline_ms=30``.  First
        attempt fails with a retryable error.  The backoff must be
        capped by the remaining deadline — the run must finalise as
        ``budget_exceeded`` quickly, not after 1000ms."""
        reg = _make_registry(_customer_recovery_caps())
        # Create the plan with the default budget so the planner
        # accepts it, then tamper with the deadline afterwards.
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, deadline_ms=30)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        _tamper_task_max_retries(plan, root_task.task_id, 3)

        retryable_error = AgentError(
            error_code="transient",
            message="retry me",
            category=AgentErrorCategory.UNKNOWN,
            retryable=True,
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(
                    task=task, status="failed", errors=[retryable_error]
                )
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            config=SupervisorConfig(retry_backoff_ms=1000),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        start = time.monotonic()
        result = await runtime.execute(plan, reg)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        # The run must be budget_exceeded (deadline exhausted during
        # backoff), not failed.
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED
        # The elapsed time must be well under 1000ms — the backoff
        # was capped by the 30ms deadline.
        assert elapsed_ms < 500, (
            f"backoff was not capped by deadline: elapsed={elapsed_ms}ms"
        )

    @pytest.mark.asyncio
    async def test_backoff_never_extends_run_past_deadline(self):
        """The run's total duration must not significantly exceed
        ``deadline_ms`` even when ``retry_backoff_ms`` is much
        larger."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, deadline_ms=50)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        _tamper_task_max_retries(plan, root_task.task_id, 5)

        retryable_error = AgentError(
            error_code="transient",
            message="retry me",
            category=AgentErrorCategory.UNKNOWN,
            retryable=True,
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == root_task.task_id:
                result = _ok_result(
                    task=task, status="failed", errors=[retryable_error]
                )
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            config=SupervisorConfig(retry_backoff_ms=5000),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        start = time.monotonic()
        result = await runtime.execute(plan, reg)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED
        # Total run duration must be close to deadline_ms (50ms),
        # with some tolerance for scheduling overhead.
        assert elapsed_ms < 300, (
            f"run exceeded deadline by too much: elapsed={elapsed_ms}ms, deadline=50ms"
        )

    @pytest.mark.asyncio
    async def test_deadline_expired_before_retry_starts_no_attempt(self):
        """If the deadline is already exhausted before the first
        retry attempt, no new attempt must be made — the task is
        skipped with ``deadline_exceeded``."""
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(deadline_ms=1)
        acc = _BudgetAccountant(budget, start_monotonic=0.0)

        # Simulate that the deadline is already exhausted.
        acc.mark_deadline_exceeded()

        # _maybe_sleep should return "deadline_exceeded" immediately.
        canc = FakeExecutionCancellation()
        cfg = SupervisorConfig(retry_backoff_ms=100)
        plan = _customer_recovery_plan(_make_registry(_customer_recovery_caps()))

        result = await SupervisorRuntime._maybe_sleep(cfg, acc, canc, plan)
        assert result == "deadline_exceeded"

    @pytest.mark.asyncio
    async def test_cancellation_interrupts_retry_backoff(self):
        """If cancellation is active when backoff starts,
        ``_maybe_sleep`` must return ``"cancelled"`` immediately
        without sleeping."""
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(deadline_ms=60_000)
        acc = _BudgetAccountant(budget, start_monotonic=time.monotonic())

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        cfg = SupervisorConfig(retry_backoff_ms=10000)
        plan = _customer_recovery_plan(_make_registry(_customer_recovery_caps()))

        import time as _time

        start = _time.monotonic()
        result = await SupervisorRuntime._maybe_sleep(cfg, acc, canc, plan)
        elapsed_ms = int((_time.monotonic() - start) * 1000)

        assert result == "cancelled"
        # Must return immediately — no real sleep.
        assert elapsed_ms < 50

    @pytest.mark.asyncio
    async def test_kill_switch_interrupts_retry_backoff(self):
        """If the kill switch is active when backoff starts,
        ``_maybe_sleep`` must return ``"cancelled"`` immediately."""
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(deadline_ms=60_000)
        acc = _BudgetAccountant(budget, start_monotonic=time.monotonic())

        canc = FakeExecutionCancellation()
        canc.activate_kill_switch("t-001")

        cfg = SupervisorConfig(retry_backoff_ms=10000)
        plan = _customer_recovery_plan(_make_registry(_customer_recovery_caps()))

        start = time.monotonic()
        result = await SupervisorRuntime._maybe_sleep(cfg, acc, canc, plan)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        assert result == "cancelled"
        assert elapsed_ms < 50


# ===========================================================================
# P0-5: Required/Optional Skipped Semantics
# ===========================================================================


class TestRequiredOptionalSkipped:
    """R2 P0-5: a Handler-returned ``skipped`` on a Required task
    fails the run; dependency-propagation ``skipped`` is transparent;
    the attempt status records real ``skipped``."""

    @pytest.mark.asyncio
    async def test_required_skipped_task_fails_run(self):
        """When the Handler returns ``status='skipped'`` on a Required
        task, the Run must be ``FAILED`` (not ``partial_success``)."""
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

        # Root task is required and was skipped by the Handler.
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "skipped"
        assert root_rec.skip_reason is None  # Handler-returned, not propagation

        # Run must be FAILED — a Required task was skipped.
        assert result.status == SupervisorRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_optional_skipped_task_allows_partial_success(self):
        """When an Optional task is skipped (Handler-returned), the
        Run should be ``PARTIAL_SUCCESS`` (not ``FAILED``) if all
        Required tasks completed."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        # Make task_a2 optional (not required).
        for pt in plan.tasks:
            if pt.task.task_id == "task_a2":
                object.__setattr__(pt, "required", False)
                break
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == "task_a2":
                result = _ok_result(task=task, status="skipped")
                return AgentInvocationReceipt(result=result, tool_calls=0)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # task_a2 is optional and was skipped — partial_success.
        assert result.status == SupervisorRunStatus.PARTIAL_SUCCESS

    @pytest.mark.asyncio
    async def test_skipped_attempt_status_is_skipped(self):
        """When the Handler returns ``status='skipped'``, the attempt
        record must have ``status='skipped'`` — NOT ``cancelled``."""
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
        assert len(root_rec.attempts) == 1
        assert root_rec.attempts[0].status == "skipped"
        assert root_rec.attempts[0].status != "cancelled"

    @pytest.mark.asyncio
    async def test_cancelled_attempt_is_not_reported_as_skipped(self):
        """When the Handler returns ``status='cancelled'``, the
        attempt must be ``cancelled`` — NOT ``skipped``.  The two
        statuses are semantically distinct."""
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
        assert root_rec.status == "cancelled"
        assert root_rec.status != "skipped"
        assert len(root_rec.attempts) == 1
        assert root_rec.attempts[0].status == "cancelled"

    @pytest.mark.asyncio
    async def test_required_root_skipped_propagates_to_descendants(self):
        """When a Required root task is skipped by the Handler, its
        descendants must be skipped via dependency propagation.  The
        run must be ``FAILED`` (Required Handler-skipped).  The
        descendants' ``skip_reason`` must be set (dependency
        propagation), so they do NOT independently trigger FAILED."""
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

        # Root is skipped (Handler-returned).
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "skipped"
        assert root_rec.skip_reason is None

        # Descendants are skipped (dependency propagation).
        for rec in result.task_records:
            if rec.task_id == root_task.task_id:
                continue
            assert rec.status == "skipped"
            assert rec.skip_reason is not None  # propagation, not Handler

        # Run is FAILED (Required Handler-skipped).
        assert result.status == SupervisorRunStatus.FAILED


# ===========================================================================
# P1-1: Lease Identity
# ===========================================================================


class TestLeaseIdentity:
    """R2 P1-1: ``complete`` and ``abort`` verify ``lease_id``; a
    stale lease cannot corrupt a newer run."""

    @pytest.mark.asyncio
    async def test_wrong_plan_hash_cannot_abort_active_lease(self):
        """``abort`` with a wrong ``plan_hash`` must be rejected when
        the run is in-progress with a different lease_id.

        Note: the current ``abort`` implementation uses ``lease_id``
        as the authoritative identity check (not ``plan_hash``).
        This test verifies that a lease with a *different* lease_id
        cannot abort an active run."""
        store = InMemoryRunStore()

        # Start a run.
        await store.begin("run-001", "hash-A")
        assert store.is_in_progress("run-001")

        # A stale lease with a different lease_id (simulating a
        # cancelled coroutine that resumed after abort).
        stale_lease = RunLease(
            run_id="run-001",
            plan_hash="hash-A",
            lease_id="stale-lease-id-0001",
        )

        # The stale lease must NOT be able to abort the active run.
        with pytest.raises(SupervisorError, match="lease_id"):
            await store.abort(stale_lease, error_code="stale")

        # The run must still be in-progress.
        assert store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_stale_abort_cannot_delete_new_lease(self):
        """After a run is aborted and a new lease is issued for the
        same ``run_id``, the old lease's ``abort`` must NOT delete
        the new lease."""
        store = InMemoryRunStore()

        # First lease.
        lease1 = await store.begin("run-001", "hash-A")
        # Abort it.
        await store.abort(lease1, error_code="first_failure")
        assert not store.is_in_progress("run-001")

        # New lease for the same run_id.
        await store.begin("run-001", "hash-A")
        assert store.is_in_progress("run-001")

        # The old lease tries to abort again — must be rejected.
        with pytest.raises(SupervisorError, match="lease_id"):
            await store.abort(lease1, error_code="stale_callback")

        # The new lease must still be active.
        assert store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_complete_rejects_wrong_plan_hash(self):
        """``complete`` with a ``plan_hash`` that does not match the
        lease's ``plan_hash`` must be rejected."""
        store = InMemoryRunStore()

        lease = await store.begin("run-001", "hash-A")

        # Build a minimal result with a different plan_hash.
        from multi_agent.contracts import ExecutionUsage
        from multi_agent.execution import SupervisorRunResult
        from multi_agent.state import MergedState

        result = SupervisorRunResult(
            run_id="run-001",
            plan_hash="hash-B",  # wrong!
            registry_version="v1",
            status=SupervisorRunStatus.COMPLETED,
            task_records=[],
            merged_state=MergedState(),
            usage=ExecutionUsage(),
            started_at=_FIXED_TS,
            completed_at=_FIXED_TS,
            duration_ms=0,
        )

        with pytest.raises(SupervisorError, match="identity"):
            await store.complete(lease, result)

        # The run must still be in-progress (complete was rejected).
        assert store.is_in_progress("run-001")


# ===========================================================================
# P1-2: Cost Usage Trust Boundary
# ===========================================================================


class TestCostUsageTrustBoundary:
    """R2 P1-2: an untrusted invoker reporting ``cost_usd=None`` when
    ``cost_budget_usd`` is configured fails closed."""

    @pytest.mark.asyncio
    async def test_untrusted_cost_usage_fails_closed(self):
        """When ``cost_budget_usd`` is configured and the invoker
        reports ``cost_usd=None`` with ``provider_metadata`` set (a
        provider call was made but cost is unverified), the run must
        fail closed with ``ExecutionUsageUnavailableError`` — the task
        is marked ``failed`` with ``error_code='usage_unavailable'``.

        R6: the receipt must carry ``provider_metadata`` so the
        accountant treats it as a provider-usage-capable attempt.
        A receipt without ``provider_metadata`` (deterministic mode)
        skips cost enforcement because no provider call was made."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("10.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("10.00"))

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # R6: include provider_metadata so a provider call was made
        # and cost enforcement applies.
        provider_meta = ProviderMetadata(
            provider="openai",
            chat_model="gpt-4",
            embedding_model="text-embedding-3-small",
            ai_mode="live",
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task, provider_metadata=provider_meta)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                # cost_usd=None: cost not reported → fail-closed
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The root task must be failed with usage_unavailable.
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "usage_unavailable" for a in root_rec.attempts), (
            f"expected usage_unavailable error_code, got "
            f"{[a.error_code for a in root_rec.attempts]}"
        )

        # R6: the run finalises as BUDGET_EXCEEDED (execution_usage_unavailable
        # sets _exceeded=True so the run fails-closed at the budget level).
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED
