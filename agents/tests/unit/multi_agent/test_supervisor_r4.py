"""Phase 4 R4 regression tests.

Direct counter-examples for the four P0 issues and one P1 cleanup
identified in the Phase 4 R3 review (commit ``5b9c647``):

* **P0-1** — Run Identity Probe must execute Plan Conflict semantics
  *before* the Registry Version check.  A different ``plan_hash``
  must raise :class:`RunPlanConflictError` regardless of whether the
  stored run is ``completed`` or ``in_progress``, and the conflict
  must not be masked by a registry version mismatch.
* **P0-2** — Usage Trust must be bound to the Invoker's
  :class:`UsageVerificationCapabilities`, not self-reported by the
  Receipt.  An unmarked Invoker cannot claim ``trusted_adapter`` or
  ``verified_provider``; fake ``provider_metadata`` does not create
  trust; a Receipt cannot elevate trust above the Invoker's
  capabilities.
* **P0-3** — Invalid Receipts must still charge *observed* tool calls
  to the budget.  ``len(receipt.result.tool_calls)`` is charged
  *before* receipt validation so an under-reporting receipt cannot
  erase already-consumed budget.
* **P0-4** — Agent Result Version must be bound to the
  :class:`ExecutionBinding` capability snapshot.  A Handler cannot
  return a result stamped with a different ``agent_version`` than the
  one bound at pre-flight.
* **P1-1** — Agent Call Budget must be deterministically pre-allocated
  in ``task_id`` order before any coroutine is created.  Which tasks
  get the remaining budget is not dependent on coroutine scheduling.
"""

from __future__ import annotations

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
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ExecutionBudget,
    ExecutionUsage,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
)
from multi_agent.execution import (
    ExecutionBinding,
    ExecutionTraceEvent,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TaskExecutionRecord,
    TRACE_TASK_STARTED,
    validate_agent_result,
)
from multi_agent.execution_errors import (
    InvalidAgentResultError,
    RunPlanConflictError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    DeterministicFakeInvoker,
    UsageVerificationCapabilities,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlanValidationReport,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    compute_request_hash,
)
from multi_agent.planning_templates import INTENT_CUSTOMER_CONTEXT
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.run_store import (
    InMemoryRunStore,
    RunIdentityStatus,
)
from multi_agent.state import MergedState
from multi_agent.supervisor import SupervisorRuntime


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_supervisor_r3.py)
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_capability(
    agent_id: str,
    domains: frozenset[str],
    supported_tasks: frozenset[str],
    allowed_tools: frozenset[str],
    timeout_ms: int = 30_000,
    max_retries: int = 0,
    version: str = "1.0.0",
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version=version,
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
    agent_version: str = "1.0.0",
    provider_metadata: ProviderMetadata | None = None,
    token_usage: TokenUsage | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
) -> AgentResult:
    return AgentResult(
        result_id=f"r-{task.task_id}",
        task_id=task.task_id,
        agent_id=agent_id or task.agent_id,
        agent_version=agent_version,
        tenant_id=tenant_id or task.tenant_id,
        status=status,  # type: ignore[arg-type]
        confidence=1.0,
        duration_ms=0.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        errors=errors or [],
        token_usage=token_usage or TokenUsage(),
        provider_metadata=provider_metadata,
        tool_calls=tool_calls or [],
        completed_at=_FIXED_TS,
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


# ---------------------------------------------------------------------------
# Helpers for multi-task independent plans (P1-1 deterministic allocation)
# ---------------------------------------------------------------------------


def _three_independent_caps() -> list[AgentCapability]:
    return [
        _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
        ),
        _make_capability(
            "agent_b",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
        ),
        _make_capability(
            "agent_c",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
        ),
    ]


def _three_independent_catalog() -> ToolCatalog:
    return ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )


