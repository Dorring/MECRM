"""Phase 4 R8 regression tests.

Direct counter-examples for the five P0 concerns and one P1 cleanup
identified in the Phase 4 R8 review:

* **P0-1** — ``NO_PROVIDER_CALL`` disposition must be explicitly
  attested by a trusted Invoker with
  ``never_calls_provider=True``.  A hybrid Invoker that
  omits ``provider_metadata`` produces ``UNAVAILABLE``, not
  ``NO_PROVIDER_CALL``.
* **P0-2** — Per-dimension verifier result: a cost-only verifier
  (``verifies_tokens=False``) cannot elevate Token trust, and
  vice-versa.  ``VerifiedUsage`` enforces per-dimension invariants.
* **P0-3** — Per-dimension source binding: Token and Cost
  ``bound_*_source_ids`` are checked independently.  A receipt
  with a correct token source but wrong cost source is rejected
  on the cost dimension only.
* **P0-4** — Atomic accounting: ``record_receipt`` uses a
  three-phase atomic commit (compute -> validate -> commit).  A
  validation failure cannot leave the accountant in a
  half-committed state.  Every committed call produces exactly
  one ``AttemptUsageRecord``.
* **P0-5** — Trusted vs Declared audit: ``TaskAttemptRecord``
  separates actual VERIFIED usage (``tokens_used`` /
  ``cost_usd``) from untrusted declared values
  (``declared_tokens_used`` / ``declared_cost_usd``).
  ``ExecutionUsage.attempt_usage_records`` exposes per-attempt
  records for external audit.
* **P1-2** — Legacy ``usage_trust`` and ``usage_provenance``
  cannot be provided simultaneously with conflicting values —
  the receipt raises ``ValidationError`` instead of silently
  overriding one with the other.
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
from multi_agent.execution_errors import (
    ExecutionUsageUnavailableError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    AttemptUsageDisposition,
    AttemptUsageRecord,
    DeterministicFakeInvoker,
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
from multi_agent.run_store import InMemoryRunStore
from multi_agent.supervisor import SupervisorRuntime, _BudgetAccountant


# ---------------------------------------------------------------------------
# Shared helpers (copied from test_supervisor_r7.py and test_supervisor_r5.py)
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


# ===========================================================================
# Group 1: Explicit Attempt Disposition (P0-1)
# ===========================================================================


class TestExplicitAttemptDisposition:
    """R8 P0-1: ``NO_PROVIDER_CALL`` must be explicitly attested by a
    trusted Invoker with ``never_calls_provider=True``."""

    def test_no_provider_call_requires_explicit_attempt_attestation(self):
        """A receipt with ``token_disposition=NO_PROVIDER_CALL`` but
        ``invoker_capabilities.never_calls_provider=False`` must
        be rejected — only a trusted deterministic Invoker can attest
        no provider call."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=False,
            source_id="hybrid_invoker",
            never_calls_provider=False,
        )
        with pytest.raises(ExecutionUsageUnavailableError) as exc_info:
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )
        msg = str(exc_info.value)
        assert "NO_PROVIDER_CALL" in msg, (
            f"error must mention NO_PROVIDER_CALL, got: {msg}"
        )
        assert "never_calls_provider" in msg, (
            f"error must mention never_calls_provider, got: {msg}"
        )

    def test_hybrid_invoker_missing_metadata_is_not_no_provider_call(self):
        """A receipt with NO explicit ``token_disposition`` (defaults to
        ``UNAVAILABLE``) and ``provider_metadata=None`` from a hybrid
        invoker (``never_calls_provider=False``) must NOT be
        treated as ``NO_PROVIDER_CALL``.  With a token budget
        configured, the ``UNAVAILABLE`` disposition triggers
        fail-closed.

        R9 Section 1: ``record_receipt`` commits the UNAVAILABLE
        record and then sets ``exceeded`` — no exception is raised.
        """
        budget = ExecutionBudget(token_budget=1000)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
        )
        # The receipt's default disposition is UNAVAILABLE, NOT
        # NO_PROVIDER_CALL — a hybrid invoker cannot self-attest.
        assert receipt.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert receipt.cost_disposition == AttemptUsageDisposition.UNAVAILABLE

        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=False,
            source_id="hybrid_invoker",
            never_calls_provider=False,
        )
        # R9 Section 1: commit-then-check — the UNAVAILABLE record is
        # committed, then exceeded is set.
        accountant.record_receipt(
            receipt,
            invoker_capabilities=caps,
            task_id=task.task_id,
            attempt=0,
        )
        assert accountant.exceeded
        assert accountant.usage_unavailable
        assert accountant.exceeded_reason == "execution_usage_unavailable"
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE

    def test_no_receipt_deterministic_attempt_can_attest_no_call(self):
        """R9 Section 3: ``NO_PROVIDER_CALL`` on the no-receipt path
        requires an explicit :class:`AgentInvocationOutcome` from the
        Invoker.  The static ``never_calls_provider`` capability is no
        longer sufficient — the Invoker must explicitly attest it via
        an Outcome (e.g. via :class:`AgentInvocationFailure`)."""
        from multi_agent.invocation import AgentInvocationOutcome

        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        outcome = AgentInvocationOutcome(
            error_code="deterministic_error",
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
        assert accountant.exceeded is False
        assert accountant.usage_unavailable is False


# ===========================================================================
# Group 2: Per-dimension Verifier Result (P0-2)
# ===========================================================================


class TestPerDimensionVerifierResult:
    """R8 P0-2: Token and Cost verification are independent.  A
    cost-only verifier cannot elevate Token trust, and vice-versa."""

    def test_cost_only_verifier_does_not_verify_tokens(self):
        """A ``UsageVerificationCapabilities`` with
        ``verifies_tokens=False`` must reject any receipt that claims
        ``token_disposition=VERIFIED`` — a cost-only verifier cannot
        self-elevate token trust."""
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
                token_source_id="cost_only",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=True,
            source_id="cost_only",
            bound_cost_source_ids=frozenset({"cost_only"}),
        )
        with pytest.raises(ExecutionUsageUnavailableError, match="verifies_tokens"):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )

    def test_token_only_verifier_does_not_verify_cost(self):
        """A ``UsageVerificationCapabilities`` with
        ``verifies_cost=False`` must reject any receipt that claims
        ``cost_disposition=VERIFIED`` — a token-only verifier cannot
        self-elevate cost trust."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                cost_source_id="token_only",
                tokens_verified=False,
                cost_verified=True,
            ),
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="token_only",
            bound_token_source_ids=frozenset({"token_only"}),
        )
        with pytest.raises(ExecutionUsageUnavailableError, match="verifies_cost"):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )

    def test_verified_zero_tokens_requires_token_attestation(self):
        """``VerifiedUsage(tokens_verified=True, tokens_used=0)`` is
        valid (zero is a legitimate verified value).  But
        ``VerifiedUsage(tokens_verified=True, tokens_used=None)`` must
        raise ``ValidationError`` — a verified dimension requires a
        non-None value."""
        valid = VerifiedUsage(tokens_verified=True, tokens_used=0)
        assert valid.tokens_verified is True
        assert valid.tokens_used == 0

        with pytest.raises(ValidationError):
            VerifiedUsage(tokens_verified=True, tokens_used=None)


# ===========================================================================
# Group 3: Per-dimension Source Binding (P0-3)
# ===========================================================================


class TestPerDimensionSourceBinding:
    """R8 P0-3: Token and Cost ``bound_*_source_ids`` are checked
    independently.  Unverified dimensions are not checked against bound
    sources."""

    def test_unverified_receipt_not_rejected_by_unrelated_bound_source(self):
        """A receipt with ``token_disposition=UNAVAILABLE`` (not
        VERIFIED) and a ``token_source_id`` that is NOT in the invoker's
        ``bound_token_source_ids`` must NOT be rejected — unverified
        dimensions are not checked against bound sources."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                token_source_id="some_other_source",
                tokens_verified=False,
                cost_verified=False,
            ),
            # UNAVAILABLE — not VERIFIED, so source binding is not checked
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="verifier_a_caps",
            bound_token_source_ids=frozenset({"verifier_a"}),
        )
        # Should NOT raise — unverified dimensions skip the source check
        accountant.record_receipt(
            receipt,
            invoker_capabilities=caps,
            task_id=task.task_id,
            attempt=0,
        )
        assert len(accountant._attempt_records) == 1

    def test_verified_capability_requires_bound_source(self):
        """``UsageVerificationCapabilities(verifies_tokens=True)`` with
        an empty ``bound_token_source_ids`` must raise ``ValidationError``
        — declaring verification capability without binding any source
        is a programming error."""
        with pytest.raises(ValidationError):
            UsageVerificationCapabilities(
                verifies_tokens=True,
                source_id="test",
                bound_token_source_ids=frozenset(),
            )

    def test_token_and_cost_sources_are_checked_independently(self):
        """A receipt with a correct ``token_source_id`` but a wrong
        ``cost_source_id`` must be rejected on the COST dimension only
        — the token dimension's source binding is checked independently."""
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
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                token_source_id="token_verifier",
                cost_source_id="wrong_cost_source",
                tokens_verified=True,
                cost_verified=True,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="dual_verifier",
            bound_token_source_ids=frozenset({"token_verifier"}),
            bound_cost_source_ids=frozenset({"cost_verifier"}),
        )
        with pytest.raises(ExecutionUsageUnavailableError, match="cost_source_id"):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )


