"""Phase 4 R9 regression tests.

Direct counter-examples for the five P0 concerns and three P1 cleanup
items identified in the Phase 4 R8 review, addressed by R9:

* **P0-1 / Section 1** — Mixed-dimension Accounting: a record with
  Token=VERIFIED + Cost=UNAVAILABLE must be committed as-is.  The
  verified dimension's usage is preserved; fail-closed happens AFTER
  the commit, not before.

* **P0-2 / Section 2** — Unified Invocation Outcome: both success and
  failure paths produce an :class:`AgentInvocationOutcome`.  Timeout
  and exception paths do NOT default ``observed_tool_calls=0`` —
  unknown counts are ``None``, triggering ``tool_usage_unavailable``
  fail-closed.

* **P0-3 / Section 3** — Per-attempt No-provider-call: the static
  ``never_calls_provider`` capability is used for VALIDATION only,
  not INFERENCE.  ``NO_PROVIDER_CALL`` on the no-receipt path requires
  an explicit :class:`AgentInvocationOutcome` from the Invoker.

* **P0-4 / Section 4+5** — Strict Usage Audit Contracts: shared
  Usage types live in :mod:`multi_agent.usage` with strict Pydantic
  types (no ``Any``).  :class:`AttemptUsageRecord` enforces
  per-dimension invariants.

* **P0-5 / Section 6** — Source Binding: no cross-dimension fallback.
  ``tokens_verified=True`` requires ``token_source_id``; the Accountant
  does not fall back to legacy ``source_id``.

* **P1-1 / Section 7** — Legacy Trust: ANY simultaneous provision of
  ``usage_trust`` and ``usage_provenance`` is a ``ValidationError``,
  even when the derived trust matches.

* **P1-2 / Section 8** — VerifiedUsage Source: per-dimension source
  fields removed (Choice A).  The Invoker uses the Verifier's frozen
  ``source_id``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from multi_agent.complexity_gate import ComplexityDecision
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
    TaskAttemptRecord,
)
from multi_agent.invocation import (
    AgentInvocationFailure,
    AgentInvocationOutcome,
    AgentInvocationReceipt,
    AttemptUsageDisposition,
    AttemptUsageRecord,
    UsageProvenance,
    UsageVerificationCapabilities,
    VerifiedUsage,
)
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlanValidationReport,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    compute_request_hash,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.supervisor import _BudgetAccountant


# ---------------------------------------------------------------------------
# Shared helpers (copied from test_supervisor_r8.py)
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


def _ok_result(
    *,
    task: AgentTask,
    status: str = "completed",
    provider_metadata: ProviderMetadata | None = None,
    token_usage: TokenUsage | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
    errors: list | None = None,
) -> AgentResult:
    return AgentResult(
        result_id=f"r-{task.task_id}",
        task_id=task.task_id,
        agent_id=task.agent_id,
        agent_version="1.0.0",
        tenant_id=task.tenant_id,
        status=status,  # type: ignore[arg-type]
        confidence=1.0,
        duration_ms=0.0,
        evidence=[],
        action_proposals=[],
        errors=errors or [],
        token_usage=token_usage or TokenUsage(),
        provider_metadata=provider_metadata,
        tool_calls=tool_calls or [],
        completed_at=_FIXED_TS,
    )


def _provider_meta() -> ProviderMetadata:
    return ProviderMetadata(
        provider="openai",
        chat_model="gpt-4",
        embedding_model="text-embedding-3-small",
        ai_mode="live",
    )


def _make_task(
    task_id: str = "task_a",
    agent_id: str = "agent_a",
    timeout_ms: int = 10_000,
) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        agent_id=agent_id,
        task_type="root_task",
        objective="test",
        tenant_id="t-001",
        timeout_ms=timeout_ms,
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
    reg = AgentRegistry(tool_catalog=catalog or _three_independent_catalog())
    for cap in caps:
        handler = (handlers or {}).get(cap.agent_id, _NoopHandler())
        reg.register(cap, handler)
    return reg


class _AlwaysValidPlanValidator:
    """PlanValidator stub that always returns ``valid=True``."""

    def validate(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> PlanValidationReport:
        return PlanValidationReport(valid=True, issues=[])


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


# Capability constants reused across tests.

_TOKEN_VERIFIER_CAPS = UsageVerificationCapabilities(
    verifies_tokens=True,
    verifies_cost=False,
    source_id="token_verifier",
    never_calls_provider=False,
    bound_token_source_ids=frozenset({"token_verifier"}),
)

_COST_VERIFIER_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=True,
    source_id="cost_verifier",
    never_calls_provider=False,
    bound_cost_source_ids=frozenset({"cost_verifier"}),
)

_DETERMINISTIC_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="deterministic_invoker",
    never_calls_provider=True,
    bound_source_ids=frozenset({"deterministic_invoker"}),
)


# ===========================================================================
# Group 1: Mixed-dimension Accounting (P0-1 / Section 1)
# ===========================================================================


class TestMixedDimensionAccounting:
    """R9 Section 1: a dimension UNAVAILABLE must NOT erase another
    dimension's VERIFIED usage.  The record is committed as-is;
    fail-closed happens AFTER the commit."""

    def test_verified_token_preserved_when_cost_unavailable(self):
        """Token=VERIFIED + Cost=UNAVAILABLE with a cost budget:
        the record is committed with tokens_used=100, cost_usd=None.
        ``exceeded`` is set AFTER the commit, not before."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
            ),
            tool_calls=0,
            tokens_used=100,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="token_verifier",
                token_source_id="token_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        # R9 Section 1: commit-then-check — no exception raised.
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_TOKEN_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # The verified token usage is PRESERVED.
        assert accountant._tokens_used == 100
        assert accountant._verified_token_attempts == 1
        # Cost is UNAVAILABLE — fail-closed is triggered.
        assert accountant.exceeded
        assert accountant.usage_unavailable
        assert accountant.exceeded_reason == "execution_usage_unavailable"
        # The record is committed with mixed dispositions.
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.VERIFIED
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.tokens_used == 100
        assert record.cost_usd is None

    def test_verified_cost_preserved_when_token_unavailable(self):
        """Token=UNAVAILABLE + Cost=VERIFIED with a token budget:
        the record is committed with cost_usd=0.50, tokens_used=None."""
        budget = ExecutionBudget(token_budget=1000)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                source_id="cost_verifier",
                cost_source_id="cost_verifier",
                tokens_verified=False,
                cost_verified=True,
            ),
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_COST_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # The verified cost usage is PRESERVED.
        assert accountant._cost_usd == Decimal("0.50")
        assert accountant._verified_cost_attempts == 1
        # Token is UNAVAILABLE — fail-closed is triggered.
        assert accountant.exceeded
        assert accountant.usage_unavailable
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.VERIFIED
        assert record.cost_usd == Decimal("0.50")
        assert record.tokens_used is None

    def test_mixed_record_committed_before_fail_closed(self):
        """The mixed record is in ``_attempt_records`` BEFORE
        ``exceeded`` is set — the audit trail preserves the verified
        dimension even when the run is marked budget_exceeded."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
            ),
            tool_calls=0,
            tokens_used=100,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="token_verifier",
                token_source_id="token_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_TOKEN_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # Record is committed (audit trail preserved).
        assert len(accountant._attempt_records) == 1
        # Fail-closed is set AFTER the commit.
        assert accountant.exceeded
        # The audit trail shows the verified token usage.
        usage = accountant.usage
        assert usage.tokens_used == 100
        assert usage.tokens_usage_available is True
        assert usage.cost_usage_available is False

    def test_mixed_usage_run_is_budget_exceeded_but_auditable(self):
        """A run with mixed VERIFIED/UNAVAILABLE usage is
        ``budget_exceeded`` but the ``attempt_usage_records`` list
        preserves the verified token count for external audit."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
            ),
            tool_calls=0,
            tokens_used=100,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="token_verifier",
                token_source_id="token_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_TOKEN_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        usage = accountant.usage
        assert len(usage.attempt_usage_records) == 1
        rec = usage.attempt_usage_records[0]
        assert rec.token_disposition == AttemptUsageDisposition.VERIFIED
        assert rec.tokens_used == 100
        assert rec.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert rec.cost_usd is None

    def test_one_dimension_failure_does_not_erase_other_usage(self):
        """When Cost=UNAVAILABLE and Token=VERIFIED, the Token's
        verified count is in ``tokens_used`` and the Token's
        applicable-attempt counter is incremented.  The Cost dimension
        being UNAVAILABLE does not zero out the Token dimension."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
            ),
            tool_calls=0,
            tokens_used=100,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="token_verifier",
                token_source_id="token_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_TOKEN_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # Token dimension is NOT erased by Cost failure.
        assert accountant._tokens_used == 100
        assert accountant._token_usage_applicable_attempts == 1
        assert accountant._verified_token_attempts == 1
        # Cost dimension is UNAVAILABLE.
        assert accountant._cost_usage_applicable_attempts == 1
        assert accountant._verified_cost_attempts == 0


# ===========================================================================
# Group 2: Unified Invocation Outcome (P0-2 / Section 2)
# ===========================================================================


class TestUnifiedInvocationOutcome:
    """R9 Section 2: both success and failure paths produce an
    :class:`AgentInvocationOutcome`.  Timeout/exception paths do NOT
    default ``observed_tool_calls=0``."""

    def test_agent_invocation_outcome_fields(self):
        """``AgentInvocationOutcome`` carries all R9 Section 2 fields."""
        outcome = AgentInvocationOutcome(
            result=None,
            error_code="task_timeout",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        assert outcome.result is None
        assert outcome.error_code == "task_timeout"
        assert outcome.observed_tool_calls is None
        assert outcome.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert outcome.cost_disposition == AttemptUsageDisposition.UNAVAILABLE

    def test_agent_invocation_failure_carries_outcome(self):
        """``AgentInvocationFailure`` carries a partial Outcome that
        the Supervisor uses to build the AttemptUsageRecord."""
        outcome = AgentInvocationOutcome(
            error_code="handler_exception",
            observed_tool_calls=3,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        failure = AgentInvocationFailure(outcome)
        assert failure.outcome is outcome
        assert failure.outcome.observed_tool_calls == 3
        assert "handler_exception" in str(failure)

    def test_unknown_tool_usage_fails_closed(self):
        """When ``record_observed_tool_calls(None)`` is called, the
        accountant marks ``tool_usage_unavailable=True`` and
        ``exceeded=True`` — the Runtime cannot safely allow retries or
        new tasks when it cannot account for tool calls."""
        budget = ExecutionBudget(max_tool_calls=10)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_observed_tool_calls(None)
        assert accountant.tool_usage_unavailable is True
        assert accountant.exceeded
        assert accountant.exceeded_reason == "tool_usage_unavailable"

    def test_timeout_after_tools_does_not_report_zero(self):
        """When a timeout occurs (no receipt), ``observed_tool_calls``
        is ``None`` (unknown), NOT ``0``.  The accountant fails closed
        via ``tool_usage_unavailable``."""
        budget = ExecutionBudget(max_tool_calls=10)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        # Simulate the timeout path: no Outcome, no receipt.
        # The Supervisor calls record_observed_tool_calls(None).
        accountant.record_observed_tool_calls(None)
        # Tool calls are NOT reported as 0 — they're unknown.
        assert accountant.tool_usage_unavailable is True
        assert accountant.exceeded
        # The tool_calls counter was NOT incremented (we don't know
        # how many calls were made before the timeout).
        assert accountant.tool_calls == 0

    def test_exception_after_tools_cannot_bypass_tool_budget(self):
        """When an ``AgentInvocationFailure`` carries
        ``observed_tool_calls=3``, the accountant charges those 3
        calls — they cannot be bypassed by the exception."""
        budget = ExecutionBudget(max_tool_calls=10)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        outcome = AgentInvocationOutcome(
            error_code="handler_error",
            observed_tool_calls=3,
        )
        accountant.record_observed_tool_calls(outcome.observed_tool_calls)
        assert accountant.tool_calls == 3
        assert not accountant.tool_usage_unavailable

    def test_observed_tool_calls_zero_is_valid_for_deterministic(self):
        """``observed_tool_calls=0`` is valid when the Invoker
        authoritatively attests zero calls (deterministic path).
        This does NOT trigger fail-closed — ``0`` is a concrete
        count, not unknown."""
        budget = ExecutionBudget(max_tool_calls=10)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_observed_tool_calls(0)
        assert not accountant.tool_usage_unavailable
        assert not accountant.exceeded
        assert accountant.tool_calls == 0


# ===========================================================================
# Group 3: Per-attempt No-provider-call (P0-3 / Section 3)
# ===========================================================================


class TestPerAttemptNoProviderCall:
    """R9 Section 3: ``NO_PROVIDER_CALL`` on the no-receipt path
    requires an explicit :class:`AgentInvocationOutcome`.  The static
    ``never_calls_provider`` capability is for VALIDATION only, not
    INFERENCE."""

    def test_capability_alone_does_not_attest_failed_attempt(self):
        """Calling ``record_usage_unavailable`` with
        ``invoker_capabilities`` that have ``never_calls_provider=True``
        but NO explicit Outcome must produce ``UNAVAILABLE``
        dispositions — the capability alone cannot attest."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=False,
            source_id="hybrid_invoker",
            never_calls_provider=True,
        )
        # No Outcome — both dimensions must be UNAVAILABLE.
        accountant.record_usage_unavailable(
            task_id="t1",
            attempt=0,
            invoker_capabilities=caps,
        )
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE

    def test_hybrid_failure_is_not_no_provider_call(self):
        """A hybrid Invoker that fails without a receipt and without
        an explicit Outcome must produce ``UNAVAILABLE`` — NOT
        ``NO_PROVIDER_CALL``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_usage_unavailable(
            task_id="t1",
            attempt=0,
        )
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE

    def test_deterministic_failure_explicitly_attests_no_call(self):
        """A deterministic Invoker that raises
        :class:`AgentInvocationFailure` with an explicit
        ``NO_PROVIDER_CALL`` Outcome produces a record with
        ``NO_PROVIDER_CALL`` dispositions — the Outcome is the
        authoritative attestation."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        outcome = AgentInvocationOutcome(
            error_code="deterministic_skip",
            observed_tool_calls=0,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        accountant.record_usage_unavailable(
            task_id="t1",
            attempt=0,
            outcome=outcome,
        )
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL
        assert record.cost_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL
        assert not accountant.exceeded
        assert not accountant.usage_unavailable

    def test_no_receipt_without_outcome_never_becomes_no_provider_call(self):
        """``record_usage_unavailable`` without an Outcome ALWAYS
        produces ``UNAVAILABLE`` — never ``NO_PROVIDER_CALL``,
        regardless of the Invoker's capabilities."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        # Pass capabilities with never_calls_provider=True but NO Outcome.
        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=False,
            source_id="deterministic_invoker",
            never_calls_provider=True,
        )
        accountant.record_usage_unavailable(
            task_id="t1",
            attempt=0,
            invoker_capabilities=caps,
        )
        record = accountant.last_attempt_record
        assert record is not None
        # UNAVAILABLE, NOT NO_PROVIDER_CALL — no explicit Outcome.
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE

    def test_no_receipt_requires_attempt_outcome(self):
        """When ``record_usage_unavailable`` is called with an Outcome
        that has ``NO_PROVIDER_CALL``, the record reflects it.  When
        called WITHOUT an Outcome, it defaults to ``UNAVAILABLE``."""
        budget = ExecutionBudget()
        accountant_a = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant_b = _BudgetAccountant(budget, start_monotonic=time.monotonic())

        # With Outcome → NO_PROVIDER_CALL
        outcome = AgentInvocationOutcome(
            error_code="skip",
            observed_tool_calls=0,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        accountant_a.record_usage_unavailable(task_id="t1", attempt=0, outcome=outcome)
        rec_a = accountant_a.last_attempt_record
        assert rec_a is not None
        assert rec_a.token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL

        # Without Outcome → UNAVAILABLE
        accountant_b.record_usage_unavailable(task_id="t1", attempt=0)
        rec_b = accountant_b.last_attempt_record
        assert rec_b is not None
        assert rec_b.token_disposition == AttemptUsageDisposition.UNAVAILABLE


# ===========================================================================
# Group 4: Strict Usage Audit Contracts (P0-4 / Section 4+5)
# ===========================================================================


class TestStrictUsageAuditContracts:
    """R9 Section 4+5: shared Usage types use strict Pydantic types
    (no ``Any``).  :class:`AttemptUsageRecord` enforces per-dimension
    invariants."""

    def test_task_attempt_record_has_strict_disposition_types(self):
        """``TaskAttemptRecord.token_disposition`` and
        ``cost_disposition`` are :class:`AttemptUsageDisposition`,
        not ``Any``."""
        field = TaskAttemptRecord.model_fields["token_disposition"]
        assert field.annotation is AttemptUsageDisposition
        field = TaskAttemptRecord.model_fields["cost_disposition"]
        assert field.annotation is AttemptUsageDisposition

    def test_task_attempt_rejects_unknown_disposition(self):
        """A ``TaskAttemptRecord`` with an invalid disposition string
        must raise ``ValidationError`` — strict types, no ``Any``."""
        with pytest.raises(ValidationError):
            TaskAttemptRecord(
                task_id="t1",
                agent_id="agent_a",
                attempt=0,
                started_at=_FIXED_TS,
                status="failed",
                token_disposition="anything",  # type: ignore[arg-type]
            )

    def test_execution_usage_rejects_invalid_attempt_record(self):
        """``ExecutionUsage.attempt_usage_records`` is
        ``list[AttemptUsageRecord]``, not ``list[Any]``.  Passing a
        non-:class:`AttemptUsageRecord` value must fail."""
        from multi_agent.contracts import ExecutionUsage

        with pytest.raises(ValidationError):
            ExecutionUsage(
                agent_calls=1,
                attempt_usage_records=["not-a-record"],  # type: ignore[list-item]
            )

    def test_verified_record_requires_value_and_source(self):
        """``AttemptUsageRecord`` with ``VERIFIED`` disposition must
        have a non-None value AND a non-None source_id for that
        dimension."""
        # VERIFIED without value → rejected
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="t1",
                attempt=0,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                tokens_used=None,
                token_source_id="src1",
            )
        # VERIFIED without source_id → rejected
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="t1",
                attempt=0,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                tokens_used=100,
                token_source_id=None,
            )
        # VERIFIED with both → accepted
        rec = AttemptUsageRecord(
            task_id="t1",
            attempt=0,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            tokens_used=100,
            token_source_id="src1",
        )
        assert rec.tokens_used == 100
        assert rec.token_source_id == "src1"

    def test_no_provider_call_record_rejects_actual_usage(self):
        """``AttemptUsageRecord`` with ``NO_PROVIDER_CALL`` disposition
        must NOT have a value or source_id for that dimension."""
        # NO_PROVIDER_CALL with value → rejected
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="t1",
                attempt=0,
                token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                tokens_used=100,
            )
        # NO_PROVIDER_CALL with source_id → rejected
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="t1",
                attempt=0,
                token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                tokens_used=None,
                token_source_id="src1",
            )
        # NO_PROVIDER_CALL clean → accepted
        rec = AttemptUsageRecord(
            task_id="t1",
            attempt=0,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        assert rec.tokens_used is None
        assert rec.token_source_id is None

    def test_unavailable_record_rejects_actual_usage(self):
        """``AttemptUsageRecord`` with ``UNAVAILABLE`` disposition
        must NOT have a value for that dimension."""
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="t1",
                attempt=0,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=100,
            )
        # UNAVAILABLE with None value → accepted
        rec = AttemptUsageRecord(
            task_id="t1",
            attempt=0,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        assert rec.tokens_used is None

    def test_mixed_verified_and_unavailable_usage_is_preserved(self):
        """A mixed record (Token=VERIFIED + Cost=UNAVAILABLE) is valid
        and preserves both dimensions' data."""
        rec = AttemptUsageRecord(
            task_id="t1",
            attempt=0,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=100,
            token_source_id="src1",
        )
        assert rec.token_disposition == AttemptUsageDisposition.VERIFIED
        assert rec.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert rec.tokens_used == 100
        assert rec.cost_usd is None


