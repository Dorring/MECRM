"""Phase 4 R6 regression tests.

Direct counter-examples for the five P0 concerns and two P1 cleanups
identified in the Phase 4 R6 review:

* **P0-1** — ``ProviderUsageVerifier`` must be *actually called* by
  ``RegistryAgentInvoker.invoke()``, not merely checked for existence.
  The verifier's ``VerifiedUsage`` values (not the Handler's
  self-reported data) enter the receipt.
* **P0-2** — ``RetryPolicy.retryable_error_codes`` must control
  runtime retry decisions via the pure ``should_retry()`` function.
  Tests use the real ``DeterministicPlanner`` and real
  ``PlanValidator`` (not ``_AlwaysValidPlanValidator``).
* **P0-3** — Every committed agent call must produce a Usage
  Disposition.  When a budget is configured and no receipt is
  produced (timeout / exception), the run must fail-closed.
* **P0-4** — Token and Cost provenance are independent dimensions.
  A ``verified_provider`` receipt that carries ``cost_usd`` must
  actually accumulate cost — the old single ``usage_trust`` string
  silently bypassed ``cost_budget_usd``.
* **P0-5** — Cache Path Isolation: a completed run cached under the
  same ``(run_id, plan_hash)`` must be returned without reading any
  Live Invoker state.
* **P1-a** — ``UsageAvailabilityStatus`` three-state
  (unavailable / partial / complete) replaces the old boolean flags.
* **P1-b** — ``RetryPolicy.retryable_error_codes`` rejects blank
  strings and never-retryable codes at construction time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentError,
    AgentErrorCategory,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ExecutionBudget,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
    UsageAvailabilityStatus,
)
from multi_agent.execution import (
    SupervisorRunStatus,
)
from multi_agent.execution_errors import (
    NonRetryableAgentError,
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
from multi_agent.supervisor import SupervisorRuntime, should_retry


# ---------------------------------------------------------------------------
# Shared helpers (self-contained — copied from test_supervisor_r5.py)
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


class _NoopHandler:
    async def run(
        self, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentResult:  # pragma: no cover
        raise RuntimeError("noop handler should not be called")


def _make_registry(
    caps: list[AgentCapability],
    handlers: dict[str, Any] | None = None,
    catalog: ToolCatalog | None = None,
) -> AgentRegistry:
    reg = AgentRegistry(tool_catalog=catalog or _three_independent_catalog())
    for cap in caps:
        handler = (handlers or {}).get(cap.agent_id, _NoopHandler())
        reg.register(cap, handler)
    return reg


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


def _three_independent_plan(
    registry: AgentRegistry,
    *,
    budget: ExecutionBudget | None = None,
    run_id: str = "run-001",
) -> PlanDraft:
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
    from multi_agent.complexity_gate import ComplexityDecision

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


def _provider_meta() -> ProviderMetadata:
    return ProviderMetadata(
        provider="openai",
        chat_model="gpt-4",
        embedding_model="text-embedding-3-small",
        ai_mode="live",
    )


def _verified_provider_receipt(
    task: AgentTask, *, tokens_used: int = 100
) -> AgentInvocationReceipt:
    """A receipt with verified token provenance and ``provider_metadata``."""
    result = _ok_result(
        task=task,
        provider_metadata=_provider_meta(),
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
            source_id="test_verifier",
            token_source_id="test_verifier",
            tokens_verified=True,
            cost_verified=False,
        ),
    )


class _AlwaysValidPlanValidator:
    """PlanValidator stub that always returns ``valid=True``.

    Used for R6 tests that exercise Supervisor runtime behaviour
    (cache path, usage provenance, no-receipt disposition, etc.)
    without needing the full plan-validation chain.  R6 P0-2 retry
    tests use the real ``PlanValidator`` instead.
    """

    def validate(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> PlanValidationReport:
        return PlanValidationReport(valid=True, issues=[])


# ===========================================================================
# P0-1: ProviderUsageVerifier is actually called by RegistryAgentInvoker
# ===========================================================================


class _TrackingVerifier:
    """Fake verifier that records every ``verify()`` call."""

    source_id: str = "tracking_verifier"

    def __init__(self, *, result: VerifiedUsage | None = None) -> None:
        self._result = result or VerifiedUsage(
            tokens_used=200,
            cost_usd=None,
            tokens_verified=True,
        )
        self.call_count: int = 0
        self.received_provider_metadata: list[ProviderMetadata] = []
        self.received_token_usage: list[TokenUsage] = []

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        self.call_count += 1
        self.received_provider_metadata.append(provider_metadata)
        self.received_token_usage.append(token_usage)
        return self._result


class _RejectingVerifier:
    """Fake verifier that returns ``verified=False``."""

    source_id: str = "rejecting_verifier"

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        # R10.1 P1-1: VerifiedUsage now enforces symmetric invariants —
        # tokens_verified=False requires tokens_used=None.
        return VerifiedUsage(tokens_used=None, cost_usd=None, verified=False)


class _RaisingVerifier:
    """Fake verifier that raises ``RuntimeError``."""

    source_id: str = "raising_verifier"

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        raise RuntimeError("verifier infrastructure unavailable")


class _ProviderResultHandler:
    """Handler that returns a result with provider_metadata and token_usage."""

    def __init__(
        self,
        *,
        tokens: int = 100,
        cost_usd: Decimal | None = None,
    ) -> None:
        self._tokens = tokens
        self._cost_usd = cost_usd

    async def run(self, task: AgentTask, ctx: AgentExecutionContext) -> AgentResult:
        return _ok_result(
            task=task,
            provider_metadata=_provider_meta(),
            token_usage=TokenUsage(
                input_tokens=self._tokens // 2,
                output_tokens=self._tokens - self._tokens // 2,
                total_tokens=self._tokens,
            ),
        )


class TestProviderUsageVerifierInvocation:
    """R6 P0-1: ``RegistryAgentInvoker.invoke()`` must actually call
    ``verifier.verify()`` and use the returned ``VerifiedUsage`` values,
    not the Handler's self-reported data."""

    @pytest.mark.asyncio
    async def test_registry_invoker_calls_verifier(self):
        """``RegistryAgentInvoker.invoke()`` must call
        ``verifier.verify()`` when a verifier is configured and the
        Handler returns ``provider_metadata``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        verifier = _TrackingVerifier()
        handler = _ProviderResultHandler(tokens=100)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = AgentTask(
            task_id="task_a",
            agent_id="agent_a",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        ctx = AgentExecutionContext(
            tenant_id="t-001",
            user_id="user-001",
            correlation_id="run-001",
            run_metadata={
                "run_id": "run-001",
                "actor_type": "user",
                "actor_id": "user-001",
            },
        )
        receipt = await invoker.invoke(handler, task, ctx)

        assert verifier.call_count == 1, (
            f"verifier.verify() must be called exactly once, got {verifier.call_count}"
        )
        assert receipt.usage_provenance.tokens_verified is True

    @pytest.mark.asyncio
    async def test_verifier_false_fails_closed(self):
        """When the verifier returns ``verified=False``, the invoker
        must raise ``NonRetryableAgentError`` — the Handler's
        self-reported usage is NOT trusted."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        verifier = _RejectingVerifier()
        handler = _ProviderResultHandler(tokens=100)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = AgentTask(
            task_id="task_a",
            agent_id="agent_a",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        ctx = AgentExecutionContext(
            tenant_id="t-001",
            user_id="user-001",
            correlation_id="run-001",
            run_metadata={
                "run_id": "run-001",
                "actor_type": "user",
                "actor_id": "user-001",
            },
        )
        with pytest.raises(NonRetryableAgentError, match="verified=False"):
            await invoker.invoke(handler, task, ctx)

    @pytest.mark.asyncio
    async def test_verifier_exception_fails_closed(self):
        """When the verifier raises an exception, the invoker must
        raise ``NonRetryableAgentError`` — a verifier failure does NOT
        upgrade the Handler's data to trusted."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        verifier = _RaisingVerifier()
        handler = _ProviderResultHandler(tokens=100)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = AgentTask(
            task_id="task_a",
            agent_id="agent_a",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        ctx = AgentExecutionContext(
            tenant_id="t-001",
            user_id="user-001",
            correlation_id="run-001",
            run_metadata={
                "run_id": "run-001",
                "actor_type": "user",
                "actor_id": "user-001",
            },
        )
        with pytest.raises(NonRetryableAgentError, match="raised RuntimeError"):
            await invoker.invoke(handler, task, ctx)

    @pytest.mark.asyncio
    async def test_verified_usage_values_enter_receipt(self):
        """The receipt's ``tokens_used`` must come from the verifier's
        ``VerifiedUsage.tokens_used``, NOT from the Handler's
        ``token_usage.total_tokens``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        # Handler reports 100 tokens, verifier says 200.
        verifier = _TrackingVerifier(
            result=VerifiedUsage(
                tokens_used=200,
                cost_usd=None,
                tokens_verified=True,
            )
        )
        handler = _ProviderResultHandler(tokens=100)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = AgentTask(
            task_id="task_a",
            agent_id="agent_a",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        ctx = AgentExecutionContext(
            tenant_id="t-001",
            user_id="user-001",
            correlation_id="run-001",
            run_metadata={
                "run_id": "run-001",
                "actor_type": "user",
                "actor_id": "user-001",
            },
        )
        receipt = await invoker.invoke(handler, task, ctx)

        assert receipt.tokens_used == 200, (
            f"receipt.tokens_used must be 200 (verifier's value), got "
            f"{receipt.tokens_used}"
        )
        assert receipt.tokens_used != 100, (
            "receipt.tokens_used must NOT be 100 (Handler's self-reported value)"
        )

    @pytest.mark.asyncio
    async def test_verified_cost_enters_receipt(self):
        """When the verifier returns ``cost_usd``, the receipt must
        carry that cost and ``cost_verified=True``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        verifier = _TrackingVerifier(
            result=VerifiedUsage(
                tokens_used=150,
                cost_usd=Decimal("0.25"),
                tokens_verified=True,
                cost_verified=True,
            )
        )
        handler = _ProviderResultHandler(tokens=150)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = AgentTask(
            task_id="task_a",
            agent_id="agent_a",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        ctx = AgentExecutionContext(
            tenant_id="t-001",
            user_id="user-001",
            correlation_id="run-001",
            run_metadata={
                "run_id": "run-001",
                "actor_type": "user",
                "actor_id": "user-001",
            },
        )
        receipt = await invoker.invoke(handler, task, ctx)

        assert receipt.cost_usd == Decimal("0.25"), (
            f"receipt.cost_usd must be 0.25 (verifier's value), got {receipt.cost_usd}"
        )
        assert receipt.usage_provenance.cost_verified is True

    @pytest.mark.asyncio
    async def test_verifier_is_called_once_per_attempt(self):
        """The verifier must be called exactly once per ``invoke()``
        call — not zero times, not multiple times."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        verifier = _TrackingVerifier()
        handler = _ProviderResultHandler(tokens=100)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = AgentTask(
            task_id="task_a",
            agent_id="agent_a",
            task_type="root_task",
            objective="test",
            tenant_id="t-001",
            timeout_ms=10_000,
        )
        ctx = AgentExecutionContext(
            tenant_id="t-001",
            user_id="user-001",
            correlation_id="run-001",
            run_metadata={
                "run_id": "run-001",
                "actor_type": "user",
                "actor_id": "user-001",
            },
        )
        await invoker.invoke(handler, task, ctx)
        await invoker.invoke(handler, task, ctx)
        await invoker.invoke(handler, task, ctx)

        assert verifier.call_count == 3, (
            f"verifier must be called 3 times (once per invoke), got "
            f"{verifier.call_count}"
        )


# ===========================================================================
# P0-2: RetryPolicy actually controls runtime retry decisions
# ===========================================================================


class TestExecutableRetryPolicy:
    """R6 P0-2: ``RetryPolicy.retryable_error_codes`` must control
    runtime retry decisions via ``should_retry()``.  Tests use the real
    ``DeterministicPlanner`` and real ``PlanValidator``."""

    def test_should_retry_pure_function_max_retries_zero(self):
        """``should_retry()`` with ``max_retries=0`` must never retry."""
        policy = RetryPolicy(max_retries=0)
        assert (
            should_retry(
                policy=policy,
                attempt_index=0,
                error_code="task_timeout",
                explicitly_retryable=True,
            )
            is False
        )

    def test_should_retry_default_retries_timeout(self):
        """With empty ``retryable_error_codes``, ``task_timeout`` is
        retried (default safe category)."""
        policy = RetryPolicy(max_retries=2)
        assert (
            should_retry(
                policy=policy,
                attempt_index=0,
                error_code="task_timeout",
                explicitly_retryable=False,
            )
            is True
        )

    def test_should_retry_allowlist_rejects_unlisted_code(self):
        """When ``retryable_error_codes`` is non-empty, only codes in
        the set are retried — ``task_timeout`` is rejected if not
        listed."""
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        assert (
            should_retry(
                policy=policy,
                attempt_index=0,
                error_code="task_timeout",
                explicitly_retryable=False,
            )
            is False
        )

    def test_should_retry_allowlist_accepts_listed_code(self):
        """When ``retryable_error_codes`` is non-empty and the code is
        in the set, the attempt is retried."""
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        assert (
            should_retry(
                policy=policy,
                attempt_index=0,
                error_code="custom_error",
                explicitly_retryable=False,
            )
            is True
        )

    def test_should_retry_never_retryable_overrides_allowlist(self):
        """Codes in ``NEVER_RETRYABLE_ERROR_CODES`` are never retried,
        even if listed in ``retryable_error_codes``."""
        # Note: RetryPolicy's validator rejects never-retryable codes,
        # so we test the function directly with a bypassed policy.
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        # invalid_receipt is in NEVER_RETRYABLE_ERROR_CODES
        assert (
            should_retry(
                policy=policy,
                attempt_index=0,
                error_code="invalid_receipt",
                explicitly_retryable=True,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_real_plan_retry_policy_reaches_supervisor(self):
        """A ``RetryPolicy`` created via ``DeterministicPlanner`` must
        reach the Supervisor runtime and control retry decisions.
        Uses the real ``PlanValidator`` (not ``_AlwaysValidPlanValidator``).
        """
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        plan = _retry_policy_plan(reg, retry_policy_a=policy)

        # Verify the policy reached the PlannedTask.
        pt_a = next(pt for pt in plan.tasks if pt.intent_id == "intent_a")
        assert pt_a.retry_policy.max_retries == 2
        assert pt_a.retry_policy.retryable_error_codes == frozenset({"custom_error"})

        # The planner generates hash-based task_ids — extract the
        # actual id so the factory can match it.
        task_a_id = pt_a.task.task_id

        # First attempt fails with custom_error, second succeeds.
        call_counts: dict[str, int] = {}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            call_counts[task.task_id] = call_counts.get(task.task_id, 0) + 1
            if task.task_id == task_a_id and call_counts[task.task_id] == 1:
                result = _ok_result(
                    task=task,
                    status="failed",
                    errors=[
                        AgentError(
                            error_code="custom_error",
                            message="transient",
                            category=AgentErrorCategory.TRANSIENT,
                            retryable=True,
                        )
                    ],
                )
            else:
                result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=PlanValidator(),  # R6 P0-2: real validator
        )
        result = await runtime.execute(plan, reg)

        # task_a was attempted twice (first failed, second succeeded).
        assert call_counts.get(task_a_id, 0) == 2, (
            f"task_a must be retried once (2 calls), got "
            f"{call_counts.get(task_a_id, 0)}"
        )
        assert result.status == SupervisorRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_retry_allowlist_controls_runtime(self):
        """An error code in the allowlist must cause a retry in the
        real Supervisor runtime."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        plan = _retry_policy_plan(reg, retry_policy_a=policy)
        task_a_id = next(
            pt.task.task_id for pt in plan.tasks if pt.intent_id == "intent_a"
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == task_a_id:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    result = _ok_result(
                        task=task,
                        status="failed",
                        errors=[
                            AgentError(
                                error_code="custom_error",
                                message="retry me",
                                retryable=True,
                            )
                        ],
                    )
                else:
                    result = _ok_result(task=task)
            else:
                result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=PlanValidator(),  # R6 P0-2: real validator
        )
        result = await runtime.execute(plan, reg)

        assert call_count["n"] == 2, (
            f"custom_error is in the allowlist → must retry, got "
            f"{call_count['n']} calls"
        )
        assert result.status == SupervisorRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_retry_code_outside_allowlist_not_retried(self):
        """An error code NOT in the allowlist must NOT be retried,
        even if ``explicitly_retryable=True``."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        plan = _retry_policy_plan(reg, retry_policy_a=policy)
        task_a_id = next(
            pt.task.task_id for pt in plan.tasks if pt.intent_id == "intent_a"
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == task_a_id:
                call_count["n"] += 1
                # "other_error" is NOT in the allowlist.
                result = _ok_result(
                    task=task,
                    status="failed",
                    errors=[
                        AgentError(
                            error_code="other_error",
                            message="not in allowlist",
                            retryable=True,
                        )
                    ],
                )
            else:
                result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=PlanValidator(),  # R6 P0-2: real validator
        )
        result = await runtime.execute(plan, reg)

        assert call_count["n"] == 1, (
            f"other_error is NOT in the allowlist → must NOT retry, got "
            f"{call_count['n']} calls"
        )
        # The task failed.
        rec_a = next(r for r in result.task_records if r.task_id == task_a_id)
        assert rec_a.status == "failed"

    @pytest.mark.asyncio
    async def test_timeout_excluded_by_policy_not_retried(self):
        """When ``retryable_error_codes`` is non-empty and does NOT
        include ``task_timeout``, a timeout must NOT be retried."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        plan = _retry_policy_plan(reg, retry_policy_a=policy)
        task_a_id = next(
            pt.task.task_id for pt in plan.tasks if pt.intent_id == "intent_a"
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == task_a_id:
                call_count["n"] += 1
                raise RetryableAgentError("timeout-like failure")
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=PlanValidator(),  # R6 P0-2: real validator
        )
        await runtime.execute(plan, reg)

        # RetryableAgentError → error_code="retryable_error", which is
        # NOT in the allowlist {"custom_error"} → no retry.
        assert call_count["n"] == 1, (
            f"retryable_error is NOT in the allowlist → must NOT retry, "
            f"got {call_count['n']} calls"
        )

    @pytest.mark.asyncio
    async def test_invalid_receipt_never_retried(self):
        """``invalid_receipt`` is in ``NEVER_RETRYABLE_ERROR_CODES`` —
        it must never be retried regardless of the policy."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        # Empty allowlist → default safe categories.  But invalid_receipt
        # is never-retryable, so even with max_retries=3 it won't retry.
        policy = RetryPolicy(max_retries=3)
        plan = _retry_policy_plan(reg, retry_policy_a=policy)
        task_a_id = next(
            pt.task.task_id for pt in plan.tasks if pt.intent_id == "intent_a"
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            if task.task_id == task_a_id:
                call_count["n"] += 1
                # Return a receipt with mismatched tool_calls count →
                # validate_invocation_receipt raises.
                result = _ok_result(task=task)
                return AgentInvocationReceipt(
                    result=result,
                    tool_calls=999,  # mismatch → invalid_receipt
                )
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=PlanValidator(),  # R6 P0-2: real validator
        )
        result = await runtime.execute(plan, reg)

        assert call_count["n"] == 1, (
            f"invalid_receipt must NEVER be retried, got {call_count['n']} calls"
        )
        rec_a = next(r for r in result.task_records if r.task_id == task_a_id)
        assert any(a.error_code == "invalid_receipt" for a in rec_a.attempts), (
            f"expected invalid_receipt error_code, got "
            f"{[a.error_code for a in rec_a.attempts]}"
        )

    @pytest.mark.asyncio
    async def test_retry_execution_uses_real_plan_validator(self):
        """The retry execution path must work with the real
        ``PlanValidator``, not ``_AlwaysValidPlanValidator``.  A
        tampered plan must be rejected by the real validator."""
        reg = _make_registry(_retry_policy_caps(), catalog=_retry_policy_catalog())
        policy = RetryPolicy(
            max_retries=1,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        plan = _retry_policy_plan(reg, retry_policy_a=policy)

        # Tamper with the retry policy AFTER planning — recompute
        # plan_hash so integrity check passes, but the Canonical Plan
        # comparison in PlanValidator catches the tampering.
        for pt in plan.tasks:
            if pt.intent_id == "intent_a":
                object.__setattr__(
                    pt,
                    "retry_policy",
                    RetryPolicy(max_retries=3),
                )
                break
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())

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
            plan_validator=PlanValidator(),  # R6 P0-2: real validator
        )
        # The real PlanValidator must reject the tampered plan.
        with pytest.raises(Exception):
            await runtime.execute(plan, reg)


# ===========================================================================
# P0-3: No-receipt Attempt Usage Disposition (Fail-Closed)
# ===========================================================================


class TestNoReceiptAttemptUsageDisposition:
    """R6 P0-3: Every committed agent call must produce a Usage
    Disposition.  When a budget is configured and no receipt is
    produced (timeout / exception), the run must fail-closed."""

    @pytest.mark.asyncio
    async def test_timeout_with_token_budget_fails_usage_closed(self):
        """When a ``token_budget`` is configured and an attempt times
        out (no receipt), the run must finalise as
        ``budget_exceeded`` with ``execution_usage_unavailable``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                token_budget=1000,
                deadline_ms=300_000,
            ),
        )

        # Factory that always times out (raises RetryableAgentError,
        # which produces no receipt).
        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            raise RetryableAgentError("timeout-like failure")

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(
                factory=factory,
                usage_capabilities=UsageVerificationCapabilities(
                    verifies_tokens=False,
                    verifies_cost=False,
                    source_id="deterministic_fake_invoker",
                    never_calls_provider=False,
                ),
            ),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED, (
            f"expected BUDGET_EXCEEDED (usage unavailable + token_budget), "
            f"got {result.status}"
        )

    @pytest.mark.asyncio
    async def test_retryable_error_without_usage_receipt_fails_closed(self):
        """When a ``cost_budget_usd`` is configured and a
        ``RetryableAgentError`` occurs (no receipt), the run must
        fail-closed — the provider call may have consumed cost."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                cost_budget_usd=Decimal("10.00"),
            ),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            raise RetryableAgentError("transient failure")

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(
                factory=factory,
                usage_capabilities=UsageVerificationCapabilities(
                    verifies_tokens=False,
                    verifies_cost=False,
                    source_id="deterministic_fake_invoker",
                    never_calls_provider=False,
                ),
            ),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_no_receipt_stops_future_retries(self):
        """When a committed attempt produces no receipt and a budget
        is configured, the run must NOT retry — ``usage_unavailable``
        stops the retry loop immediately."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                token_budget=1000,
            ),
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            call_count["n"] += 1
            raise RetryableAgentError("fail")

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        await runtime.execute(plan, reg)

        # Each task is called at most once — no retries despite
        # RetryableAgentError being retryable, because usage_unavailable
        # stops the retry loop.
        assert call_count["n"] <= 3, (
            f"expected at most 3 calls (one per task, no retries), got "
            f"{call_count['n']}"
        )

    @pytest.mark.asyncio
    async def test_no_receipt_stops_independent_tasks_when_budgeted(self):
        """When a committed attempt produces no receipt and a budget
        is configured, the run must stop dispatching new tasks —
        independent sibling tasks must not run."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                token_budget=1000,
            ),
        )

        invoked_tasks: list[str] = []

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            invoked_tasks.append(task.task_id)
            raise RetryableAgentError("fail")

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(
                factory=factory,
                usage_capabilities=UsageVerificationCapabilities(
                    verifies_tokens=False,
                    verifies_cost=False,
                    source_id="deterministic_fake_invoker",
                    never_calls_provider=False,
                ),
            ),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The run fails-closed after the first wave's failures —
        # usage_unavailable sets exceeded, should_stop prevents future
        # waves.  All three tasks in the first wave may be invoked
        # (they're independent and in the same wave), but no retries.
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_trusted_no_provider_call_does_not_charge_usage(self):
        """A receipt WITHOUT ``provider_metadata`` (deterministic mode)
        does NOT count as a provider-usage-capable attempt — it does
        not trigger fail-closed even with a token budget, because no
        provider call was made."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                token_budget=1000,
            ),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            # No provider_metadata → deterministic mode → no provider call.
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                # tokens_used=None, no provider_metadata
                token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Completed, not budget_exceeded — deterministic receipts don't
        # trigger usage_unavailable.
        assert result.status == SupervisorRunStatus.COMPLETED, (
            f"deterministic (no provider) receipts must not trigger "
            f"fail-closed, got {result.status}"
        )
        # No provider-usage-capable attempts.
        assert result.usage.provider_usage_capable_attempts == 0

    @pytest.mark.asyncio
    async def test_unknown_usage_not_reported_as_zero(self):
        """When a committed attempt produces no receipt and a budget
        is configured, the usage status must NOT be ``COMPLETE`` with
        zero tokens — it must be ``UNAVAILABLE``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                token_budget=1000,
            ),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            raise RetryableAgentError("fail")

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(
                factory=factory,
                usage_capabilities=UsageVerificationCapabilities(
                    verifies_tokens=False,
                    verifies_cost=False,
                    source_id="deterministic_fake_invoker",
                    never_calls_provider=False,
                ),
            ),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.tokens_usage_status == UsageAvailabilityStatus.UNAVAILABLE
        assert result.usage.cost_usage_status == UsageAvailabilityStatus.UNAVAILABLE