# ===========================================================================
# Group 4: Atomic Accounting (P0-4)
# ===========================================================================


class TestAtomicAccounting:
    """R8 P0-4: ``record_receipt`` uses a three-phase atomic commit
    (compute -> validate -> commit).  A validation failure cannot leave
    the accountant in a half-committed state."""

    def test_source_mismatch_marks_budget_usage_unavailable(self):
        """When ``record_receipt`` raises due to a source mismatch, the
        caller must call ``record_usage_unavailable`` (as
        ``_execute_task`` does).  With a token budget configured, the
        UNAVAILABLE disposition sets ``exceeded=True`` with reason
        ``execution_usage_unavailable``."""
        budget = ExecutionBudget(token_budget=1000)
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
                token_source_id="unbound_source",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="verifier",
            bound_token_source_ids=frozenset({"correct_source"}),
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )
        # Simulate what _execute_task does after catching the exception
        accountant.record_usage_unavailable(task_id=task.task_id, attempt=0)
        assert accountant.exceeded is True
        assert accountant.exceeded_reason == "execution_usage_unavailable"

    def test_record_receipt_is_atomic_on_token_failure(self):
        """When token validation fails (source mismatch), NO state is
        mutated — ``_tokens_used`` stays 0, ``_attempt_records`` stays
        empty, and ``_token_usage_applicable_attempts`` stays 0."""
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
                token_source_id="wrong",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="verifier",
            bound_token_source_ids=frozenset({"correct"}),
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )
        # No state mutated — the compute phase raised before commit
        assert accountant._tokens_used == 0
        assert len(accountant._attempt_records) == 0
        assert accountant._token_usage_applicable_attempts == 0

    def test_record_receipt_is_atomic_on_cost_failure(self):
        """When cost validation fails (source mismatch), NO state is
        mutated — ``_cost_usd`` stays 0, ``_attempt_records`` stays
        empty, ``_cost_usage_applicable_attempts`` stays 0, and the
        token dimension was NOT partially committed either."""
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
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                token_source_id="token_correct",
                cost_source_id="wrong_cost",
                tokens_verified=True,
                cost_verified=True,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="dual_verifier",
            bound_token_source_ids=frozenset({"token_correct"}),
            bound_cost_source_ids=frozenset({"cost_correct"}),
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=caps,
                task_id=task.task_id,
                attempt=0,
            )
        # No state mutated — cost validation failed before commit
        assert accountant._cost_usd == Decimal("0")
        assert len(accountant._attempt_records) == 0
        assert accountant._cost_usage_applicable_attempts == 0
        # Token state was NOT partially committed either
        assert accountant._tokens_used == 0

    def test_every_committed_call_has_exactly_one_usage_record(self):
        """Every committed agent call produces EXACTLY ONE
        ``AttemptUsageRecord``.  Three calls (VERIFIED, UNAVAILABLE,
        NO_PROVIDER_CALL) produce three records, and
        ``last_attempt_record`` is the third."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # 1. VERIFIED via record_receipt
        verified_caps = UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="verifier",
            bound_token_source_ids=frozenset({"verifier"}),
            bound_cost_source_ids=frozenset({"verifier"}),
        )
        receipt_verified = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
            ),
            tool_calls=0,
            tokens_used=100,
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                token_source_id="verifier",
                cost_source_id="verifier",
                tokens_verified=True,
                cost_verified=True,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt_verified,
            invoker_capabilities=verified_caps,
            task_id=task.task_id,
            attempt=0,
        )

        # 2. UNAVAILABLE via record_usage_unavailable (no outcome)
        accountant.record_usage_unavailable(task_id=task.task_id, attempt=1)

        # 3. NO_PROVIDER_CALL via record_usage_unavailable with an
        # explicit Outcome (R9 Section 3: capability-based inference
        # is no longer accepted — only an explicit Outcome attests it).
        from multi_agent.invocation import AgentInvocationOutcome

        no_call_outcome = AgentInvocationOutcome(
            error_code="deterministic_skip",
            observed_tool_calls=0,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        accountant.record_usage_unavailable(
            task_id=task.task_id,
            attempt=2,
            outcome=no_call_outcome,
        )

        assert len(accountant._attempt_records) == 3
        last = accountant.last_attempt_record
        assert last is not None
        assert last.attempt == 2
        assert last.token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL


# ===========================================================================
# Group 5: Trusted vs Declared Audit (P0-5)
# ===========================================================================


class TestTrustedVsDeclaredAudit:
    """R8 P0-5: ``TaskAttemptRecord`` separates actual VERIFIED usage
    from untrusted declared values.  ``ExecutionUsage.attempt_usage_records``
    exposes per-attempt records for external audit."""

    def test_invalid_receipt_does_not_publish_actual_tokens(self):
        """A ``TaskAttemptRecord`` with ``token_disposition=UNAVAILABLE``
        must have ``tokens_used=None`` (actual field is None for
        UNAVAILABLE).  The declared value is retained in
        ``declared_tokens_used`` for audit only."""
        record = TaskAttemptRecord(
            task_id="t1",
            agent_id="agent_a",
            attempt=0,
            started_at=_FIXED_TS,
            status="failed",
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            declared_tokens_used=100,
        )
        assert record.tokens_used is None
        assert record.declared_tokens_used == 100
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE

    def test_invalid_receipt_does_not_publish_actual_cost(self):
        """A ``TaskAttemptRecord`` with ``cost_disposition=UNAVAILABLE``
        must have ``cost_usd=None`` (actual field is None for
        UNAVAILABLE).  The declared value is retained in
        ``declared_cost_usd`` for audit only."""
        record = TaskAttemptRecord(
            task_id="t1",
            agent_id="agent_a",
            attempt=0,
            started_at=_FIXED_TS,
            status="failed",
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            declared_cost_usd=Decimal("1.50"),
        )
        assert record.cost_usd is None
        assert record.declared_cost_usd == Decimal("1.50")

    @pytest.mark.asyncio
    async def test_attempt_usage_records_are_returned_in_run_result(self):
        """``SupervisorRunResult.usage.attempt_usage_records`` must be a
        non-empty list of ``AttemptUsageRecord`` instances, each carrying
        ``token_disposition`` and ``cost_disposition`` fields."""
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

        runtime = SupervisorRuntime(
            invoker=DeterministicFakeInvoker(factory=factory),
            run_store=InMemoryRunStore(),
            plan_validator=_AlwaysValidPlanValidator(),
        )
        result = await runtime.execute(plan, reg)

        assert len(result.usage.attempt_usage_records) > 0, (
            "attempt_usage_records must be non-empty after a run"
        )
        for rec in result.usage.attempt_usage_records:
            assert isinstance(rec, AttemptUsageRecord)
            assert hasattr(rec, "token_disposition")
            assert hasattr(rec, "cost_disposition")
            assert isinstance(rec.token_disposition, AttemptUsageDisposition)
            assert isinstance(rec.cost_disposition, AttemptUsageDisposition)


# ===========================================================================
# Group 6: Legacy Trust Cleanup (P1-2)
# ===========================================================================


class TestLegacyTrustCleanup:
    """R8 P1-2 / R9 Section 7: Legacy ``usage_trust`` and
    ``usage_provenance`` cannot be provided simultaneously — the
    receipt raises ``ValidationError`` instead of silently overriding
    one with the other.  R9 tightens this: ANY simultaneous provision
    is rejected, even when the derived trust matches."""

    def test_legacy_trust_and_provenance_conflict_rejected(self):
        """An ``AgentInvocationReceipt`` constructed with BOTH
        ``usage_trust="verified_provider"`` AND a ``usage_provenance``
        that derives to a DIFFERENT trust level must raise
        ``ValidationError`` — simultaneous provision with conflicting
        values is a programming error."""
        task = _make_task()
        with pytest.raises(ValidationError):
            AgentInvocationReceipt(
                result=_ok_result(task=task),
                tool_calls=0,
                usage_trust="verified_provider",
                usage_provenance=UsageProvenance(
                    source_id="some_src",
                    tokens_verified=False,
                    cost_verified=False,
                ),
            )
