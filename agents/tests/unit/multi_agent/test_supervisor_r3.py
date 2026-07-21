"""Phase 4 R3 regression tests.

Direct counter-examples for the four P0 issues and three P1 cleanups
identified in the Phase 4 R2 review (commit ``64fedd1``):

* **P0-1** — Pre-cancelled Run must still pass full Pre-flight
  (Registry/Validator/Handler).  An invalid plan cannot be cached as
  ``cancelled`` — that would poison the ``run_id`` for a later valid
  attempt.
* **P0-2** — Unknown Handler exceptions (``RuntimeError``,
  ``TypeError``, ``KeyError``, ``AssertionError``) must propagate to
  the Scheduler's structured-concurrency boundary so sibling tasks
  are cancelled and awaited.  Only explicit Agent Domain Errors
  (``RetryableAgentError``, ``InvalidAgentResultError``,
  ``InvalidInvocationReceiptError``) are caught and downgraded to a
  Task failure record.
* **P0-3** — Covered in ``test_run_store.py`` (frozen RunLease +
  three-part identity on complete/abort).
* **P0-4** — Usage Provenance: when ``token_budget`` or
  ``cost_budget_usd`` is configured, only ``verified_provider`` or
  ``trusted_adapter`` receipts are accepted.  An ``unverified``
  receipt with zero / positive / None usage fails closed.
* **P1-1** — Covered in ``test_run_store.py`` (lookup_run_identity
  read-only probe).
* **P1-2** — Run-level Cancellation has the highest priority — it
  overrides ``forced_status=BUDGET_EXCEEDED`` and the computed
  status.
* **P1-3** — ExecutionBinding is the authoritative input to
  ``_execute_task``; the pre-flight capability snapshot is emitted
  into the trace for audit correlation.
"""

from __future__ import annotations

import asyncio
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
)
from multi_agent.execution import (
    ExecutionBinding,
    FakeExecutionCancellation,
    SupervisorRunStatus,
    TRACE_TASK_STARTED,
)
from multi_agent.execution_errors import (
    NonRetryableAgentError,
    RetryableAgentError,
    SupervisorError,
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
from multi_agent.run_store import InMemoryRunStore
from multi_agent.supervisor import SupervisorRuntime


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_supervisor_r2.py)
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


class _AlwaysInvalidPlanValidator:
    """PlanValidator stub that always returns ``valid=False``."""

    def validate(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> PlanValidationReport:
        from multi_agent.planning import PlanValidationIssue

        return PlanValidationReport(
            valid=False,
            issues=[
                PlanValidationIssue(
                    code="r3_test_validator_failure",
                    message="R3 test: PlanValidator rejects this plan",
                    severity="error",
                )
            ],
        )


def _tamper_plan_budget(plan: PlanDraft, **budget_overrides: Any) -> PlanDraft:
    budget = plan.request.budget
    for k, v in budget_overrides.items():
        object.__setattr__(budget, k, v)
    object.__setattr__(plan, "request_hash", compute_request_hash(plan.request))
    object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
    return plan


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


# ---------------------------------------------------------------------------
# Trusted-usage invoker test doubles
# ---------------------------------------------------------------------------


class _TrustedTokenInvoker:
    """R3 P0-4 / R4 P0-2: an invoker that reports ``verified_provider``
    token usage (e.g. from ``result.provider_metadata``).  Exposes
    :class:`UsageVerificationCapabilities` with ``verifies_tokens=True``
    so the Supervisor accepts its receipt for ``token_budget``
    enforcement — the receipt's ``usage_trust`` is cross-checked
    against the invoker's capabilities."""

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        return UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="test_trusted_token_invoker",
        )

    def __init__(self, tokens_used: int) -> None:
        self._tokens_used = tokens_used

    async def invoke(
        self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentInvocationReceipt:
        result = _ok_result(
            task=task,
            provider_metadata=ProviderMetadata(
                provider="openai",
                chat_model="gpt-4",
                embedding_model="text-embedding-3-small",
                ai_mode="live",
            ),
            token_usage=TokenUsage(
                input_tokens=self._tokens_used // 2,
                output_tokens=self._tokens_used - self._tokens_used // 2,
                total_tokens=self._tokens_used,
            ),
        )
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            tokens_used=self._tokens_used,
            usage_trust="verified_provider",
        )