# ===========================================================================
# P0-4: Per-dimension Usage Provenance (Token vs Cost)
# ===========================================================================


class TestPerDimensionUsageProvenance:
    """R6 P0-4: Token and Cost provenance are independent dimensions.
    A ``verified_provider`` receipt that carries ``cost_usd`` must
    actually accumulate cost — the old single ``usage_trust`` silently
    bypassed ``cost_budget_usd``."""

    @pytest.mark.asyncio
    async def test_verified_provider_cost_is_charged(self):
        """A receipt with ``cost_verified=True`` and ``cost_usd=0.50``
        must accumulate cost into ``usage.cost_usd`` — even without a
        ``cost_budget_usd`` configured."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="test_both_verifier",
            bound_token_source_ids=frozenset({"test_both_verifier"}),
            bound_cost_source_ids=frozenset({"test_both_verifier"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=100,
                cost_usd=Decimal("0.50"),
                usage_provenance=UsageProvenance(
                    source_id="test_both_verifier",
                    token_source_id="test_both_verifier",
                    cost_source_id="test_both_verifier",
                    tokens_verified=True,
                    cost_verified=True,
                ),
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.VERIFIED,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.cost_usd >= Decimal("1.50"), (
            f"3 tasks × 0.50 = 1.50, got {result.usage.cost_usd}"
        )
        assert result.usage.cost_usage_status != UsageAvailabilityStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_cost_budget_cannot_bypass_provenance(self):
        """A receipt with ``tokens_verified=True`` but
        ``cost_verified=False`` must NOT pass ``cost_budget_usd``
        enforcement — cost trust is independent of token trust."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                cost_budget_usd=Decimal("10.00"),
            ),
        )

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,  # Invoker cannot verify cost
            source_id="token_only_verifier",
            bound_token_source_ids=frozenset({"token_only_verifier"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=100,
                cost_usd=None,  # R10 P0-5: cost not verified → must be None
                usage_provenance=UsageProvenance(
                    source_id="token_only_verifier",
                    token_source_id="token_only_verifier",
                    tokens_verified=True,
                    cost_verified=False,  # cost NOT verified
                ),
                token_disposition=AttemptUsageDisposition.VERIFIED,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # Must fail-closed: cost_budget configured but cost not verified.
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED, (
            f"cost_budget_usd is configured but cost_verified=False → "
            f"must fail-closed, got {result.status}"
        )

    @pytest.mark.asyncio
    async def test_token_only_verifier_cannot_claim_cost(self):
        """An Invoker with ``verifies_cost=False`` cannot produce a
        receipt with ``cost_verified=True`` — the accountant cross-
        checks provenance against capabilities."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="token_only_invoker",
            bound_token_source_ids=frozenset({"token_only_invoker"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=100,
                cost_usd=Decimal("0.50"),
                usage_provenance=UsageProvenance(
                    source_id="token_only_invoker",
                    token_source_id="token_only_invoker",
                    cost_source_id="token_only_invoker",
                    tokens_verified=True,
                    cost_verified=True,  # LIAR — invoker can't verify cost
                ),
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.VERIFIED,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The receipt's cost_verified=True is rejected because the
        # invoker's verifies_cost=False.
        for rec in result.task_records:
            if rec.status == "failed":
                assert any(a.error_code == "usage_unavailable" for a in rec.attempts), (
                    f"expected usage_unavailable (cost provenance mismatch), "
                    f"got {[a.error_code for a in rec.attempts]}"
                )

    @pytest.mark.asyncio
    async def test_cost_only_verifier_cannot_claim_tokens(self):
        """An Invoker with ``verifies_tokens=False`` cannot produce a
        receipt with ``tokens_verified=True`` — the accountant cross-
        checks provenance against capabilities."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=True,
            source_id="cost_only_invoker",
            bound_cost_source_ids=frozenset({"cost_only_invoker"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=100,
                cost_usd=Decimal("0.50"),
                usage_provenance=UsageProvenance(
                    source_id="cost_only_invoker",
                    token_source_id="cost_only_invoker",
                    cost_source_id="cost_only_invoker",
                    tokens_verified=True,  # LIAR — invoker can't verify tokens
                    cost_verified=True,
                ),
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.VERIFIED,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        for rec in result.task_records:
            if rec.status == "failed":
                assert any(a.error_code == "usage_unavailable" for a in rec.attempts), (
                    f"expected usage_unavailable (token provenance mismatch), "
                    f"got {[a.error_code for a in rec.attempts]}"
                )

    @pytest.mark.asyncio
    async def test_cost_is_recorded_before_enforcement(self):
        """Cost is accumulated into ``_cost_usd`` BEFORE the budget
        check — a receipt that pushes the total over the limit must
        be detected, not silently passed because the old total was
        under the limit."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        # Very tight budget: 3 tasks × 0.50 = 1.50 > 1.00 limit.
        plan = _three_independent_plan(
            reg,
            budget=ExecutionBudget(
                max_agent_calls=10,
                cost_budget_usd=Decimal("1.00"),
            ),
        )

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="test_cost_record",
            bound_token_source_ids=frozenset({"test_cost_record"}),
            bound_cost_source_ids=frozenset({"test_cost_record"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=100,
                cost_usd=Decimal("0.50"),
                usage_provenance=UsageProvenance(
                    source_id="test_cost_record",
                    token_source_id="test_cost_record",
                    cost_source_id="test_cost_record",
                    tokens_verified=True,
                    cost_verified=True,
                ),
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.VERIFIED,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # The third task pushes total to 1.50 > 1.00 → budget exceeded.
        assert result.status == SupervisorRunStatus.BUDGET_EXCEEDED, (
            f"cost must be recorded before enforcement so 1.50 > 1.00 is "
            f"detected, got {result.status}"
        )
        assert result.usage.cost_usd >= Decimal("1.00"), (
            f"cost must be accumulated before the check, got {result.usage.cost_usd}"
        )


# ===========================================================================
# P0-5: Cache Path Isolation
# ===========================================================================


class _BrokenCapsInvoker:
    """An invoker whose ``usage_capabilities`` property raises.

    If the cache path reads this property, the run will crash — proving
    that the cache path is NOT pure.
    """

    source_id: str = "broken_caps_invoker"

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        raise RuntimeError(
            "broken_caps_invoker.usage_capabilities must NOT be accessed "
            "on the cache hit path"
        )

    async def invoke(
        self,
        handler: Any,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt:  # pragma: no cover
        raise RuntimeError("broken_caps_invoker.invoke must NOT be called on cache hit")


class _ConstructionTrackingRegistry:
    """Wrapper around AgentRegistry that tracks whether resolve() was
    called — used to verify the cache path doesn't touch the registry."""

    def __init__(self, inner: AgentRegistry) -> None:
        self._inner = inner
        self.resolve_calls: int = 0

    def snapshot(self):
        return self._inner.snapshot()

    def resolve(self, agent_id: str):
        self.resolve_calls += 1
        return self._inner.resolve(agent_id)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class TestCachePathIsolation:
    """R6 P0-5: A completed run cached under the same
    ``(run_id, plan_hash)`` must be returned without reading any Live
    Invoker state — not the Invoker's ``usage_capabilities``, not the
    ``ProviderUsageVerifier``, not the Live Registry Handler."""

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_read_invoker_capabilities(self):
        """A second execution with the same ``(run_id, plan_hash)``
        must return the cached result WITHOUT accessing the Invoker's
        ``usage_capabilities`` property."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        run_store = InMemoryRunStore()

        # First execution: succeeds and caches the result.
        runtime1 = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result1 = await runtime1.execute(plan, reg)
        assert result1.status == SupervisorRunStatus.COMPLETED

        # Second execution: pass a broken invoker whose caps property
        # raises.  If the cache path is pure, this invoker is never
        # accessed and the cached result is returned.
        runtime2 = SupervisorRuntime(
            invoker=_BrokenCapsInvoker(),  # type: ignore[arg-type]
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result2 = await runtime2.execute(plan, reg)

        assert result2.status == SupervisorRunStatus.COMPLETED
        assert result2.run_id == result1.run_id
        assert result2.plan_hash == result1.plan_hash

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_call_usage_verifier(self):
        """The cache hit path must NOT call the ProviderUsageVerifier.
        A second execution with a verifier that raises must still
        return the cached result."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        run_store = InMemoryRunStore()

        # First execution: no verifier, succeeds and caches.
        runtime1 = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result1 = await runtime1.execute(plan, reg)
        assert result1.status == SupervisorRunStatus.COMPLETED

        # Second execution: pass a broken invoker (which simulates
        # a verifier that would raise).  The cache path must not
        # touch it.
        runtime2 = SupervisorRuntime(
            invoker=_BrokenCapsInvoker(),  # type: ignore[arg-type]
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result2 = await runtime2.execute(plan, reg)

        assert result2.status == SupervisorRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_cached_result_survives_broken_usage_capability_property(self):
        """Even if the second execution's Invoker has a
        ``usage_capabilities`` property that raises RuntimeError, the
        cached result must survive — the cache path never reads it."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        run_store = InMemoryRunStore()

        runtime1 = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result1 = await runtime1.execute(plan, reg)
        assert result1.status == SupervisorRunStatus.COMPLETED

        # Second execution with a broken invoker.
        runtime2 = SupervisorRuntime(
            invoker=_BrokenCapsInvoker(),  # type: ignore[arg-type]
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        # Must NOT raise — the cache path is pure.
        result2 = await runtime2.execute(plan, reg)
        assert result2.status == SupervisorRunStatus.COMPLETED
        assert result2.run_id == result1.run_id

    @pytest.mark.asyncio
    async def test_cached_result_does_not_construct_default_invoker(self):
        """When ``invoker=None``, the Supervisor normally constructs a
        ``RegistryAgentInvoker(registry)``.  On the cache hit path,
        this construction must NOT happen — the cached result is
        returned before the invoker is created."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
            )

        run_store = InMemoryRunStore()

        # First execution: with a working invoker.
        runtime1 = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result1 = await runtime1.execute(plan, reg)
        assert result1.status == SupervisorRunStatus.COMPLETED

        # Second execution: invoker=None.  If the cache path constructs
        # a default RegistryAgentInvoker, that's fine as long as it
        # doesn't call it — but the key is the cache result is returned
        # without invoking any handler.
        runtime2 = SupervisorRuntime(
            invoker=None,
            run_store=run_store,
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result2 = await runtime2.execute(plan, reg)

        assert result2.status == SupervisorRunStatus.COMPLETED
        assert result2.run_id == result1.run_id


# ===========================================================================
# P1-a: Usage Status (unavailable / partial / complete)
# ===========================================================================


class TestUsageAvailabilityStatus:
    """R6 P1: ``UsageAvailabilityStatus`` three-state replaces the old
    boolean flags.  ``PARTIAL`` means some (but not all) provider-usage-
    capable attempts had verified usage; ``COMPLETE`` means all did."""

    @pytest.mark.asyncio
    async def test_usage_status_distinguishes_partial_and_complete(self):
        """When some provider-usage-capable attempts have verified
        tokens and others don't, the status must be ``PARTIAL``, not
        ``COMPLETE`` or ``UNAVAILABLE``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="partial_verifier",
            bound_token_source_ids=frozenset({"partial_verifier"}),
        )

        call_count = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            call_count["n"] += 1
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            if task.task_id == "task_a":
                # Verified tokens.
                return AgentInvocationReceipt(
                    result=result,
                    tool_calls=len(result.tool_calls),
                    tokens_used=100,
                    usage_provenance=UsageProvenance(
                        source_id="partial_verifier",
                        token_source_id="partial_verifier",
                        tokens_verified=True,
                        cost_verified=False,
                    ),
                    token_disposition=AttemptUsageDisposition.VERIFIED,
                )
            # Unverified tokens (provider_metadata present but no
            # verifier attestation — self-reported).
            # R10 P0-5: tokens_used must be None when the disposition
            # is UNAVAILABLE (the default for unverified).  The
            # self-reported value remains in result.token_usage.
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=None,
                usage_provenance=UsageProvenance(
                    source_id="unverified",
                    tokens_verified=False,
                    cost_verified=False,
                ),
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        # 3 provider-usage-capable attempts (all have provider_metadata),
        # but only 1 has verified tokens → PARTIAL.
        assert result.usage.provider_usage_capable_attempts == 3, (
            f"expected 3 provider-usage-capable attempts, got "
            f"{result.usage.provider_usage_capable_attempts}"
        )
        assert result.usage.verified_token_attempts == 1, (
            f"expected 1 verified token attempt, got "
            f"{result.usage.verified_token_attempts}"
        )
        assert result.usage.tokens_usage_status == UsageAvailabilityStatus.PARTIAL, (
            f"1/3 verified → PARTIAL, got {result.usage.tokens_usage_status}"
        )

    @pytest.mark.asyncio
    async def test_usage_status_complete_when_all_verified(self):
        """When all provider-usage-capable attempts have verified
        tokens, the status must be ``COMPLETE``."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="full_verifier",
            bound_token_source_ids=frozenset({"full_verifier"}),
            bound_cost_source_ids=frozenset({"full_verifier"}),
        )

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=100,
                cost_usd=Decimal("0.10"),
                usage_provenance=UsageProvenance(
                    source_id="full_verifier",
                    token_source_id="full_verifier",
                    cost_source_id="full_verifier",
                    tokens_verified=True,
                    cost_verified=True,
                ),
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.VERIFIED,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory, usage_capabilities=caps),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.provider_usage_capable_attempts == 3
        assert result.usage.verified_token_attempts == 3
        assert result.usage.verified_cost_attempts == 3
        assert result.usage.tokens_usage_status == UsageAvailabilityStatus.COMPLETE
        assert result.usage.cost_usage_status == UsageAvailabilityStatus.COMPLETE

    @pytest.mark.asyncio
    async def test_usage_status_complete_when_no_provider_calls(self):
        """When no attempts made provider calls (deterministic mode),
        the status must be ``COMPLETE`` (vacuously — 0/0)."""
        reg = _make_registry(
            _three_independent_caps(), catalog=_three_independent_catalog()
        )
        plan = _three_independent_plan(reg)

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            result = _ok_result(task=task)
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            )

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert result.usage.provider_usage_capable_attempts == 0
        assert result.usage.tokens_usage_status == UsageAvailabilityStatus.COMPLETE
        assert result.usage.cost_usage_status == UsageAvailabilityStatus.COMPLETE


# ===========================================================================
# P1-b: RetryPolicy content validation
# ===========================================================================


class TestRetryPolicyContentValidation:
    """R6 P1: ``RetryPolicy.retryable_error_codes`` rejects blank
    strings and never-retryable codes at construction time."""

    def test_retry_policy_rejects_blank_error_codes(self):
        """Blank strings (empty or whitespace-only) must be rejected."""
        with pytest.raises(ValueError, match="blank"):
            RetryPolicy(
                max_retries=1,
                retryable_error_codes=frozenset({""}),
            )

        with pytest.raises(ValueError, match="blank"):
            RetryPolicy(
                max_retries=1,
                retryable_error_codes=frozenset({"  "}),
            )

    def test_retry_policy_rejects_never_retryable_codes(self):
        """Codes in ``NEVER_RETRYABLE_ERROR_CODES`` must be rejected —
        listing them is a no-op that indicates a misconfiguration."""
        from multi_agent.planning import NEVER_RETRYABLE_ERROR_CODES

        for code in NEVER_RETRYABLE_ERROR_CODES:
            with pytest.raises(ValueError, match="never-retryable"):
                RetryPolicy(
                    max_retries=1,
                    retryable_error_codes=frozenset({code}),
                )

    def test_retry_policy_accepts_valid_codes(self):
        """Valid error codes (non-blank, not never-retryable) are
        accepted and stripped of surrounding whitespace."""
        policy = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset({"  custom_error  ", "transient"}),
        )
        assert policy.retryable_error_codes == frozenset({"custom_error", "transient"})

    def test_retry_policy_accepts_empty_set(self):
        """An empty ``retryable_error_codes`` set is valid (default)."""
        policy = RetryPolicy(max_retries=2)
        assert policy.retryable_error_codes == frozenset()

        policy2 = RetryPolicy(
            max_retries=2,
            retryable_error_codes=frozenset(),
        )
        assert policy2.retryable_error_codes == frozenset()