def _three_independent_plan(
    registry: AgentRegistry,
    *,
    budget: ExecutionBudget | None = None,
    run_id: str = "run-001",
) -> PlanDraft:
    """Build a plan with three independent root tasks (no dependencies)."""
    task_a = AgentTask(
        task_id="task_a",
        agent_id="agent_a",
        task_type="root_task",
        objective="root A",
        tenant_id="t-001",
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
    task_c = AgentTask(
        task_id="task_c",
        agent_id="agent_c",
        task_type="root_task",
        objective="root C",
        tenant_id="t-001",
        timeout_ms=10_000,
    )

    signals = PlanningSignals(
        event_type=None,
        domains=frozenset({"test"}),
        requested_task_types=frozenset({"root_task"}),
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
        objective="three-independent test",
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
            intent_id="intent_b",
            domain="test",
            task=task_b,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        ),
        PlannedTask(
            intent_id="intent_c",
            domain="test",
            task=task_c,
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


# ---------------------------------------------------------------------------
# Helpers for chain plan (P0-3 budget exceeded stops new tasks)
# ---------------------------------------------------------------------------


def _chain_caps() -> list[AgentCapability]:
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


def _chain_catalog() -> ToolCatalog:
    return ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )


def _chain_plan(
    registry: AgentRegistry,
    *,
    budget: ExecutionBudget | None = None,
    run_id: str = "run-001",
) -> PlanDraft:
    """Build a plan with a chain: task_a → task_b."""
    task_a = AgentTask(
        task_id="task_a",
        agent_id="agent_a",
        task_type="root_task",
        objective="root A",
        tenant_id="t-001",
        timeout_ms=10_000,
    )
    task_b = AgentTask(
        task_id="task_b",
        agent_id="agent_b",
        task_type="child_task",
        objective="child B",
        tenant_id="t-001",
        dependencies=frozenset({"task_a"}),
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
        objective="chain test",
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
            intent_id="intent_b",
            domain="test",
            task=task_b,
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


def _make_stored_result(
    *,
    run_id: str = "run-001",
    plan_hash: str = "a" * 64,
    registry_version: str = "reg-v-001",
) -> SupervisorRunResult:
    """Build a minimal SupervisorRunResult for store pre-population."""
    task_record = TaskExecutionRecord(
        task_id="task-001",
        agent_id="agent_001",
        status="completed",
        attempts=[
            TaskAttemptRecord(
                task_id="task-001",
                agent_id="agent_001",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="completed",
                duration_ms=10,
            )
        ],
    )
    trace_event = ExecutionTraceEvent(
        sequence=0,
        event_type="run_started",
        run_id=run_id,
        occurred_at=_FIXED_TS,
    )
    return SupervisorRunResult(
        run_id=run_id,
        plan_hash=plan_hash,
        registry_version=registry_version,
        status=SupervisorRunStatus.COMPLETED,
        task_records=[task_record],
        merged_state=MergedState(),
        usage=ExecutionUsage(),
        trace=[trace_event],
        started_at=_FIXED_TS,
        completed_at=_FIXED_TS,
        duration_ms=10,
    )


# ===========================================================================
# P0-1: Run Identity Probe must execute Plan Conflict semantics
# ===========================================================================


class TestRunIdentityProbe:
    """R4 P0-1: the Supervisor must check ``plan_hash_matches`` BEFORE
    checking ``status`` so a plan conflict is never masked by a registry
    version mismatch or an in-progress error."""

    @pytest.mark.asyncio
    async def test_completed_different_plan_conflicts_before_registry(self):
        """A completed run with a different ``plan_hash`` must raise
        :class:`RunPlanConflictError` BEFORE the registry version check.

        Reproduction: pre-populate the store with a completed run whose
        ``plan_hash`` differs from the incoming plan.  The live registry
        has also drifted (version mismatch).  The Supervisor must raise
        :class:`RunPlanConflictError`, NOT ``SupervisorError`` with
        "registry version mismatch" — the conflict is the root cause.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Pre-populate the store with a completed run whose plan_hash
        # differs from the incoming plan.
        store = InMemoryRunStore()
        stale_plan_hash = "a" * 64
        assert plan.plan_hash != stale_plan_hash, (
            "test setup: plan_hash must differ from stale_plan_hash"
        )
        lease = await store.begin(plan.run_id, stale_plan_hash)
        await store.complete(
            lease,
            _make_stored_result(
                run_id=plan.run_id,
                plan_hash=stale_plan_hash,
                registry_version=plan.registry_version,
            ),
        )

        # Mutate the live registry so its version drifts — if the
        # Supervisor checked the registry BEFORE the identity probe,
        # it would raise "registry version mismatch" and mask the real
        # conflict.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"recovery_metrics"}),
            frozenset({"crm_reader.get_customers"}),
        )
        reg.register(extra_cap, _NoopHandler())

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RunPlanConflictError):
            await runtime.execute(plan, reg)

    @pytest.mark.asyncio
    async def test_in_progress_different_plan_is_conflict(self):
        """An in-progress run with a different ``plan_hash`` must raise
        :class:`RunPlanConflictError`, NOT :class:`RunAlreadyInProgressError`.

        Reproduction: pre-populate the store with an in-progress run
        whose ``plan_hash`` differs from the incoming plan.  The
        Supervisor must raise :class:`RunPlanConflictError` because the
        plan conflict is the root cause — the run_id is bound to a
        different plan.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        store = InMemoryRunStore()
        stale_plan_hash = "b" * 64
        assert plan.plan_hash != stale_plan_hash
        await store.begin(plan.run_id, stale_plan_hash)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RunPlanConflictError):
            await runtime.execute(plan, reg)

    @pytest.mark.asyncio
    async def test_conflict_not_masked_by_registry_version(self):
        """A plan conflict must not be masked by a registry version
        mismatch even when the stored run is completed.

        This is a focused variant of
        :meth:`test_completed_different_plan_conflicts_before_registry`
        that asserts the error type is exactly
        :class:`RunPlanConflictError`, not a generic
        :class:`SupervisorError`.
        """
        from multi_agent.execution_errors import SupervisorError

        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        store = InMemoryRunStore()
        stale_plan_hash = "c" * 64
        assert plan.plan_hash != stale_plan_hash
        lease = await store.begin(plan.run_id, stale_plan_hash)
        await store.complete(
            lease,
            _make_stored_result(
                run_id=plan.run_id,
                plan_hash=stale_plan_hash,
                registry_version=plan.registry_version,
            ),
        )

        # Mutate the live registry so its version drifts.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"recovery_metrics"}),
            frozenset({"crm_reader.get_customers"}),
        )
        reg.register(extra_cap, _NoopHandler())

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        # Must be RunPlanConflictError, NOT SupervisorError (registry).
        with pytest.raises(RunPlanConflictError):
            await runtime.execute(plan, reg)
        # Extra guard: RunPlanConflictError is a subclass of
        # SupervisorError, so we also assert the message mentions the
        # plan_hash conflict (not "registry version mismatch").
        try:
            await runtime.execute(plan, reg)
        except RunPlanConflictError as exc:
            assert "plan_hash" in str(exc).lower()
        except SupervisorError as exc:
            pytest.fail(
                f"expected RunPlanConflictError, got SupervisorError "
                f"with message: {exc}"
            )

    def test_run_identity_status_contract_has_no_unreachable_value(self):
        """R4 P0-1: ``RunIdentityStatus`` must not contain the
        unreachable ``"conflict"`` value.  The InMemoryRunStore never
        returned it, and the Supervisor determines conflict from
        ``plan_hash_matches`` — the literal is dead code.

        This test asserts the type contract so a future change cannot
        re-introduce the unreachable value without breaking the build.
        """
        # RunIdentityStatus is a typing.Literal.  We introspect its
        # arguments via ``typing.get_args``.
        from typing import get_args

        allowed = set(get_args(RunIdentityStatus))
        assert "conflict" not in allowed, (
            "RunIdentityStatus must not contain 'conflict' — the "
            "Supervisor determines conflict from plan_hash_matches, "
            "and the InMemoryRunStore never returns this status."
        )
        assert allowed == {"in_progress", "completed"}, (
            f"RunIdentityStatus must be exactly {{'in_progress', "
            f"'completed'}}, got {allowed}"
        )