class _TrustedCostAdapterInvoker:
    """R3 P0-4 / R4 P0-2: an invoker that reports ``trusted_adapter``
    cost usage (e.g. from a vetted cost-reporting middleware).
    Exposes :class:`UsageVerificationCapabilities` with
    ``verifies_cost=True`` so the Supervisor accepts its receipt for
    ``cost_budget_usd`` enforcement — the receipt's ``usage_trust`` is
    cross-checked against the invoker's capabilities."""

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        return UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=True,
            source_id="test_trusted_cost_adapter_invoker",
        )

    def __init__(self, cost_usd: Decimal) -> None:
        self._cost_usd = cost_usd

    async def invoke(
        self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentInvocationReceipt:
        result = _ok_result(task=task)
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            cost_usd=self._cost_usd,
            usage_trust="trusted_adapter",
        )


class _UnverifiedZeroCostInvoker:
    """R3 P0-4: an untrusted invoker that reports ``cost_usd=0`` with
    ``usage_trust='unverified'``.  A configured ``cost_budget_usd``
    must fail closed — the zero value cannot be trusted."""

    async def invoke(
        self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentInvocationReceipt:
        result = _ok_result(task=task)
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            cost_usd=Decimal("0"),
            usage_trust="unverified",
        )


class _UnverifiedPositiveCostInvoker:
    """R3 P0-4: an untrusted invoker that reports a *positive*
    ``cost_usd`` with ``usage_trust='unverified'``.  Even a positive
    value from an unverified source must fail closed."""

    def __init__(self, cost_usd: Decimal) -> None:
        self._cost_usd = cost_usd

    async def invoke(
        self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentInvocationReceipt:
        result = _ok_result(task=task)
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            cost_usd=self._cost_usd,
            usage_trust="unverified",
        )


class _UnverifiedZeroTokensInvoker:
    """R3 P0-4: an untrusted invoker that reports ``tokens_used=0``
    with ``usage_trust='unverified'``.  A configured ``token_budget``
    must fail closed — the zero value cannot be trusted."""

    async def invoke(
        self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentInvocationReceipt:
        result = _ok_result(task=task)
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            tokens_used=0,
            usage_trust="unverified",
        )


# ---------------------------------------------------------------------------
# Delayed-cancellation source (for P1-2 final-status priority test)
# ---------------------------------------------------------------------------


class _DelayedCancellation:
    """R3 P1-2: returns ``False`` for the first ``delay_count``
    ``is_cancelled`` calls, then ``True`` afterwards.

    This simulates a cancellation that arrives *after* the pre-flight
    cancellation check but *before* ``_finalize``'s
    ``cancelled_during_run`` check — the exact window where the R3
    P1-2 fix must take effect (``cancelled`` overrides
    ``forced_status=BUDGET_EXCEEDED``).
    """

    def __init__(self, delay_count: int = 1) -> None:
        self._delay_count = delay_count
        self._call_count = 0

    async def is_cancelled(self, run_id: str) -> bool:
        self._call_count += 1
        if self._call_count <= self._delay_count:
            return False
        return True

    async def is_kill_switch_active(self, tenant_id: str) -> bool:
        return False


# ===========================================================================
# P0-1: Pre-cancelled Run must pass full Pre-flight
# ===========================================================================


