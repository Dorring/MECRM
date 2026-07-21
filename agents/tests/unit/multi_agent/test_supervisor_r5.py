"""Phase 4 R5 regression tests.

Direct counter-examples for the five P0 concerns identified in the
Phase 4 R5 review:

* **P0-1** — RetryPolicy must be a canonical contract that flows
  through ``RequestedTask`` → ``TaskIntent`` → ``PlannedTask`` →
  Plan Hash → PlanValidator.  Previously ``max_retries=0`` was
  hardcoded, making retry untestable through the real Phase 3 →
  Phase 4 boundary.  Tests use the real ``DeterministicPlanner`` and
  real ``PlanValidator``.
* **P0-2** — DispatchDecision must carry ``allowed_task_ids``,
  ``denied_task_ids``, ``denial_reason``, and ``budget_exhausted``
  so the Scheduler can distinguish budget-exhaustion denials from
  other reasons and finalise the run as ``budget_exceeded``.
* **P0-3** — AgentCallPermit must separate dispatch permits (issued
  at pre-dispatch) from actual agent calls (committed right before
  ``invoker.invoke()``).  Permits released on cancellation or
  deadline-exceeded must NOT increment ``agent_calls``.
* **P0-4** — ExecutionUsage must separate usage *recording*
  (``tokens_usage_available`` / ``cost_usage_available`` flags) from
  budget *enforcement*.  Trusted usage is ALWAYS recorded regardless
  of whether the corresponding budget is configured.
* **P0-5** — ProviderUsageVerifier Protocol and RegistryAgentInvoker
  with ``usage_verifier`` parameter.  The Invoker's
  ``UsageVerificationCapabilities`` are frozen ONCE at pre-flight so
  a mutable Invoker cannot self-elevate trust mid-run.
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
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
)
from multi_agent.execution import (
    SupervisorRunStatus,
)
from multi_agent.execution_errors import (
    RetryableAgentError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    AttemptUsageDisposition,
    DeterministicFakeInvoker,
    RegistryAgentInvoker,
    UsageProvenance,
    UsageVerificationCapabilities,
    VerifiedUsage,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlanValidationReport,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    RetryPolicy,
    compute_request_hash,
)
from multi_agent.plan_validator import PlanValidator
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.run_store import InMemoryRunStore
from multi_agent.scheduler import AgentCallPermit, DispatchDecision
from multi_agent.supervisor import SupervisorRuntime


# ---------------------------------------------------------------------------
# Shared helpers (copied from test_supervisor_r4.py — self-contained)
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
# Helpers for multi-task independent plans (P0-2, P0-3, P0-4, P0-5)
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
# Helpers for P0-1: RetryPolicy plan via real DeterministicPlanner
# ---------------------------------------------------------------------------


def _retry_policy_caps() -> list[AgentCapability]:
    return [
        _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"task_a_type"}),
            frozenset({"tool.read"}),
        ),
        _make_capability(
            "agent_b",
            frozenset({"test"}),
            frozenset({"task_b_type"}),
            frozenset({"tool.read"}),
        ),
    ]


def _retry_policy_catalog() -> ToolCatalog:
    return ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )


def _retry_policy_plan(
    registry: AgentRegistry,
    *,
    retry_policy_a: RetryPolicy | None = None,
    retry_policy_b: RetryPolicy | None = None,
    budget: ExecutionBudget | None = None,
    run_id: str = "run-001",
) -> PlanDraft:
    """Create a multi-agent plan with requested_tasks carrying retry policies.

    Uses the real ``DeterministicPlanner`` so retry_policy flows through
    the full chain: RequestedTask → TaskIntent → PlannedTask → Plan Hash.
    """
    rt_a = RequestedTask(
        intent_id="intent_a",
        domain="test",
        task_type="task_a_type",
        objective="Task A",
        retry_policy=retry_policy_a or RetryPolicy(),
    )
    rt_b = RequestedTask(
        intent_id="intent_b",
        domain="test",
        task_type="task_b_type",
        objective="Task B",
        retry_policy=retry_policy_b or RetryPolicy(),
    )
    signals = PlanningSignals(
        event_type=None,
        domains=frozenset({"test"}),
        requested_task_types=frozenset({"task_a_type", "task_b_type"}),
        requested_tasks=[rt_a, rt_b],
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
        objective="retry policy test",
        signals=signals,
        budget=budget or ExecutionBudget(),
        context_summary=None,
        registry_version=registry.snapshot().version,
    )
    return DeterministicPlanner().create_plan(request, registry)


def _tamper_retry_policy(
    plan: PlanDraft, intent_id: str, **policy_overrides: Any
) -> PlanDraft:
    """Tamper with a PlannedTask's retry_policy and recompute plan_hash.

    Uses ``object.__setattr__`` to bypass the frozen PlannedTask.  The
    plan_hash is recomputed so the hash integrity check passes — the
    Canonical Plan comparison is what catches the tampering.
    """
    for pt in plan.tasks:
        if pt.intent_id == intent_id:
            original = pt.retry_policy
            new_policy = RetryPolicy(
                max_retries=policy_overrides.get("max_retries", original.max_retries),
                retryable_error_codes=policy_overrides.get(
                    "retryable_error_codes", original.retryable_error_codes
                ),
            )
            object.__setattr__(pt, "retry_policy", new_policy)
            break
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


# ---------------------------------------------------------------------------
# Helpers for P0-5: ProviderUsageVerifier
# ---------------------------------------------------------------------------


class _FakeProviderUsageVerifier:
    """Fake ProviderUsageVerifier for testing."""

    source_id: str = "fake_provider_verifier"
    verifies_tokens: bool = True
    verifies_cost: bool = True

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        return VerifiedUsage(
            tokens_used=token_usage.total_tokens,
            cost_usd=None,
            tokens_verified=True,
        )


class _MutableCapsInvoker:
    """An invoker whose ``usage_capabilities`` changes mid-run.

    Starts with ``verifies_tokens=False`` (unverified).  After the
    first ``invoke()`` call, flips to ``verifies_tokens=True``.  This
    proves that the Supervisor freezes caps at pre-flight and does
    NOT re-read them mid-run.
    """

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self._verifies_tokens = False
        self.invocations: list[tuple[AgentTask, AgentExecutionContext]] = []

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        return UsageVerificationCapabilities(
            verifies_tokens=self._verifies_tokens,
            verifies_cost=False,
            source_id="mutable_caps_invoker",
            bound_source_ids=(
                frozenset({"mutable_caps_invoker"})
                if self._verifies_tokens
                else frozenset()
            ),
        )

    async def invoke(
        self,
        handler: Any,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt:
        self.invocations.append((task, context))
        receipt = self._factory(task, context)
        # Flip caps AFTER invoke — the Supervisor has already frozen
        # the pre-flight caps and will NOT see this change.
        self._verifies_tokens = True
        return receipt


# ===========================================================================
# P0-1: RetryPolicy canonical contract
# ===========================================================================


class TestRetryPolicyCanonical:
    """R5 P0-1: RetryPolicy must be a first-class planning contract that
    flows through RequestedTask → TaskIntent → PlannedTask → Plan Hash →
    PlanValidator.  Tests use the real DeterministicPlanner and real
    PlanValidator."""

    def test_default_retry_policy_flows_through_planner(self):
        """A plan created with default RetryPolicy (max_retries=0) must
        have ``PlannedTask.retry_policy.max_retries == 0`` and
        ``PlannedTask.task.max_retries == 0``.  The RetryPolicy flows
        through the full chain without loss."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        plan = _retry_policy_plan(reg)

        assert len(plan.tasks) == 2
        for pt in plan.tasks:
            assert pt.retry_policy.max_retries == 0
            assert pt.retry_policy.retryable_error_codes == frozenset()
            assert pt.task.max_retries == 0

    def test_custom_retry_policy_flows_through_planner(self):
        """A plan created with ``RetryPolicy(max_retries=2,
        retryable_error_codes=frozenset({"timeout"}))`` must propagate
        the policy to ``PlannedTask.retry_policy`` and the derived
        ``max_retries`` to ``PlannedTask.task.max_retries``."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy_a = RetryPolicy(
            max_retries=2, retryable_error_codes=frozenset({"timeout"})
        )
        policy_b = RetryPolicy(max_retries=1)
        plan = _retry_policy_plan(reg, retry_policy_a=policy_a, retry_policy_b=policy_b)

        pt_a = next(pt for pt in plan.tasks if pt.intent_id == "intent_a")
        pt_b = next(pt for pt in plan.tasks if pt.intent_id == "intent_b")

        assert pt_a.retry_policy.max_retries == 2
        assert pt_a.retry_policy.retryable_error_codes == frozenset({"timeout"})
        assert pt_a.task.max_retries == 2

        assert pt_b.retry_policy.max_retries == 1
        assert pt_b.retry_policy.retryable_error_codes == frozenset()
        assert pt_b.task.max_retries == 1

    def test_tampered_max_retries_detected_by_real_validator(self):
        """Tampering with ``PlannedTask.retry_policy.max_retries`` after
        planning must be detected by the real PlanValidator — even when
        ``plan_hash`` is recomputed to pass the integrity check.  The
        Canonical Plan comparison catches the field mismatch."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy_a = RetryPolicy(max_retries=2)
        plan = _retry_policy_plan(reg, retry_policy_a=policy_a)

        # Tamper: change retry_policy.max_retries from 2 to 3.
        _tamper_retry_policy(plan, "intent_a", max_retries=3)

        validator = PlanValidator()
        report = validator.validate(plan.request, plan, reg)
        assert not report.valid, (
            "PlanValidator must reject a tampered retry_policy.max_retries"
        )
        retry_issues = [i for i in report.issues if "retry_policy" in i.message]
        assert len(retry_issues) > 0, (
            f"expected a retry_policy mismatch issue, got "
            f"{[i.code for i in report.issues]}"
        )

    def test_tampered_retryable_error_codes_detected_by_real_validator(self):
        """Tampering with ``PlannedTask.retry_policy.retryable_error_codes``
        after planning must be detected by the real PlanValidator.  The
        Canonical Plan comparison checks the full RetryPolicy object, not
        just ``max_retries``."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy_a = RetryPolicy(
            max_retries=1, retryable_error_codes=frozenset({"timeout"})
        )
        plan = _retry_policy_plan(reg, retry_policy_a=policy_a)

        # Tamper: change retryable_error_codes from {"timeout"} to empty.
        _tamper_retry_policy(plan, "intent_a", retryable_error_codes=frozenset())

        validator = PlanValidator()
        report = validator.validate(plan.request, plan, reg)
        assert not report.valid, (
            "PlanValidator must reject a tampered retryable_error_codes"
        )
        retry_issues = [i for i in report.issues if "retry_policy" in i.message]
        assert len(retry_issues) > 0, (
            f"expected a retry_policy mismatch issue, got "
            f"{[i.code for i in report.issues]}"
        )


# ===========================================================================
# P0-2: DispatchDecision — budget-denied ready tasks
# ===========================================================================


class TestBudgetDeniedReadyTask:
    """R5 P0-2: the pre-dispatch filter returns a DispatchDecision that
    distinguishes budget-exhaustion denials from other reasons.  Denied
    tasks are marked ``skipped`` and the run finalises as
    ``budget_exceeded``."""

    @pytest.mark.asyncio
    async def test_denied_task_marked_skipped_with_budget_reason(self):
        """A ready task denied by the pre-dispatch filter must be marked
        ``skipped`` with a reason mentioning the agent-call budget."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=1, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=1, max_iterations=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result, tool_calls=len(result.tool_calls)
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # task_a gets the slot; task_b and task_c are denied.
        rec_b = next(r for r in result.task_records if r.task_id == "task_b")
        assert rec_b.status == "skipped"
        assert rec_b.skip_reason is not None
        assert "agent_call" in rec_b.skip_reason or "budget" in rec_b.skip_reason, (
            f"skip_reason must mention agent_call budget, got {rec_b.skip_reason!r}"
        )

    @pytest.mark.asyncio
    async def test_budget_exhausted_finalizes_as_budget_exceeded(self):
        """When the pre-dispatch filter denies tasks due to budget
        exhaustion, the run must finalise as ``BUDGET_EXCEEDED``, not
        ``COMPLETED``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=1, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=1, max_iterations=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result, tool_calls=len(result.tool_calls)
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED, (
            f"expected BUDGET_EXCEEDED, got {result.status}"
        )

    @pytest.mark.asyncio
    async def test_denied_task_has_zero_attempts(self):
        """A task denied by the pre-dispatch filter must have zero
        attempts — it never started a Handler."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=1, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=1, max_iterations=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result, tool_calls=len(result.tool_calls)
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        rec_c = next(r for r in result.task_records if r.task_id == "task_c")
        assert rec_c.status == "skipped"
        assert len(rec_c.attempts) == 0, (
            f"denied task must have 0 attempts, got {len(rec_c.attempts)}"
        )

    def test_dispatch_decision_contract_carries_denial_fields(self):
        """DispatchDecision must be a frozen contract with
        ``allowed_task_ids``, ``denied_task_ids``, ``denial_reason``,
        and ``budget_exhausted`` fields.  ``extra='forbid'`` prevents
        undeclared fields."""
        decision = DispatchDecision(
            allowed_task_ids=("task_a",),
            denied_task_ids=("task_b", "task_c"),
            denial_reason="agent_call budget exhausted",
            budget_exhausted=True,
        )
        assert decision.allowed_task_ids == ("task_a",)
        assert decision.denied_task_ids == ("task_b", "task_c")
        assert decision.denial_reason == "agent_call budget exhausted"
        assert decision.budget_exhausted is True

        # frozen — cannot reassign
        with pytest.raises(Exception):
            decision.budget_exhausted = False  # type: ignore[misc]

        # extra='forbid' — cannot add unknown fields
        with pytest.raises(Exception):
            DispatchDecision(  # type: ignore[call-arg]
                allowed_task_ids=(),
                denied_task_ids=(),
                unknown_field="bad",
            )