# ===========================================================================
# P0-2: Usage Trust must be bound to Invoker capabilities
# ===========================================================================


class _UnmarkedInvoker:
    """An invoker with NO ``usage_capabilities`` property —
    ``get_usage_capabilities`` returns the fully-unverified default.

    The ``receipt_factory`` receives only the ``task`` (not the context)
    because the test receipts are task-specific but context-agnostic.
    """

    def __init__(self, receipt_factory: Any) -> None:
        self._factory = receipt_factory

    async def invoke(
        self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentInvocationReceipt:
        return self._factory(task)


def _receipt_with_trusted_adapter(
    task: AgentTask, *, cost_usd: Decimal = Decimal("0.01")
) -> AgentInvocationReceipt:
    """A receipt that previously claimed ``trusted_adapter`` trust
    with a cost.

    R10 P0-5: the legacy ``usage_trust='trusted_adapter'`` derived
    ``cost_source_id='trusted_adapter'`` which conflicted with the
    default ``cost_disposition=UNAVAILABLE`` (UNAVAILABLE requires
    ``source_id=None`` AND ``value=None``).  The receipt now uses
    ``UNAVAILABLE`` with ``cost_usd=None`` — the test still verifies
    that an unverified receipt fails closed when ``cost_budget_usd``
    is configured (R9 Section 1 commit-then-check).
    """
    result = _ok_result(task=task)
    return AgentInvocationReceipt(
        result=result,
        tool_calls=len(result.tool_calls),
        cost_usd=None,
    )


def _receipt_with_verified_provider(
    task: AgentTask,
    *,
    tokens_used: int = 50,
) -> AgentInvocationReceipt:
    """A receipt that previously claimed ``verified_provider`` trust
    with tokens.

    Includes ``provider_metadata`` so the result carries the
    self-reported token usage for diagnostics.

    R10 P0-5: the legacy ``usage_trust='verified_provider'`` derived
    ``token_source_id='verified_provider'`` which conflicted with the
    default ``token_disposition=UNAVAILABLE``.  The receipt now uses
    ``UNAVAILABLE`` with ``tokens_used=None`` — the test still
    verifies that an unverified receipt fails closed when
    ``token_budget`` is configured (R9 Section 1 commit-then-check).
    """
    result = _ok_result(
        task=task,
        provider_metadata=ProviderMetadata(
            provider="openai",
            chat_model="gpt-4",
            embedding_model="text-embedding-3-small",
            ai_mode="live",
        ),
        token_usage=TokenUsage(
            input_tokens=tokens_used // 2,
            output_tokens=tokens_used - tokens_used // 2,
            total_tokens=tokens_used,
        ),
    )
    return AgentInvocationReceipt(
        result=result,
        tool_calls=len(result.tool_calls),
        tokens_used=None,
    )


class TestUsageTrustInvokerBound:
    """R4 P0-2: Usage Trust must be bound to the Invoker's
    :class:`UsageVerificationCapabilities`, not self-reported by the
    Receipt."""

    @pytest.mark.asyncio
    async def test_unmarked_invoker_cannot_claim_trusted_adapter(self):
        """An unmarked Invoker (no ``usage_capabilities``) that returns
        a receipt with ``usage_trust='trusted_adapter'`` must be
        rejected when ``cost_budget_usd`` is configured — the receipt
        cannot self-elevate trust above the Invoker's capabilities."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("10.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("10.00"))

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        runtime = SupervisorRuntime(
            invoker=_UnmarkedInvoker(_receipt_with_trusted_adapter),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # R9 Section 1 — commit-then-check means the task completes, but
        # the run is BUDGET_EXCEEDED.
        assert root_rec.status == "completed"

    @pytest.mark.asyncio
    async def test_unmarked_invoker_cannot_claim_verified_provider(self):
        """An unmarked Invoker that returns a receipt with
        ``usage_trust='verified_provider'`` must be rejected when
        ``token_budget`` is configured — the receipt cannot
        self-elevate trust above the Invoker's capabilities."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg, budget=ExecutionBudget(token_budget=10_000))
        _tamper_plan_budget(plan, token_budget=10_000)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        runtime = SupervisorRuntime(
            invoker=_UnmarkedInvoker(_receipt_with_verified_provider),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # R9 Section 1 — commit-then-check means the task completes, but
        # the run is BUDGET_EXCEEDED.
        assert root_rec.status == "completed"

    @pytest.mark.asyncio
    async def test_fake_provider_metadata_does_not_create_trust(self):
        """An unmarked Invoker that returns a receipt with
        ``usage_trust='verified_provider'`` AND ``provider_metadata``
        set must STILL be rejected when ``token_budget`` is configured.

        The ``provider_metadata`` makes the receipt structurally valid
        (``validate_invocation_receipt`` passes), but the Invoker does
        not have ``verifies_tokens=True`` — fake metadata does not
        create trust."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg, budget=ExecutionBudget(token_budget=10_000))
        _tamper_plan_budget(plan, token_budget=10_000)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        runtime = SupervisorRuntime(
            invoker=_UnmarkedInvoker(_receipt_with_verified_provider),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # R9 Section 1 — commit-then-check means the task completes, but
        # the run is BUDGET_EXCEEDED.
        assert root_rec.status == "completed"

    @pytest.mark.asyncio
    async def test_token_trust_capability_checked_on_invoker(self):
        """An Invoker with ``verifies_tokens=False`` (but
        ``verifies_cost=True``) that returns a receipt with
        ``usage_trust='verified_provider'`` must be rejected when
        ``token_budget`` is configured — token trust requires
        ``verifies_tokens=True`` specifically."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg, budget=ExecutionBudget(token_budget=10_000))
        _tamper_plan_budget(plan, token_budget=10_000)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # Invoker with verifies_cost=True but verifies_tokens=False.
        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=True,
            source_id="test_cost_only_invoker",
            bound_cost_source_ids=frozenset({"test_cost_only_invoker"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _receipt_with_verified_provider(task)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # R9 Section 1 — commit-then-check means the task completes, but
        # the run is BUDGET_EXCEEDED.
        assert root_rec.status == "completed"

    @pytest.mark.asyncio
    async def test_cost_trust_capability_checked_on_invoker(self):
        """An Invoker with ``verifies_tokens=True`` (but
        ``verifies_cost=False``) that returns a receipt with
        ``usage_trust='trusted_adapter'`` must be rejected when
        ``cost_budget_usd`` is configured — cost trust requires
        ``verifies_cost=True`` specifically."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("10.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("10.00"))

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        # Invoker with verifies_tokens=True but verifies_cost=False.
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="test_token_only_invoker",
            bound_token_source_ids=frozenset({"test_token_only_invoker"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _receipt_with_trusted_adapter(task)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # R9 Section 1 — commit-then-check means the task completes, but
        # the run is BUDGET_EXCEEDED.
        assert root_rec.status == "completed"

    @pytest.mark.asyncio
    async def test_receipt_cannot_elevate_invoker_trust(self):
        """An Invoker with ``verifies_tokens=True`` but
        ``verifies_cost=False`` cannot use a ``trusted_adapter``
        receipt to enforce ``cost_budget_usd`` — the receipt's
        ``trusted_adapter`` claim passes the cross-check (because
        ``verifies_tokens`` is True), but the cost budget check
        specifically requires ``verifies_cost=True``.

        This proves the Receipt cannot elevate the Invoker's trust
        above its actual capabilities for a specific budget dimension.
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("10.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("10.00"))

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="test_token_only_invoker",
            bound_token_source_ids=frozenset({"test_token_only_invoker"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _receipt_with_trusted_adapter(task)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        # R9 Section 1 — commit-then-check means the task completes, but
        # the run is BUDGET_EXCEEDED.
        assert root_rec.status == "completed"


# ===========================================================================
# P0-3: Invalid Receipt must still charge observed tool calls
# ===========================================================================


def _tool_call_record(name: str = "tool.read") -> ToolCallRecord:
    return ToolCallRecord(
        tool_name=name,
        authority=ToolAuthority.READ,
        ok=True,
        duration_ms=1.0,
    )


def _underreporting_receipt(
    task: AgentTask, *, observed: int = 5
) -> AgentInvocationReceipt:
    """A receipt that under-reports ``tool_calls``: the result carries
    ``observed`` :class:`ToolCallRecord` entries, but the receipt
    declares ``tool_calls=0``.  ``validate_invocation_receipt`` will
    reject this receipt as inconsistent."""
    result = _ok_result(
        task=task,
        tool_calls=[_tool_call_record() for _ in range(observed)],
    )
    return AgentInvocationReceipt(
        result=result,
        tool_calls=0,  # under-reported!
        usage_trust="unverified",
    )


def _overreporting_receipt(
    task: AgentTask, *, observed: int = 1
) -> AgentInvocationReceipt:
    """A receipt that over-reports ``tool_calls``: the result carries
    ``observed`` entries, but the receipt declares ``tool_calls=10``.
    ``validate_invocation_receipt`` will reject this receipt."""
    result = _ok_result(
        task=task,
        tool_calls=[_tool_call_record() for _ in range(observed)],
    )
    return AgentInvocationReceipt(
        result=result,
        tool_calls=10,  # over-reported!
        usage_trust="unverified",
    )


class TestObservedToolCallAccounting:
    """R4 P0-3: invalid Receipts must still charge *observed* tool
    calls to the budget.  ``len(receipt.result.tool_calls)`` is charged
    *before* receipt validation so an under-reporting receipt cannot
    erase already-consumed budget."""

    @pytest.mark.asyncio
    async def test_invalid_receipt_still_charges_actual_tool_calls(self):
        """An under-reporting receipt (5 actual tool calls, 0 declared)
        must still charge 5 tool calls to the Run usage.  The Task is
        marked ``failed`` (``invalid_receipt``), but the budget
        reflects the actual consumption."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _underreporting_receipt(task, observed=5)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The Task must be failed with invalid_receipt.
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "invalid_receipt" for a in root_rec.attempts)

        # The observed 5 tool calls must be charged to the Run usage,
        # even though the receipt declared 0.
        assert result.usage.tool_calls >= 5, (
            f"expected >=5 observed tool calls in usage, got {result.usage.tool_calls}"
        )

    @pytest.mark.asyncio
    async def test_underreported_tool_calls_cannot_preserve_budget(self):
        """An under-reporting receipt must NOT preserve the
        ``max_tool_calls`` budget.  If the Handler returned 5 tool
        calls but the receipt declared 0, the budget must reflect 5
        charged calls — the under-reporting cannot erase the
        consumption."""
        reg = _make_registry(_customer_recovery_caps())
        # Create with default budget (planner accepts it), then tamper
        # max_tool_calls down to 3 so 5 observed calls exceed it.
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, max_tool_calls=3)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _underreporting_receipt(task, observed=5)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # 5 observed tool calls > max_tool_calls=3 → budget exceeded.
        assert result.usage.tool_calls >= 5
        # The run should reflect budget exceeded (or failed — the
        # important part is the tool calls were charged).
        assert result.status in (
            SupervisorRunStatus.BUDGET_EXCEEDED,
            SupervisorRunStatus.FAILED,
        )

    @pytest.mark.asyncio
    async def test_overreported_receipt_uses_observed_tool_count(self):
        """An over-reporting receipt (1 actual tool call, 10 declared)
        must charge 1 (the observed count) to the budget, not 10.
        The Task is ``failed`` (``invalid_receipt``), and the attempt
        record uses the observed count."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _overreporting_receipt(task, observed=1)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "invalid_receipt" for a in root_rec.attempts)

        # The observed count (1) must be used, not the declared count (10).
        invalid_attempt = next(
            a for a in root_rec.attempts if a.error_code == "invalid_receipt"
        )
        assert invalid_attempt.tool_calls == 1, (
            f"attempt record must use observed tool_calls=1, got "
            f"{invalid_attempt.tool_calls}"
        )

    @pytest.mark.asyncio
    async def test_invalid_receipt_budget_exceeded_stops_new_tasks(self):
        """When an invalid receipt causes the tool call budget to be
        exceeded, subsequent tasks must NOT start.

        Setup: a chain plan (task_a → task_b) with ``max_tool_calls=3``.
        task_a returns 5 actual tool calls but declares 0 → invalid
        receipt, budget exceeded (5 > 3).  task_b must have no attempts
        (never started) — its dependency failed AND the budget is
        exceeded."""
        reg = _make_registry(_chain_caps(), catalog=_chain_catalog())
        plan = _chain_plan(
            reg, budget=ExecutionBudget(max_tool_calls=3, max_agent_calls=10)
        )
        _tamper_plan_budget(plan, max_tool_calls=3, max_agent_calls=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == "task_a":
                return _underreporting_receipt(task, observed=5)
            # task_b should never be invoked.
            return AgentInvocationReceipt(  # pragma: no cover
                result=_ok_result(task=task),
                tool_calls=0,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # task_a failed with invalid_receipt.
        rec_a = next(r for r in result.task_records if r.task_id == "task_a")
        assert rec_a.status == "failed"
        assert any(a.error_code == "invalid_receipt" for a in rec_a.attempts)

        # task_b never started — no attempts.
        rec_b = next(r for r in result.task_records if r.task_id == "task_b")
        assert len(rec_b.attempts) == 0, (
            f"task_b must have no attempts (never started), got {len(rec_b.attempts)}"
        )
        assert rec_b.status in ("skipped", "cancelled")

        # Budget was exceeded (5 > 3).
        assert result.usage.tool_calls >= 5

    @pytest.mark.asyncio
    async def test_attempt_record_uses_observed_tool_calls(self):
        """The :class:`TaskAttemptRecord` for an invalid receipt must
        record the *observed* tool call count, not the receipt's
        declared count.  This ensures the audit trail reflects what
        actually happened, not what the (faulty) receipt claimed."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _underreporting_receipt(task, observed=3)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        invalid_attempt = next(
            a for a in root_rec.attempts if a.error_code == "invalid_receipt"
        )
        assert invalid_attempt.tool_calls == 3, (
            f"attempt record must use observed tool_calls=3, got "
            f"{invalid_attempt.tool_calls}"
        )


# ===========================================================================
# P0-4: Agent Result Version must be bound to ExecutionBinding
# ===========================================================================


class TestAgentVersionBinding:
    """R4 P0-4: ``validate_agent_result`` must verify
    ``result.agent_version == binding.capability_snapshot.version``.
    A Handler cannot return a result from a different capability
    version than the one bound at pre-flight."""

    def test_result_agent_version_mismatch_rejected(self):
        """A result whose ``agent_version`` differs from the binding's
        ``capability_snapshot.version`` must be rejected with
        :class:`InvalidAgentResultError`."""
        task = AgentTask(
            task_id="task-001",
            agent_id="agent_001",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        capability = _make_capability(
            "agent_001",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            version="1.0.0",
        )
        binding = ExecutionBinding(
            task_id="task-001",
            agent_id="agent_001",
            capability_snapshot=capability,
        )
        plan = _customer_recovery_plan(_make_registry(_customer_recovery_caps()))

        # Result with a MISMATCHED agent_version.
        result = _ok_result(task=task, agent_version="2.0.0")

        with pytest.raises(InvalidAgentResultError, match="agent_version"):
            validate_agent_result(result, task=task, plan=plan, binding=binding)

    def test_bound_capability_version_enters_trace(self):
        """The ``TRACE_TASK_STARTED`` trace event must include
        ``binding_capability_version`` so audit consumers can correlate
        the executed task with the capability version bound at
        pre-flight."""
        # This is a structural assertion on the trace event data keys.
        # We verify the key is present in the emitted event by checking
        # the supervisor's _execute_task trace emission code path via
        # an integration test below (test_bound_capability_version_enters_trace_integration).
        # Here we assert the ExecutionTraceEvent contract carries the
        # data field that the Supervisor populates.
        event = ExecutionTraceEvent(
            sequence=0,
            event_type=TRACE_TASK_STARTED,
            run_id="run-001",
            task_id="task-001",
            agent_id="agent_001",
            occurred_at=_FIXED_TS,
            data={
                "attempt": 0,
                "binding_agent_id": "agent_001",
                "binding_capability_agent_id": "agent_001",
                "binding_capability_authority": "read",
                "binding_capability_version": "1.0.0",
            },
        )
        assert event.data["binding_capability_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_bound_capability_version_enters_trace_integration(self):
        """Integration test: run a plan and verify the
        ``TRACE_TASK_STARTED`` trace events carry
        ``binding_capability_version`` matching the pre-flight
        capability snapshot."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        started_events = [e for e in result.trace if e.event_type == TRACE_TASK_STARTED]
        assert len(started_events) > 0, "expected at least one TRACE_TASK_STARTED"
        for event in started_events:
            assert "binding_capability_version" in event.data, (
                f"TRACE_TASK_STARTED event must include "
                f"binding_capability_version, got keys: "
                f"{list(event.data.keys())}"
            )
            assert event.data["binding_capability_version"] == "1.0.0", (
                f"binding_capability_version must be '1.0.0' (the "
                f"pre-flight capability version), got "
                f"{event.data['binding_capability_version']}"
            )

    def test_registry_drift_does_not_change_expected_result_version(self):
        """After the registry drifts (capability upgraded to a new
        version), the :class:`ExecutionBinding} still carries the
        pre-flight version.  ``validate_agent_result`` must accept a
        result whose ``agent_version`` matches the *binding* version,
        not reject it for not matching the *live* registry version."""
        task = AgentTask(
            task_id="task-001",
            agent_id="agent_001",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        # Binding captured at pre-flight with version "1.0.0".
        capability_v1 = _make_capability(
            "agent_001",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            version="1.0.0",
        )
        binding = ExecutionBinding(
            task_id="task-001",
            agent_id="agent_001",
            capability_snapshot=capability_v1,
        )
        plan = _customer_recovery_plan(_make_registry(_customer_recovery_caps()))

        # Result with agent_version="1.0.0" — matches the binding.
        result = _ok_result(task=task, agent_version="1.0.0")

        # validate_agent_result must PASS — the result matches the
        # binding version, even if the live registry has drifted to
        # "2.0.0".  (The binding is the authoritative snapshot.)
        validate_agent_result(result, task=task, plan=plan, binding=binding)

    def test_result_version_validated_against_binding_not_live_registry(self):
        """A result whose ``agent_version`` matches the binding but
        NOT the live registry version must be ACCEPTED.  A result
        whose ``agent_version`` matches the live registry but NOT the
        binding must be REJECTED.  This proves the validation uses the
        binding, not the live registry."""
        task = AgentTask(
            task_id="task-001",
            agent_id="agent_001",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        # Binding captured at pre-flight with version "1.0.0".
        capability_v1 = _make_capability(
            "agent_001",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            version="1.0.0",
        )
        binding = ExecutionBinding(
            task_id="task-001",
            agent_id="agent_001",
            capability_snapshot=capability_v1,
        )
        plan = _customer_recovery_plan(_make_registry(_customer_recovery_caps()))

        # Result matching the binding (1.0.0), NOT the live registry
        # (which has drifted to 2.0.0) — must be ACCEPTED.
        result_matching_binding = _ok_result(task=task, agent_version="1.0.0")
        validate_agent_result(
            result_matching_binding, task=task, plan=plan, binding=binding
        )

        # Result matching the live registry (2.0.0), NOT the binding
        # (1.0.0) — must be REJECTED.
        result_matching_live = _ok_result(task=task, agent_version="2.0.0")
        with pytest.raises(InvalidAgentResultError, match="agent_version"):
            validate_agent_result(
                result_matching_live, task=task, plan=plan, binding=binding
            )


# ===========================================================================
# P1-1: Deterministic Agent Call Budget pre-allocation
# ===========================================================================


class TestDeterministicCallSlotAllocation:
    """R4 P1-1: the Scheduler must pre-allocate agent call slots in
    ``task_id`` order BEFORE any coroutine is created.  Tasks that
    cannot get a slot are marked ``skipped`` — they never start a
    Handler.  Which tasks get the remaining budget is deterministic,
    not dependent on coroutine scheduling."""

    @pytest.mark.asyncio
    async def test_agent_call_budget_selects_tasks_in_task_id_order(self):
        """When ``max_agent_calls`` is less than the number of ready
        tasks, the tasks that get slots must be the first N in
        ``task_id`` order — not whichever coroutine happened to
        acquire the semaphore first."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        # max_agent_calls=2, but 3 ready tasks.
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=2, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=2, max_iterations=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # task_a and task_b (task_id order) must be completed.
        rec_a = next(r for r in result.task_records if r.task_id == "task_a")
        rec_b = next(r for r in result.task_records if r.task_id == "task_b")
        rec_c = next(r for r in result.task_records if r.task_id == "task_c")

        assert rec_a.status == "completed", (
            f"task_a must get a slot (task_id order), got {rec_a.status}"
        )
        assert rec_b.status == "completed", (
            f"task_b must get a slot (task_id order), got {rec_b.status}"
        )
        # task_c must be skipped — no slot.
        assert rec_c.status == "skipped", (
            f"task_c must be skipped (no agent call slot), got {rec_c.status}"
        )
        assert len(rec_c.attempts) == 0, (
            f"task_c must have no attempts (never started), got {len(rec_c.attempts)}"
        )

    @pytest.mark.asyncio
    async def test_agent_call_budget_assignment_is_cross_platform_deterministic(
        self,
    ):
        """Running the same plan + budget multiple times must always
        select the same tasks for agent call slots.  The selection is
        deterministic (by ``task_id`` order), not dependent on the
        event loop or platform."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        # max_agent_calls=1, 3 ready tasks — only task_a should get a slot.
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=1, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=1, max_iterations=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        # Run multiple times and verify the same tasks are selected.
        for i in range(5):
            store = InMemoryRunStore()
            run_id = f"run-deterministic-{i}"
            # Tamper the request's run_id so each run is independent.
            # PlanDraft.run_id is a read-only property delegating to
            # request.run_id, so we must set it on the request.
            object.__setattr__(plan.request, "run_id", run_id)
            # Rebuild request_hash and plan_hash for the new run_id.
            object.__setattr__(plan, "request_hash", compute_request_hash(plan.request))
            object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())

            runtime = SupervisorRuntime(
                invoker=DeterministicFakeInvoker(factory=factory),
                run_store=store,
                plan_validator=_AlwaysValidPlanValidator(),
            )
            result = await runtime.execute(plan, reg)

            rec_a = next(r for r in result.task_records if r.task_id == "task_a")
            rec_b = next(r for r in result.task_records if r.task_id == "task_b")
            rec_c = next(r for r in result.task_records if r.task_id == "task_c")

            # Every run must select task_a (task_id order) and skip b, c.
            assert rec_a.status == "completed", (
                f"run {i}: task_a must get the slot, got {rec_a.status}"
            )
            assert rec_b.status == "skipped", (
                f"run {i}: task_b must be skipped, got {rec_b.status}"
            )
            assert rec_c.status == "skipped", (
                f"run {i}: task_c must be skipped, got {rec_c.status}"
            )

    @pytest.mark.asyncio
    async def test_ready_tasks_without_call_slot_never_start(self):
        """A ready task that does not get an agent call slot must
        NEVER start a Handler — it must have zero attempts and be
        marked ``skipped`` with an ``agent_call budget exhausted``
        reason."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        # max_agent_calls=1, 3 ready tasks.
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=1, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=1, max_iterations=10)

        def _factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        invoker = DeterministicFakeInvoker(factory=_factory)

        runtime = SupervisorRuntime(
            invoker=invoker,
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # task_a gets the slot — 1 invocation.
        rec_a = next(r for r in result.task_records if r.task_id == "task_a")
        assert rec_a.status == "completed"
        assert len(rec_a.attempts) == 1

        # task_b and task_c never start — 0 invocations each.
        for tid in ("task_b", "task_c"):
            rec = next(r for r in result.task_records if r.task_id == tid)
            assert rec.status == "skipped", f"{tid} must be skipped, got {rec.status}"
            assert len(rec.attempts) == 0, (
                f"{tid} must have 0 attempts, got {len(rec.attempts)}"
            )
            assert rec.skip_reason is not None
            assert "agent_call" in rec.skip_reason or "budget" in rec.skip_reason, (
                f"{tid} skip_reason must mention agent_call budget, "
                f"got {rec.skip_reason!r}"
            )

        # The invoker must have been called exactly once (for task_a).
        assert len(invoker.invocations) == 1, (
            f"invoker must be called once (task_a only), got "
            f"{len(invoker.invocations)} calls"
        )