# ===========================================================================
# Group 5: Source Binding — no cross-dimension fallback (P0-5 / Section 6)
# ===========================================================================


class TestSourceBindingNoFallback:
    """R9 Section 6: ``tokens_verified=True`` requires
    ``token_source_id``; no fallback to legacy ``source_id``."""

    def test_token_source_does_not_fill_cost_source(self):
        """``UsageProvenance`` with ``tokens_verified=True`` and
        ``token_source_id="src"`` but ``cost_verified=True`` and
        NO ``cost_source_id`` must raise ``ValidationError`` — the
        token source cannot fill the cost source."""
        with pytest.raises(ValidationError):
            UsageProvenance(
                source_id="legacy_src",
                token_source_id="token_src",
                tokens_verified=True,
                cost_verified=True,
                # cost_source_id is None — must NOT fall back to
                # source_id or token_source_id.
            )

    def test_cost_source_does_not_fill_token_source(self):
        """``UsageProvenance`` with ``cost_verified=True`` and
        ``cost_source_id="src"`` but ``tokens_verified=True`` and
        NO ``token_source_id`` must raise ``ValidationError``."""
        with pytest.raises(ValidationError):
            UsageProvenance(
                source_id="legacy_src",
                cost_source_id="cost_src",
                tokens_verified=True,
                cost_verified=True,
                # token_source_id is None — must NOT fall back.
            )

    def test_legacy_source_id_alone_does_not_satisfy_verified(self):
        """Setting only ``source_id`` (legacy) with
        ``tokens_verified=True`` must raise — the legacy field does
        NOT satisfy the per-dimension requirement."""
        with pytest.raises(ValidationError):
            UsageProvenance(
                source_id="legacy_only",
                tokens_verified=True,
                cost_verified=False,
            )

    def test_accountant_does_not_fallback_to_legacy_source_id(self):
        """When a receipt has ``tokens_verified=True`` and
        ``token_source_id`` is in the bound set, but the legacy
        ``source_id`` is different, the Accountant uses
        ``token_source_id`` — NOT the legacy ``source_id``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
            ),
            tool_calls=0,
            tokens_used=100,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="legacy_different",
                token_source_id="token_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="token_verifier",
            never_calls_provider=False,
            bound_token_source_ids=frozenset({"token_verifier"}),
        )
        # Should succeed — token_source_id matches the bound set.
        # The legacy source_id "legacy_different" is NOT in the bound
        # set, but it's not used.
        accountant.record_receipt(
            receipt,
            invoker_capabilities=caps,
            task_id=task.task_id,
            attempt=0,
        )
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_source_id == "token_verifier"


# ===========================================================================
# Group 6: Legacy Trust — always conflict (P1-1 / Section 7)
# ===========================================================================


class TestLegacyTrustAlwaysConflict:
    """R9 Section 7: ANY simultaneous provision of ``usage_trust``
    and ``usage_provenance`` is a ``ValidationError``, even when the
    derived trust matches."""

    def test_legacy_and_new_trust_always_conflict(self):
        """Simultaneous ``usage_trust`` and ``usage_provenance`` with
        MATCHING derived trust must STILL raise ``ValidationError``.
        R9 removes the R8 carve-out that allowed matching values."""
        task = _make_task()
        # The provenance derives to "unverified" (neither verified).
        # The trust is also "unverified" — they MATCH.
        with pytest.raises(ValidationError):
            AgentInvocationReceipt(
                result=_ok_result(task=task),
                tool_calls=0,
                usage_trust="unverified",
                usage_provenance=UsageProvenance(
                    source_id="unverified",
                    tokens_verified=False,
                    cost_verified=False,
                ),
            )

    def test_legacy_trust_alone_is_accepted_with_warning(self):
        """Providing ONLY ``usage_trust`` (legacy) is still accepted —
        the provenance is derived from it for backwards compatibility."""
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task),
            tool_calls=0,
            usage_trust="unverified",
        )
        assert receipt.usage_trust == "unverified"
        assert receipt.usage_provenance is not None

    def test_provenance_alone_is_accepted(self):
        """Providing ONLY ``usage_provenance`` is the preferred path —
        ``usage_trust`` is derived from it."""
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task),
            tool_calls=0,
            usage_provenance=UsageProvenance(
                source_id="verifier",
                token_source_id="verifier",
                cost_source_id="verifier",
                tokens_verified=True,
                cost_verified=True,
            ),
        )
        # R9: ``_provenance_to_trust`` returns ``trusted_adapter`` when
        # ``cost_verified=True`` (cost is checked first), even when
        # ``tokens_verified`` is also True.
        assert receipt.usage_trust == "trusted_adapter"
        assert receipt.usage_provenance.tokens_verified is True
        assert receipt.usage_provenance.cost_verified is True


# ===========================================================================
# Group 7: VerifiedUsage Source — Choice A (P1-2 / Section 8)
# ===========================================================================


class TestVerifiedUsageSourceChoiceA:
    """R9 Section 8 (Choice A): ``VerifiedUsage`` no longer carries
    ``token_source_id`` / ``cost_source_id``.  The Invoker uses the
    Verifier's frozen ``source_id`` for both dimensions."""

    def test_verified_usage_has_no_per_dimension_source_fields(self):
        """``VerifiedUsage`` must NOT have ``token_source_id`` or
        ``cost_source_id`` fields — they were removed in R9
        Section 8 (Choice A)."""
        field_names = set(VerifiedUsage.model_fields.keys())
        assert "token_source_id" not in field_names, (
            "VerifiedUsage.token_source_id must be removed (R9 Section 8 Choice A)"
        )
        assert "cost_source_id" not in field_names, (
            "VerifiedUsage.cost_source_id must be removed (R9 Section 8 Choice A)"
        )

    def test_verified_usage_retains_core_fields(self):
        """``VerifiedUsage`` retains ``tokens_verified``,
        ``cost_verified``, ``tokens_used``, ``cost_usd``, and the
        deprecated ``verified`` field."""
        v = VerifiedUsage(
            tokens_verified=True,
            cost_verified=False,
            tokens_used=100,
        )
        assert v.tokens_verified is True
        assert v.cost_verified is False
        assert v.tokens_used == 100
        assert v.verified is True  # auto-derived

    def test_verified_usage_per_dimension_invariants(self):
        """``tokens_verified=True`` requires ``tokens_used`` non-None;
        ``cost_verified=True`` requires ``cost_usd`` non-None."""
        # Token verified without value → rejected
        with pytest.raises(ValidationError):
            VerifiedUsage(tokens_verified=True, tokens_used=None)
        # Cost verified without value → rejected
        with pytest.raises(ValidationError):
            VerifiedUsage(cost_verified=True, cost_usd=None)
        # Both verified with values → accepted
        v = VerifiedUsage(
            tokens_verified=True,
            cost_verified=True,
            tokens_used=100,
            cost_usd=Decimal("0.50"),
        )
        assert v.verified is True