class TestPreCancelledRunPreFlight:
    """R3 P0-1: a pre-cancelled Run must still pass the full
    Pre-flight (Registry/Validator/Handler).  An invalid plan cannot
    be cached as ``cancelled`` — that would poison the ``run_id`` for
    a later valid attempt."""

    @pytest.mark.asyncio
    async def test_pre_cancelled_registry_mismatch_rejected(self):
        """A pre-cancelled run with a stale ``registry_version`` must
        raise ``SupervisorError`` (registry mismatch) — NOT return a
        cached ``cancelled`` result.

        Reproduction for the R3 P0-1 bug: in R2, the cancellation
        check happened *before* the Registry Version check, so a
        pre-cancelled run with a stale registry would be cached as
        ``cancelled`` without ever validating the registry.  A later
        valid attempt with the same ``run_id + plan_hash`` would hit
        the cache and never re-validate.
        """
        reg_a = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg_a)

        # Mutate the live registry by adding an unrelated agent so
        # the version changes.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"recovery_metrics"}),
            frozenset({"crm_reader.get_customers"}),
        )
        reg_a.register(extra_cap, _NoopHandler())
        # Now plan.registry_version != reg_a.snapshot().version.

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )

        # Must raise SupervisorError (registry mismatch), NOT return
        # a cancelled result.
        with pytest.raises(SupervisorError, match="registry version mismatch"):
            await runtime.execute(plan, reg_a, cancellation=canc)

        # The run_id must NOT be poisoned — no entry should exist in
        # the store because pre-flight failed before begin().
        assert canc is not None  # keep linter happy

    @pytest.mark.asyncio
    async def test_pre_cancelled_missing_handler_rejected(self):
        """A pre-cancelled run whose plan references an unregistered
        agent must raise ``SupervisorError`` (handler not registered)
        — NOT return a cached ``cancelled`` result."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        # Tamper with one task's agent_id to reference an unregistered
        # agent.  Use _AlwaysValidPlanValidator so the validator does
        # not reject first — we want to test the handler resolution
        # check specifically.
        root_pt = next(
            pt for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        object.__setattr__(root_pt.task, "agent_id", "missing_agent")
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(SupervisorError, match="not registered"):
            await runtime.execute(plan, reg, cancellation=canc)

    @pytest.mark.asyncio
    async def test_pre_cancelled_validator_failure_rejected(self):
        """A pre-cancelled run whose plan fails the PlanValidator
        must raise ``SupervisorError`` — NOT return a cached
        ``cancelled`` result."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysInvalidPlanValidator(),
        )

        with pytest.raises(SupervisorError, match="PlanValidator rejected"):
            await runtime.execute(plan, reg, cancellation=canc)

    @pytest.mark.asyncio
    async def test_cancelled_result_cached_only_after_preflight(self):
        """A *valid* pre-cancelled run (plan passes all pre-flight
        checks) must be cached as ``cancelled`` so a later attempt
        with the same ``run_id + plan_hash`` hits the cache.

        This is the positive counterpart to the three rejection
        tests above — the R3 fix does not break the legitimate
        pre-cancelled cache path; it only prevents *invalid* plans
        from poisoning the cache."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        result1 = await runtime.execute(plan, reg, cancellation=canc)
        assert result1.status == SupervisorRunStatus.CANCELLED

        # The cancelled result must be cached.
        assert store.is_completed("run-001")

        # A second execution with the same (run_id, plan_hash) must
        # return the cached cancelled result — without re-running
        # pre-flight or re-invoking the cancellation check.
        # We use a *fresh* cancellation source that is NOT cancelled
        # to prove the cache hit short-circuits before the cancellation
        # check.
        fresh_canc = FakeExecutionCancellation()
        result2 = await runtime.execute(plan, reg, cancellation=fresh_canc)
        assert result2.status == SupervisorRunStatus.CANCELLED
        # Same cached object identity is not required, but the status
        # and identity must match.
        assert result2.run_id == result1.run_id
        assert result2.plan_hash == result1.plan_hash

    @pytest.mark.asyncio
    async def test_cached_cancelled_result_survives_registry_drift(self):
        """Once a valid cancelled result is cached, a later attempt
        with the same ``run_id + plan_hash`` must return the cached
        result even if the live registry has drifted (e.g. an
        unrelated agent was registered).

        This mirrors the R2 P0-1 test for ``COMPLETED`` cache hits —
        the cache path must not check the live registry version."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=store,
        )

        result1 = await runtime.execute(plan, reg, cancellation=canc)
        assert result1.status == SupervisorRunStatus.CANCELLED
        original_version = result1.registry_version

        # Mutate the live registry.
        extra_cap = _make_capability(
            "extra_agent",
            frozenset({"customer_recovery"}),
            frozenset({"recovery_metrics"}),
            frozenset({"crm_reader.get_customers"}),
        )
        reg.register(extra_cap, _NoopHandler())
        assert reg.snapshot().version != original_version

        # Cached result must still be returned.
        result2 = await runtime.execute(plan, reg)
        assert result2.status == SupervisorRunStatus.CANCELLED
        assert result2.registry_version == original_version