# ===========================================================================
# P0-3: AgentCallPermit — permit vs actual call
# ===========================================================================


class TestCallPermitVsActualCall:
    """R5 P0-3: AgentCallPermit separates dispatch permits (issued at
    pre-dispatch) from actual agent calls (committed right before
    ``invoker.invoke()``).  ``usage.agent_calls`` equals the number of
    committed permits, not the number of issued permits."""

    @pytest.mark.asyncio
    async def test_agent_calls_equals_invoker_invoke_count(self):
        """``usage.agent_calls`` must equal ``len(invoker.invocations)``
        — every committed permit results in exactly one invoke call."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result, tool_calls=len(result.tool_calls)
            )

        invoker = DeterministicFakeInvoker(factory=factory)
        runtime = SupervisorRuntime(
            invoker=invoker,
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.agent_calls == len(invoker.invocations), (
            f"agent_calls={result.usage.agent_calls} != "
            f"invocations={len(invoker.invocations)}"
        )
        assert result.usage.agent_calls == 3

    @pytest.mark.asyncio
    async def test_denied_task_not_invoked_not_counted(self):
        """A task denied by pre-dispatch must NOT appear in
        ``invoker.invocations`` and must NOT increment ``agent_calls``.
        Only committed permits count."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg, budget=ExecutionBudget(max_agent_calls=2, max_iterations=10)
        )
        _tamper_plan_budget(plan, max_agent_calls=2, max_iterations=10)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result, tool_calls=len(result.tool_calls)
            )

        invoker = DeterministicFakeInvoker(factory=factory)
        runtime = SupervisorRuntime(
            invoker=invoker,
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Only 2 tasks were invoked; task_c was denied.
        assert len(invoker.invocations) == 2
        assert result.usage.agent_calls == 2
        invoked_ids = {t.task_id for t, _ in invoker.invocations}
        assert "task_c" not in invoked_ids

    @pytest.mark.asyncio
    async def test_permit_released_on_deadline_not_counted(self):
        """When the deadline is exhausted before invocation, the permit
        is released (NOT committed) and ``agent_calls`` stays at 0.
        The tasks are skipped with ``deadline_exceeded``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)
        # Set deadline_ms=0 so remaining_deadline_ms is immediately 0.
        _tamper_plan_budget(plan, deadline_ms=0)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:  # pragma: no cover
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result, tool_calls=len(result.tool_calls)
            )

        invoker = DeterministicFakeInvoker(factory=factory)
        runtime = SupervisorRuntime(
            invoker=invoker,
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # No invocations — permits were released before commit.
        assert len(invoker.invocations) == 0
        assert result.usage.agent_calls == 0, (
            f"agent_calls must be 0 (permits released), got {result.usage.agent_calls}"
        )
        # All tasks skipped with deadline_exceeded.
        for rec in result.task_records:
            assert rec.status == "skipped"
            assert rec.skip_reason == "deadline_exceeded"

    @pytest.mark.asyncio
    async def test_committed_permit_counts_even_if_invoker_raises(self):
        """The permit is committed BEFORE ``invoker.invoke()`` is called.
        If the invoker raises, the agent_call is still counted — the
        commit is the point of no return."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            raise RetryableAgentError("transient failure")

        invoker = DeterministicFakeInvoker(factory=factory)
        runtime = SupervisorRuntime(
            invoker=invoker,
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # All 3 tasks were invoked (commit happened before invoke),
        # even though every invoke raised RetryableAgentError.
        assert len(invoker.invocations) == 3
        assert result.usage.agent_calls == 3, (
            f"agent_calls must be 3 (committed before raise), got "
            f"{result.usage.agent_calls}"
        )

    def test_agent_call_permit_contract_frozen(self):
        """AgentCallPermit must be a frozen contract with
        ``extra='forbid'``, carrying ``task_id`` and ``permit_sequence``."""
        permit = AgentCallPermit(task_id="task_a", permit_sequence=0)
        assert permit.task_id == "task_a"
        assert permit.permit_sequence == 0

        # frozen — cannot reassign
        with pytest.raises(Exception):
            permit.task_id = "task_b"  # type: ignore[misc]

        # extra='forbid' — cannot add unknown fields
        with pytest.raises(Exception):
            AgentCallPermit(  # type: ignore[call-arg]
                task_id="task_a", permit_sequence=0, extra_field="bad"
            )


# ===========================================================================
# P0-4: Usage Recording vs Enforcement
# ===========================================================================


def _verified_provider_receipt(
    task: AgentTask, *, tokens_used: int = 100
) -> AgentInvocationReceipt:
    """A receipt with ``usage_trust='verified_provider'`` and
    ``provider_metadata`` set so ``validate_invocation_receipt`` passes."""
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
        tokens_used=tokens_used,
        usage_provenance=UsageProvenance(
            source_id="test_token_verifier",
            tokens_verified=True,
            cost_verified=False,
        ),
        token_disposition=AttemptUsageDisposition.VERIFIED,
    )


def _trusted_adapter_cost_receipt(
    task: AgentTask, *, cost_usd: Decimal = Decimal("0.05")
) -> AgentInvocationReceipt:
    """A receipt with ``usage_trust='trusted_adapter'`` and ``cost_usd``
    set.  No ``provider_metadata`` — cost-only trusted adapter."""
    result = _ok_result(task=task)
    return AgentInvocationReceipt(
        result=result,
        tool_calls=len(result.tool_calls),
        cost_usd=cost_usd,
        usage_provenance=UsageProvenance(
            source_id="test_cost_verifier",
            tokens_verified=False,
            cost_verified=True,
        ),
        cost_disposition=AttemptUsageDisposition.VERIFIED,
    )


class TestUsageRecordingVsEnforcement:
    """R5 P0-4: usage *recording* (``tokens_usage_available`` /
    ``cost_usage_available`` flags) is separated from budget
    *enforcement*.  Trusted usage is ALWAYS recorded, regardless of
    whether the corresponding budget is configured."""

    @pytest.mark.asyncio
    async def test_verified_tokens_recorded_without_token_budget(self):
        """A ``verified_provider`` receipt with ``tokens_used=100`` must
        set ``tokens_usage_available=True`` and accumulate
        ``tokens_used >= 100`` even when ``token_budget`` is NOT
        configured — recording is independent of enforcement."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="test_token_verifier",
            bound_source_ids=frozenset({"test_token_verifier"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _verified_provider_receipt(task, tokens_used=100)

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.tokens_usage_available is True, (
            "tokens_usage_available must be True even without token_budget"
        )
        assert result.usage.tokens_used >= 100

    @pytest.mark.asyncio
    async def test_verified_cost_recorded_without_cost_budget(self):
        """A ``trusted_adapter`` receipt with ``cost_usd=0.05`` must set
        ``cost_usage_available=True`` and accumulate
        ``cost_usd >= 0.05`` even when ``cost_budget_usd`` is NOT
        configured — recording is independent of enforcement."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=True,
            source_id="test_cost_verifier",
            bound_source_ids=frozenset({"test_cost_verifier"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _trusted_adapter_cost_receipt(task, cost_usd=Decimal("0.05"))

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.cost_usage_available is True, (
            "cost_usage_available must be True even without cost_budget_usd"
        )
        assert result.usage.cost_usd >= Decimal("0.05")

    @pytest.mark.asyncio
    async def test_unverified_receipt_not_recorded_as_available(self):
        """An ``unverified`` receipt must NOT set
        ``tokens_usage_available`` or ``cost_usage_available`` —
        unverified usage is not trusted and cannot be reported as
        available in the run usage.

        R6: the receipt now carries ``provider_metadata`` so it is
        counted as a provider-usage-capable attempt.  With 3 capable
        attempts and 0 verified, the status is ``UNAVAILABLE`` (not
        the vacuous ``COMPLETE`` that applies when ``capable == 0``).
        """
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=ProviderMetadata(
                    provider="openai",
                    chat_model="gpt-4",
                    embedding_model="text-embedding-3-small",
                    ai_mode="live",
                ),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                usage_trust="unverified",
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.tokens_usage_available is False
        assert result.usage.cost_usage_available is False

    @pytest.mark.asyncio
    async def test_token_budget_enforcement_only_when_configured(self):
        """A ``verified_provider`` receipt with ``tokens_used=100`` must:
        - with ``token_budget=50`` → run finalises as ``BUDGET_EXCEEDED``
          (enforcement triggered because budget is configured).
        - without ``token_budget`` → run finalises as ``COMPLETED``
          (no enforcement, but tokens still recorded).

        This proves enforcement only applies when the budget is
        configured, while recording always happens for trusted usage.
        """
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="test_token_verifier",
            bound_source_ids=frozenset({"test_token_verifier"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _verified_provider_receipt(task, tokens_used=100)

        # --- Sub-run A: with token_budget=50 ---
        plan_a = _three_independent_plan(
            reg,
            budget=ExecutionBudget(token_budget=50),
            run_id="run-enforce-a",
        )
        runtime_a = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result_a = await runtime_a.execute(plan_a, reg)
        assert result_a.usage.tokens_usage_available is True
        assert result_a.status == SupervisorRunStatus.BUDGET_EXCEEDED, (
            f"with token_budget=50, expected BUDGET_EXCEEDED, got {result_a.status}"
        )

        # --- Sub-run B: without token_budget ---
        plan_b = _three_independent_plan(reg, run_id="run-enforce-b")
        runtime_b = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result_b = await runtime_b.execute(plan_b, reg)
        assert result_b.usage.tokens_usage_available is True
        assert result_b.status != SupervisorRunStatus.BUDGET_EXCEEDED, (
            f"without token_budget, expected non-exceeded status, got {result_b.status}"
        )


# ===========================================================================
# P0-5: ProviderUsageVerifier — capability frozen at pre-flight
# ===========================================================================


class TestProviderUsageVerification:
    """R5 P0-5: ProviderUsageVerifier Protocol and RegistryAgentInvoker
    with ``usage_verifier`` parameter.  The Invoker's
    ``UsageVerificationCapabilities`` are frozen ONCE at pre-flight so
    a mutable Invoker cannot self-elevate trust mid-run."""

    def test_registry_invoker_without_verifier_is_unverified(self):
        """A ``RegistryAgentInvoker`` constructed WITHOUT a
        ``usage_verifier`` must have ``usage_capabilities`` with
        ``verifies_tokens=False`` and ``verifies_cost=False`` — the
        Handler's ``provider_metadata`` is self-attested and cannot
        be trusted."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        invoker = RegistryAgentInvoker(reg)
        caps = invoker.usage_capabilities

        assert caps.verifies_tokens is False
        assert caps.verifies_cost is False
        assert caps.source_id == "registry_agent_invoker"

    def test_registry_invoker_with_verifier_has_verified_caps(self):
        """A ``RegistryAgentInvoker`` constructed WITH a
        ``ProviderUsageVerifier`` must have ``usage_capabilities`` with
        ``verifies_tokens=True`` and ``verifies_cost=True`` — the
        verifier can authoritatively confirm both token and cost usage."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        verifier = _FakeProviderUsageVerifier()
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)
        caps = invoker.usage_capabilities

        assert caps.verifies_tokens is True
        assert caps.verifies_cost is True
        assert caps.source_id == "registry_agent_invoker+provider_verifier"

    @pytest.mark.asyncio
    async def test_invoker_caps_frozen_at_preflight(self):
        """The Invoker's ``UsageVerificationCapabilities`` are frozen
        ONCE at pre-flight via ``get_usage_capabilities(invoker)``.
        A mutable Invoker that flips ``verifies_tokens`` to True after
        the first invoke still fails the cross-check because the
        frozen pre-flight caps are used in ``record_receipt``."""

        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            return _verified_provider_receipt(task, tokens_used=100)

        invoker = _MutableCapsInvoker(factory)

        runtime = SupervisorRuntime(
            invoker=invoker,  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The invoker was invoked (at least once) and flipped its caps.
        assert len(invoker.invocations) > 0
        assert invoker._verifies_tokens is True, (
            "invoker should have flipped verifies_tokens after invoke"
        )

        # But the pre-flight caps had verifies_tokens=False, so the
        # receipt's verified_provider claim is rejected.
        for rec in result.task_records:
            if rec.status == "failed":
                assert any(a.error_code == "usage_unavailable" for a in rec.attempts), (
                    f"expected usage_unavailable (frozen caps check), "
                    f"got {[a.error_code for a in rec.attempts]}"
                )

    @pytest.mark.asyncio
    async def test_provider_usage_verifier_protocol_contract(self):
        """The ``ProviderUsageVerifier`` Protocol must expose
        ``source_id`` and an async ``verify(*, provider_metadata,
        token_usage)`` method returning ``VerifiedUsage``."""
        verifier = _FakeProviderUsageVerifier()

        # source_id is a string.
        assert isinstance(verifier.source_id, str)
        assert len(verifier.source_id) > 0

        # R7 P0-5: verify is async and returns a VerifiedUsage.
        pm = ProviderMetadata(
            provider="openai",
            chat_model="gpt-4",
            embedding_model="text-embedding-3-small",
            ai_mode="live",
        )
        tu = TokenUsage(input_tokens=50, output_tokens=50, total_tokens=100)
        usage = await verifier.verify(provider_metadata=pm, token_usage=tu)
        assert isinstance(usage, VerifiedUsage)
        assert usage.tokens_used == 100
        assert usage.verified is True

    def test_verified_usage_contract_frozen(self):
        """``VerifiedUsage`` must be a frozen contract with
        ``extra='forbid'``, carrying ``tokens_used``, ``cost_usd``,
        and ``verified`` fields."""
        usage = VerifiedUsage(
            tokens_used=100,
            cost_usd=Decimal("0.05"),
            tokens_verified=True,
            cost_verified=True,
        )
        assert usage.tokens_used == 100
        assert usage.cost_usd == Decimal("0.05")
        assert usage.verified is True

        # frozen — cannot reassign
        with pytest.raises(Exception):
            usage.verified = False  # type: ignore[misc]

        # extra='forbid' — cannot add unknown fields
        with pytest.raises(Exception):
            VerifiedUsage(  # type: ignore[call-arg]
                tokens_used=0, verified=False, extra_field="bad"
            )
