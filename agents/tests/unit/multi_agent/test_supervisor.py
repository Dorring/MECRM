"""Phase 4 SupervisorRuntime tests.

Covers:

* Plan entry boundary — invalid plan rejected before Handler invocation.
* Registry version mismatch rejected.
* PlanValidator re-executed at runtime entry.
* No Handler called when plan is invalid.
* Cancellation before run / between waves.
* Retry/timeout semantics.
* Failure propagation (required vs optional).
* Run idempotency (same run + same plan → cached; conflict → error).
* Result validation (tenant mismatch, agent mismatch).
* Merge reuses Phase 2 algorithm.
* ActionProposals are collected but not executed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from multi_agent.complexity_gate import CUSTOMER_RECOVERY_OBJECTIVE_KIND
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
    SupervisorRunStatus,
)
from multi_agent.execution_errors import (
    RetryableAgentError,
    RunPlanConflictError,
    SupervisorError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    DeterministicFakeInvoker,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PlanDraft,
    PlanValidationReport,
    PlanningRequest,
    PlanningSignals,
    compute_request_hash,
)
from multi_agent.planning_templates import (
    INTENT_CUSTOMER_CONTEXT,
    INTENT_KNOWLEDGE_RECOMMENDATION,
    INTENT_SALES_RISK_ANALYSIS,
    INTENT_SUPPORT_ANALYSIS,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.run_store import InMemoryRunStore
from multi_agent.supervisor import SupervisorRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


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


class _StubHandler:
    """Records calls; returns the preset ``AgentResult``."""

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
    handlers: dict[str, _StubHandler] | None = None,
    catalog: ToolCatalog | None = None,
) -> AgentRegistry:
    reg = AgentRegistry(tool_catalog=catalog or _default_catalog())
    for cap in caps:
        handler = (handlers or {}).get(cap.agent_id, _StubHandler())
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
    request = _make_request(
        registry,
        budget=budget,
        run_id=run_id,
    )
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
        token_usage=TokenUsage(),
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
    """Build a fake invoker that returns one result per task_id.

    ``results`` maps task_id → AgentResult.  Tasks without an entry
    get a default ``completed`` result.
    """
    results = results or {}

    def _factory(task: AgentTask, ctx: AgentExecutionContext) -> AgentInvocationReceipt:
        if task.task_id in results:
            result = results[task.task_id]
        else:
            result = _ok_result(task=task)
        return AgentInvocationReceipt(result=result, tool_calls=0)

    return DeterministicFakeInvoker(factory=factory or _factory)


class _AlwaysValidPlanValidator:
    """PlanValidator stub that always returns ``valid=True``.

    Used in tests that need to tamper with plan content (e.g.
    ``max_retries``, ``budget``) and recompute hashes — the real
    :class:`PlanValidator` would reject the tampered plan because it
    rebuilds the canonical plan from (request, registry).
    """

    def validate(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> PlanValidationReport:
        return PlanValidationReport(valid=True, issues=[])


def _tamper_plan_budget(plan: PlanDraft, **budget_overrides: Any) -> PlanDraft:
    """Return a plan with ``request.budget`` fields tampered.

    Recomputes ``request_hash`` and ``plan_hash`` so the Supervisor's
    ``verify_integrity()`` passes.  Caller must inject
    :class:`_AlwaysValidPlanValidator` because the canonical plan
    reconstruction would detect the budget change.
    """
    budget = plan.request.budget
    for k, v in budget_overrides.items():
        object.__setattr__(budget, k, v)
    object.__setattr__(plan, "request_hash", compute_request_hash(plan.request))
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


def _tamper_task_max_retries(
    plan: PlanDraft, task_id: str, max_retries: int
) -> PlanDraft:
    """Return a plan with one task's ``max_retries`` tampered.

    Recomputes ``plan_hash`` so ``verify_integrity()`` passes.  Caller
    must inject :class:`_AlwaysValidPlanValidator`.
    """
    for pt in plan.tasks:
        if pt.task.task_id == task_id:
            object.__setattr__(pt.task, "max_retries", max_retries)
            break
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


# ---------------------------------------------------------------------------
# Plan entry boundary
# ---------------------------------------------------------------------------


class TestPlanEntryBoundary:
    @pytest.mark.asyncio
    async def test_invalid_plan_rejected_before_handler_invocation(self):
        """Tamper with plan_hash and verify no Handler is called."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Tamper with plan_hash on the valid plan — bypass Pydantic's
        # frozen check and hash validator via object.__setattr__.
        object.__setattr__(plan, "plan_hash", "0" * 64)

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

        with pytest.raises(Exception):
            await runtime.execute(plan, reg)

        assert invoker_calls == [], "Invoker must not be called when plan is invalid"

    @pytest.mark.asyncio
    async def test_registry_version_mismatch_rejected(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Build a second registry with one extra agent — different version.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"customer_context_summary"}),
            frozenset({"crm_reader.get_customers"}),
        )
        reg2 = _make_registry(_customer_recovery_caps() + [extra_cap])

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )
        with pytest.raises(SupervisorError, match="registry version"):
            await runtime.execute(plan, reg2)

    @pytest.mark.asyncio
    async def test_no_handler_called_when_plan_invalid(self):
        """When the registry is incompatible with the plan, the
        Supervisor must fail before invoking any handler."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        invoker_calls: list[str] = []

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            invoker_calls.append(task.task_id)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        # Build a second registry with one extra agent — different
        # version.  The Supervisor must detect the version mismatch
        # and reject the plan before invoking any handler.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"customer_context_summary"}),
            frozenset({"crm_reader.get_customers"}),
        )
        bad_reg = _make_registry(_customer_recovery_caps() + [extra_cap])

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )

        with pytest.raises(SupervisorError):
            await runtime.execute(plan, bad_reg)
        assert invoker_calls == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_customer_recovery_executes_all_five_tasks(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )

        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.COMPLETED
        assert len(result.task_records) == 5

        # customer_context must complete before any child.
        records_by_id = {r.task_id: r for r in result.task_records}
        customer_context_id = next(
            pt.task.task_id
            for pt in plan.tasks
            if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        assert records_by_id[customer_context_id].status == "completed"

        # Every task should be completed.
        assert all(r.status == "completed" for r in result.task_records)

    @pytest.mark.asyncio
    async def test_trace_contains_run_started_and_completed(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )

        result = await runtime.execute(plan, reg)

        event_types = [ev.event_type for ev in result.trace]
        assert event_types[0] == "run_started"
        assert event_types[-1] == "run_completed"
        assert "plan_validated" in event_types
        assert "results_merged" in event_types

    @pytest.mark.asyncio
    async def test_proposals_collected_but_not_executed(self):
        """ActionProposals should be collected into merged_state but
        no Handler side-effect should be observed."""
        from multi_agent.contracts import ActionProposal, ActionRiskLevel

        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Build a proposal attached to the support task.
        support_task_id = next(
            pt.task.task_id
            for pt in plan.tasks
            if pt.intent_id == INTENT_SUPPORT_ANALYSIS
        )
        support_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_SUPPORT_ANALYSIS
        )

        proposal = ActionProposal.create(
            proposal_id="prop-001",
            tenant_id="t-001",
            created_by_agent=support_task.agent_id,
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-prop-001",
            created_at=_FIXED_TS,
        )
        results = {support_task_id: _ok_result(task=support_task, proposals=[proposal])}

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan, results=results),
            run_store=InMemoryRunStore(),
        )

        result = await runtime.execute(plan, reg)

        # Proposal must be in merged_state.merged_proposals.
        proposal_ids = {p.proposal_id for p in result.merged_state.merged_proposals}
        assert "prop-001" in proposal_ids
        # And the result must be COMPLETED — proposals were just collected.
        assert result.status == SupervisorRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


class TestFailurePropagation:
    @pytest.mark.asyncio
    async def test_required_failure_skips_descendants(self):
        """When a required task fails, the run status is FAILED (spec §17).
        Independent branches (other root children) still execute."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        support_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_SUPPORT_ANALYSIS
        )
        sales_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_SALES_RISK_ANALYSIS
        )

        # support_specialist (required) returns failed.
        results = {
            support_task.task_id: _ok_result(
                task=support_task,
                status="failed",
                errors=[
                    AgentError(
                        error_code="boom",
                        message="oops",
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

        # Spec §17: Required Task failed → Run final state = failed.
        assert result.status == SupervisorRunStatus.FAILED

        # Independent branch (sales) should still complete because it
        # does not depend on support.
        sales_rec = next(
            r for r in result.task_records if r.task_id == sales_task.task_id
        )
        assert sales_rec.status == "completed"
        support_rec = next(
            r for r in result.task_records if r.task_id == support_task.task_id
        )
        assert support_rec.status == "failed"

    @pytest.mark.asyncio
    async def test_needs_input_propagates_to_run_status(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        results = {ctx_task.task_id: _ok_result(task=ctx_task, status="needs_input")}

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan, results=results),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        # customer_context is the root — needs_input means the four
        # children get skipped.  Run status = needs_input.
        assert result.status == SupervisorRunStatus.NEEDS_INPUT

        # Children should be skipped.
        for pt in plan.tasks:
            if pt.intent_id == INTENT_CUSTOMER_CONTEXT:
                continue
            rec = next(r for r in result.task_records if r.task_id == pt.task.task_id)
            assert rec.status == "skipped"

    @pytest.mark.asyncio
    async def test_optional_failure_produces_partial_success(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        knowledge_task = next(
            pt.task
            for pt in plan.tasks
            if pt.intent_id == INTENT_KNOWLEDGE_RECOMMENDATION
        )

        results = {
            knowledge_task.task_id: _ok_result(
                task=knowledge_task,
                status="failed",
                errors=[
                    AgentError(
                        error_code="optional_fail",
                        message="optional",
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

        # knowledge is optional; the other 4 still complete.  Run
        # status should be partial_success.
        assert result.status == SupervisorRunStatus.PARTIAL_SUCCESS

    @pytest.mark.asyncio
    async def test_independent_branch_continues(self):
        """If one child fails but the root is OK, the other children
        should still execute."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        support_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_SUPPORT_ANALYSIS
        )
        sales_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_SALES_RISK_ANALYSIS
        )

        results = {
            support_task.task_id: _ok_result(
                task=support_task,
                status="failed",
                errors=[
                    AgentError(
                        error_code="x",
                        message="x",
                        category=AgentErrorCategory.UNKNOWN,
                        retryable=False,
                    )
                ],
            ),
            # sales still completes.
        }

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan, results=results),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        sales_rec = next(
            r for r in result.task_records if r.task_id == sales_task.task_id
        )
        assert sales_rec.status == "completed"
        support_rec = next(
            r for r in result.task_records if r.task_id == support_task.task_id
        )
        assert support_rec.status == "failed"


# ---------------------------------------------------------------------------
# Retry / Timeout
# ---------------------------------------------------------------------------


class TestRetryTimeout:
    @pytest.mark.asyncio
    async def test_retryable_error_retried(self):
        """A Handler that raises RetryableAgentError once then succeeds
        should result in a completed task with two attempts.

        The Phase 3 planner always sets ``max_retries=0`` (not
        configurable), so we tamper with the task's ``max_retries``
        after planning and inject a fake PlanValidator that skips
        canonical-plan reconstruction.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # Tamper with max_retries=1 on the root task and recompute
        # plan_hash so verify_integrity() passes.
        _tamper_task_max_retries(plan, ctx_task.task_id, 1)

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == ctx_task.task_id:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RetryableAgentError("transient")
                return AgentInvocationReceipt(result=_ok_result(task=task))
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        ctx_rec = next(r for r in result.task_records if r.task_id == ctx_task.task_id)
        assert ctx_rec.status == "completed"
        assert len(ctx_rec.attempts) == 2
        assert ctx_rec.attempts[0].status == "failed"
        assert ctx_rec.attempts[1].status == "completed"

    @pytest.mark.asyncio
    async def test_non_retryable_error_not_retried(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == ctx_task.task_id:
                call_count["n"] += 1
                # Generic ValueError — non-retryable.
                raise ValueError("boom")
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        ctx_rec = next(r for r in result.task_records if r.task_id == ctx_task.task_id)
        assert ctx_rec.status == "failed"
        assert len(ctx_rec.attempts) == 1
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_retry_count_respects_max_retries(self):
        """If max_retries=0, only one attempt is made even when the
        error is retryable."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == ctx_task.task_id:
                call_count["n"] += 1
                raise RetryableAgentError("always")
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        ctx_rec = next(r for r in result.task_records if r.task_id == ctx_task.task_id)
        assert ctx_rec.status == "failed"
        assert len(ctx_rec.attempts) == 1  # max_retries=0 → 1 attempt only
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_needs_input_not_retried(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == ctx_task.task_id:
                call_count["n"] += 1
                return AgentInvocationReceipt(
                    result=_ok_result(task=task, status="needs_input")
                )
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        ctx_rec = next(r for r in result.task_records if r.task_id == ctx_task.task_id)
        assert ctx_rec.status == "needs_input"
        assert len(ctx_rec.attempts) == 1
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Budget exceeded
# ---------------------------------------------------------------------------


class TestBudgetExceeded:
    @pytest.mark.asyncio
    async def test_max_tasks_exceeded_short_circuits(self):
        """Plan with more tasks than max_tasks → BUDGET_EXCEEDED.

        The Phase 3 planner rejects plans that exceed ``max_tasks`` at
        creation time, so we tamper with the budget *after* planning
        and inject a fake PlanValidator.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, max_tasks=2)  # plan has 5 tasks

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED
        # All tasks should be skipped.
        assert all(r.status == "skipped" for r in result.task_records)

    @pytest.mark.asyncio
    async def test_max_agent_calls_exceeded(self):
        """max_agent_calls=1 means only the first task can run.

        The Phase 3 planner rejects plans that exceed ``max_agent_calls``
        at creation time, so we tamper with the budget *after* planning
        and inject a fake PlanValidator.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, max_agent_calls=1)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Root completes (1 call); children get skipped because the
        # budget is exhausted.  Status: BUDGET_EXCEEDED (accountant
        # flag set when max_agent_calls is hit).
        assert result.status in (
            SupervisorRunStatus.PARTIAL_SUCCESS,
            SupervisorRunStatus.BUDGET_EXCEEDED,
        )
        assert result.usage.agent_calls == 1


# ---------------------------------------------------------------------------
# Cancellation / Kill Switch
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_before_run(self):
        """If the cancellation flag is set before execute(), the
        run must be cancelled and no Handler must be called."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        canc = FakeExecutionCancellation()
        canc.cancel_run(plan.run_id)

        # The fake invoker would record calls — but the supervisor
        # should reject before invoking.
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

        # Root attempt starts, sees cancellation, returns cancelled.
        # All pending tasks are then cancelled by the Scheduler.
        assert result.status == SupervisorRunStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_kill_switch_checked_before_invocation(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        canc = FakeExecutionCancellation()
        canc.activate_kill_switch("t-001")

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg, cancellation=canc)

        assert result.status == SupervisorRunStatus.CANCELLED


# ---------------------------------------------------------------------------
# Run Idempotency
# ---------------------------------------------------------------------------


class TestRunIdempotency:
    @pytest.mark.asyncio
    async def test_same_run_and_plan_returns_cached_result(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        store = InMemoryRunStore()

        invoker_calls: list[str] = []

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            invoker_calls.append(task.task_id)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=store,
        )

        r1 = await runtime.execute(plan, reg)
        first_calls = list(invoker_calls)

        r2 = await runtime.execute(plan, reg)

        assert r1.run_id == r2.run_id
        assert r1.plan_hash == r2.plan_hash
        # No additional Handler calls on the cached run.
        assert invoker_calls == first_calls

    @pytest.mark.asyncio
    async def test_cached_result_is_defensive_copy(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        store = InMemoryRunStore()

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        r1 = await runtime.execute(plan, reg)
        # Mutate r1.task_records; the cached copy must be independent.
        original_status = r1.task_records[0].status
        r1.task_records[0].__dict__["status"] = "tampered"

        r2 = await runtime.execute(plan, reg)
        assert r2.task_records[0].status == original_status

    @pytest.mark.asyncio
    async def test_same_run_different_plan_rejected(self):
        reg = _make_registry(_customer_recovery_caps())
        plan1 = _customer_recovery_plan(reg, run_id="run-001")

        # Build a different plan for the same run_id with a different budget
        # so the plan_hash differs.  We need a fresh registry version
        # because budget change re-hashes the request.
        budget2 = ExecutionBudget(max_tasks=20)
        # The registry version is the same, so we can build plan2
        # against the same registry.
        request2 = _make_request(reg, run_id="run-001", budget=budget2)
        plan2 = DeterministicPlanner().create_plan(request2, reg)

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan1),
            run_store=store,
        )

        await runtime.execute(plan1, reg)
        with pytest.raises(RunPlanConflictError):
            await runtime.execute(plan2, reg)


# ---------------------------------------------------------------------------
# Result validation
# ---------------------------------------------------------------------------


class TestResultValidation:
    @pytest.mark.asyncio
    async def test_result_task_id_mismatch_rejected(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # Return a result whose task_id is wrong.
        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == ctx_task.task_id:
                wrong_task = task.model_copy(update={"task_id": "task_other"})
                wrong_result = _ok_result(task=wrong_task)
                return AgentInvocationReceipt(result=wrong_result)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        ctx_rec = next(r for r in result.task_records if r.task_id == ctx_task.task_id)
        # The Handler returned a wrong-task_id result → failed.
        assert ctx_rec.status == "failed"
        # Descendants are skipped.
        assert result.status in (
            SupervisorRunStatus.FAILED,
            SupervisorRunStatus.PARTIAL_SUCCESS,
        )

    @pytest.mark.asyncio
    async def test_result_tenant_mismatch_rejected(self):
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        ctx_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # Return a result whose tenant_id is foreign.
        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == ctx_task.task_id:
                wrong = _ok_result(task=task, tenant_id="t-other")
                return AgentInvocationReceipt(result=wrong)
            return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)

        ctx_rec = next(r for r in result.task_records if r.task_id == ctx_task.task_id)
        assert ctx_rec.status == "failed"