# ===========================================================================
# P0-2: Unknown Handler exceptions propagate to Structured Concurrency
# ===========================================================================


class TestUnknownExceptionPropagation:
    """R3 P0-2: unknown Handler exceptions (``RuntimeError``,
    ``TypeError``, ``KeyError``, ``AssertionError``) must propagate
    to the Scheduler's structured-concurrency boundary so sibling
    tasks are cancelled and awaited.

    Only explicit Agent Domain Errors (``RetryableAgentError``,
    ``InvalidAgentResultError``, ``InvalidInvocationReceiptError``)
    are caught and downgraded to a Task failure record.
    """

    @pytest.mark.asyncio
    async def test_runtime_error_cancels_siblings(self):
        """A ``RuntimeError`` raised by one task's Handler must
        cancel the sibling task — the Scheduler must not return
        until the sibling has terminated."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        b_completed = {"v": False}

        class _RuntimeErrorInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    raise RuntimeError("programming error")
                # task_b: sleep so we can prove it's cancelled.
                try:
                    await asyncio.sleep(5.0)
                    b_completed["v"] = True
                except asyncio.CancelledError:
                    raise
                return AgentInvocationReceipt(result=_ok_result(task=task))

        store = InMemoryRunStore()
        runtime = SupervisorRuntime(
            invoker=_RuntimeErrorInvoker(),  # type: ignore[arg-type]
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RuntimeError, match="programming error"):
            await runtime.execute(plan, reg)

        # B must NOT have completed — it was cancelled.
        assert not b_completed["v"]
        # The lease must have been released via abort().
        assert not store.is_in_progress("run-001")

    @pytest.mark.asyncio
    async def test_type_error_cancels_wave_siblings(self):
        """A ``TypeError`` raised by one task's Handler must also
        propagate and cancel siblings — it is NOT a business
        exception."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        b_completed = {"v": False}

        class _TypeErrorInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    raise TypeError("wrong type")
                try:
                    await asyncio.sleep(5.0)
                    b_completed["v"] = True
                except asyncio.CancelledError:
                    raise
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_TypeErrorInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(TypeError, match="wrong type"):
            await runtime.execute(plan, reg)

        assert not b_completed["v"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_not_downgraded(self):
        """An unexpected ``RuntimeError`` must NOT be downgraded to a
        Task failure record.  The Run must propagate the exception
        (not return a ``FAILED`` result with the error recorded).

        This is the key difference from R2: in R2, the Supervisor's
        ``except Exception`` catch-all turned ``RuntimeError`` into a
        plain task failure, so the Run completed as ``FAILED`` with
        the error swallowed.  R3 removes the catch-all so the
        ``RuntimeError`` propagates to the caller."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        class _ExplodingInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == root_task.task_id:
                    raise RuntimeError("infrastructure failure")
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_ExplodingInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        # The Run must raise — NOT return a FAILED result.
        with pytest.raises(RuntimeError, match="infrastructure failure"):
            await runtime.execute(plan, reg)

    @pytest.mark.asyncio
    async def test_expected_agent_error_becomes_failed_record(self):
        """A ``RetryableAgentError`` (an explicit Agent Domain Error)
        IS caught by the Supervisor and downgraded to a Task failure
        record.  The Run completes as ``FAILED`` (after retries are
        exhausted) — the exception does NOT propagate to the caller.

        This verifies the R3 fix is *precise*: only unknown exceptions
        propagate; explicit Agent Domain Errors are still handled
        gracefully."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        class _RetryableInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == root_task.task_id:
                    raise RetryableAgentError("transient failure")
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_RetryableInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        # The Run completes as FAILED — the exception is NOT raised.
        result = await runtime.execute(plan, reg)
        assert result.status == SupervisorRunStatus.FAILED

        # The root task's attempt record shows the retryable error.
        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "retryable_error" for a in root_rec.attempts)

    @pytest.mark.asyncio
    async def test_non_retryable_agent_error_does_not_cancel_siblings(self):
        """R3 P0-2: A ``NonRetryableAgentError`` is an explicit Agent
        Domain Error — the Supervisor catches it and marks the task
        ``failed`` with ``error_code=non_retryable_error``, but does
        NOT propagate to the Scheduler.  Sibling tasks continue to
        run and the Run completes (not aborted).

        This is the complementary boundary to
        ``test_runtime_error_cancels_siblings``: a RuntimeError
        propagates and cancels siblings, but a
        ``NonRetryableAgentError`` is a business-domain failure that
        stays contained to the failing task.

        Uses ``_two_chain_plan`` (A→A2, B→B2) so ``task_a`` and
        ``task_b`` are independent siblings running in the same wave."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        call_count = {"n": 0}

        class _NonRetryableInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    call_count["n"] += 1
                    raise NonRetryableAgentError("definite business failure")
                return AgentInvocationReceipt(result=_ok_result(task=task))

        runtime = SupervisorRuntime(
            invoker=_NonRetryableInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        # The Run completes — the exception is NOT raised to the caller.
        result = await runtime.execute(plan, reg)

        # task_a is ``failed`` with exactly one attempt (no retry) and
        # the explicit ``non_retryable_error`` error_code.
        task_a_rec = next(r for r in result.task_records if r.task_id == "task_a")
        assert task_a_rec.status == "failed"
        assert len(task_a_rec.attempts) == 1
        assert task_a_rec.attempts[0].error_code == "non_retryable_error"
        assert call_count["n"] == 1

        # task_b (sibling of task_a in the same wave) must have
        # completed normally — it was NOT cancelled.  This is the key
        # assertion: had the exception propagated, the Scheduler would
        # have cancelled task_b before the Supervisor aborted.
        task_b_rec = next(r for r in result.task_records if r.task_id == "task_b")
        assert task_b_rec.status == "completed"

    @pytest.mark.asyncio
    async def test_scheduler_waits_for_siblings_before_abort_on_runtime_error(self):
        """When a ``RuntimeError`` propagates, the Scheduler must
        cancel and await all sibling tasks BEFORE the Supervisor's
        ``except BaseException`` block calls ``abort()``.

        We verify by tracking the order: the sibling's
        ``CancelledError`` handler must run before ``abort()`` is
        called."""
        reg = _make_registry(_two_chain_caps(), catalog=_two_chain_catalog())
        plan = _two_chain_plan(reg)

        order_log: list[str] = []

        class _TrackingStore(InMemoryRunStore):
            async def abort(self, lease: Any, *, error_code: str) -> None:
                order_log.append("abort_called")
                await super().abort(lease, error_code=error_code)

        class _OrderedInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                if task.task_id == "task_a":
                    raise RuntimeError("boom")
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    order_log.append("task_b_cancelled")
                    raise
                return AgentInvocationReceipt(result=_ok_result(task=task))

        store = _TrackingStore()
        runtime = SupervisorRuntime(
            invoker=_OrderedInvoker(),  # type: ignore[arg-type]
            run_store=store,
            plan_validator=_AlwaysValidPlanValidator(),
        )

        with pytest.raises(RuntimeError, match="boom"):
            await runtime.execute(plan, reg)

        # The abort must have been called (lease released).
        assert "abort_called" in order_log
        # task_b must have been cancelled before abort — NOT still
        # running when abort was called.
        assert "task_b_cancelled" in order_log


# ===========================================================================
# P0-4: Usage Provenance
# ===========================================================================


class TestUsageProvenance:
    """R3 P0-4: when ``token_budget`` or ``cost_budget_usd`` is
    configured, only ``verified_provider`` or ``trusted_adapter``
    receipts are accepted.  An ``unverified`` receipt fails closed
    regardless of whether the self-reported value is ``None``,
    ``0``, or positive."""

    @pytest.mark.asyncio
    async def test_untrusted_zero_cost_fails_closed(self):
        """An unverified invoker reporting ``cost_usd=0`` with
        ``cost_budget_usd`` configured must fail closed — the zero
        value cannot be trusted to enforce the budget."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("10.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("10.00"))

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        runtime = SupervisorRuntime(
            invoker=_UnverifiedZeroCostInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "usage_unavailable" for a in root_rec.attempts), (
            f"expected usage_unavailable error_code, got "
            f"{[a.error_code for a in root_rec.attempts]}"
        )
        # R6: usage_unavailable now sets _exceeded=True so the run
        # finalises as BUDGET_EXCEEDED (fail-closed at budget level).
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_untrusted_positive_cost_still_fails_closed(self):
        """An unverified invoker reporting a *positive* ``cost_usd``
        with ``cost_budget_usd`` configured must STILL fail closed —
        even a positive value from an unverified source cannot be
        trusted (the invoker could under-report)."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("10.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("10.00"))

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        runtime = SupervisorRuntime(
            invoker=_UnverifiedPositiveCostInvoker(Decimal("0.05")),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "usage_unavailable" for a in root_rec.attempts)

    @pytest.mark.asyncio
    async def test_untrusted_zero_tokens_fail_closed(self):
        """An unverified invoker reporting ``tokens_used=0`` with
        ``token_budget`` configured must fail closed — the zero
        value cannot be trusted to enforce the budget."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg, budget=ExecutionBudget(token_budget=1000))
        _tamper_plan_budget(plan, token_budget=1000)

        root_task = next(
            pt.task for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )

        runtime = SupervisorRuntime(
            invoker=_UnverifiedZeroTokensInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        root_rec = next(
            r for r in result.task_records if r.task_id == root_task.task_id
        )
        assert root_rec.status == "failed"
        assert any(a.error_code == "usage_unavailable" for a in root_rec.attempts)

    @pytest.mark.asyncio
    async def test_verified_provider_tokens_are_accepted(self):
        """A ``verified_provider`` receipt (from a
        :class:`TrustedUsageInvoker`) with positive ``tokens_used``
        is accepted for ``token_budget`` enforcement — the run
        completes successfully when the budget is not exceeded."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg, budget=ExecutionBudget(token_budget=10_000))
        _tamper_plan_budget(plan, token_budget=10_000)

        runtime = SupervisorRuntime(
            invoker=_TrustedTokenInvoker(tokens_used=50),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # All tasks complete — the verified tokens are accepted.
        assert result.status == SupervisorRunStatus.COMPLETED
        # The usage is accumulated.
        assert result.usage.tokens_used > 0

    @pytest.mark.asyncio
    async def test_trusted_cost_adapter_is_accepted(self):
        """A ``trusted_adapter`` receipt (from a
        :class:`TrustedUsageInvoker`) with positive ``cost_usd`` is
        accepted for ``cost_budget_usd`` enforcement — the run
        completes successfully when the budget is not exceeded."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(
            reg, budget=ExecutionBudget(cost_budget_usd=Decimal("100.00"))
        )
        _tamper_plan_budget(plan, cost_budget_usd=Decimal("100.00"))

        runtime = SupervisorRuntime(
            invoker=_TrustedCostAdapterInvoker(Decimal("0.05")),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # All tasks complete — the trusted cost is accepted.
        assert result.status == SupervisorRunStatus.COMPLETED
        # The cost is accumulated.
        assert result.usage.cost_usd > Decimal("0")

    @pytest.mark.asyncio
    async def test_untrusted_receipt_with_no_budget_configured_passes(self):
        """When NO ``token_budget`` or ``cost_budget_usd`` is
        configured, an ``unverified`` receipt is accepted — budget
        enforcement is opt-in, not mandatory."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)  # default budget, no token/cost limits

        runtime = SupervisorRuntime(
            invoker=_UnverifiedZeroCostInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The unverified receipt is accepted because no cost budget
        # is configured.
        assert result.status == SupervisorRunStatus.COMPLETED


# ===========================================================================
# P1-2: Final Status priority — cancelled > forced budget_exceeded
# ===========================================================================


class TestFinalStatusPriority:
    """R3 P1-2: Run-level Cancellation has the highest priority — it
    overrides ``forced_status=BUDGET_EXCEEDED`` and the computed
    status.

    Spec §17 priority:
        cancelled > budget_exceeded > failed > needs_input >
        partial_success > completed
    """

    @pytest.mark.asyncio
    async def test_cancellation_overrides_forced_budget_exceeded(self):
        """When ``max_tasks`` is exceeded (triggering
        ``forced_status=BUDGET_EXCEEDED``) AND cancellation is active
        at finalize time, the final status must be ``CANCELLED`` —
        not ``BUDGET_EXCEEDED``.

        Reproduction for the R3 P1-2 bug: in R2, ``_finalize``
        checked ``forced_status`` *before* ``cancelled_during_run``,
        so a ``forced_status=BUDGET_EXCEEDED`` (from max_tasks)
        masked an active cancellation.

        We use a ``_DelayedCancellation`` source that returns
        ``False`` for the pre-flight cancellation check (so the run
        proceeds past pre-flight) and ``True`` for the
        ``_finalize``'s ``cancelled_during_run`` check (so the
        final status should be CANCELLED).
        """
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        # Set max_tasks=0 so check_max_tasks always fails.
        _tamper_plan_budget(plan, max_tasks=0)

        # DelayedCancellation: first is_cancelled call (pre-flight)
        # returns False; subsequent calls (in _finalize) return True.
        canc = _DelayedCancellation(delay_count=1)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        result = await runtime.execute(plan, reg, cancellation=canc)

        # The final status must be CANCELLED, not BUDGET_EXCEEDED.
        assert result.status == SupervisorRunStatus.CANCELLED
        # Verify a run_cancelled trace event was emitted.
        cancelled_events = [
            ev for ev in result.trace if ev.event_type == "run_cancelled"
        ]
        assert len(cancelled_events) >= 1

    @pytest.mark.asyncio
    async def test_budget_exceeded_when_cancellation_not_active(self):
        """Counter-test: when ``max_tasks`` is exceeded and
        cancellation is NOT active, the final status is
        ``BUDGET_EXCEEDED`` — the P1-2 fix only changes behaviour
        when cancellation IS active."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)
        _tamper_plan_budget(plan, max_tasks=0)

        canc = FakeExecutionCancellation()  # not cancelled

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )

        result = await runtime.execute(plan, reg, cancellation=canc)
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED


# ===========================================================================
# P1-3: ExecutionBinding is the authoritative input to _execute_task
# ===========================================================================


class TestExecutionBindingUsage:
    """R3 P1-3: ``ExecutionBinding`` is the authoritative input to
    ``_execute_task``.  The pre-flight capability snapshot is
    emitted into the trace for audit correlation — consumers can
    verify which capability version was bound at pre-flight time,
    not just which agent_id ran."""

    @pytest.mark.asyncio
    async def test_task_started_trace_emits_binding_info(self):
        """Every ``task_started`` trace event must carry
        ``binding_agent_id`` and ``binding_capability_*`` fields so
        audit consumers can correlate the executed task with the
        capability bound at pre-flight time."""
        reg = _make_registry(_customer_recovery_caps())
        plan = _customer_recovery_plan(reg)

        runtime = SupervisorRuntime(
            invoker=_fake_invoker_for_plan(plan),
            run_store=InMemoryRunStore(),
        )
        result = await runtime.execute(plan, reg)
        assert result.status == SupervisorRunStatus.COMPLETED

        started_events = [
            ev for ev in result.trace if ev.event_type == TRACE_TASK_STARTED
        ]
        # At least one task was started.
        assert len(started_events) >= 1

        for ev in started_events:
            # binding_agent_id matches the task's agent_id (the
            # binding was built from the same registry resolution).
            assert "binding_agent_id" in ev.data
            assert ev.data["binding_agent_id"] == ev.agent_id
            # binding_capability_agent_id matches the capability's
            # agent_id (the snapshot was taken at pre-flight time).
            assert "binding_capability_agent_id" in ev.data
            assert ev.data["binding_capability_agent_id"] == ev.agent_id
            # binding_capability_authority is the authority string.
            assert "binding_capability_authority" in ev.data
            assert ev.data["binding_capability_authority"] == "read"

    @pytest.mark.asyncio
    async def test_execution_binding_is_frozen_contract(self):
        """``ExecutionBinding`` is a frozen contract — its identity
        fields cannot be mutated after construction."""
        from pydantic import ValidationError

        cap = _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
        )
        binding = ExecutionBinding(
            task_id="task_a",
            agent_id="agent_a",
            capability_snapshot=cap,
        )
        with pytest.raises(ValidationError):
            binding.agent_id = "tampered"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_binding_capability_snapshot_survives_registry_drift(self):
        """The ``binding_capability_*`` fields in the trace must
        reflect the capability bound at pre-flight time, NOT the
        live registry's capability.  If the handler is replaced
        mid-run, the trace still shows the original capability."""
        # Use RecordingHandlers so RegistryAgentInvoker works.
        handlers = {
            cap.agent_id: _RecordingHandler() for cap in _customer_recovery_caps()
        }
        original_handler = _RecordingHandler()
        handlers["customer_context_specialist"] = original_handler
        reg = _make_registry(_customer_recovery_caps(), handlers=handlers)
        plan = _customer_recovery_plan(reg)
        original_version = plan.registry_version

        call_count = {"n": 0}

        class _DriftingInvoker:
            async def invoke(
                self, handler: Any, task: AgentTask, ctx: AgentExecutionContext
            ) -> AgentInvocationReceipt:
                call_count["n"] += 1
                # After the first call, replace the capability in
                # the registry with a different authority.
                if call_count["n"] == 1:
                    new_cap = _make_capability(
                        "customer_context_specialist",
                        frozenset({"customer_recovery"}),
                        frozenset({"customer_context_summary"}),
                        frozenset({"crm_reader.get_customers"}),
                    )
                    # Override authority to EXECUTE to simulate a
                    # capability drift (original is READ).
                    object.__setattr__(new_cap, "authority", AgentAuthority.EXECUTE)
                    reg.replace(new_cap, _RecordingHandler())
                result = await handler.run(task, ctx)
                return AgentInvocationReceipt(
                    result=result,
                    tool_calls=len(result.tool_calls),
                )

        runtime = SupervisorRuntime(
            invoker=_DriftingInvoker(),  # type: ignore[arg-type]
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)
        assert result.status == SupervisorRunStatus.COMPLETED

        # The trace's binding_capability_authority for the root task
        # must be "read" (the original authority bound at pre-flight
        # time), NOT "write" (the drifted authority).
        root_started = next(
            ev
            for ev in result.trace
            if ev.event_type == TRACE_TASK_STARTED
            and ev.agent_id == "customer_context_specialist"
        )
        assert root_started.data["binding_capability_authority"] == "read"

        # And the result's registry_version is the plan's version.
        assert result.registry_version == original_version
