"""Phase 4 R7 regression tests.

Direct counter-examples for the five P0 concerns and two P1 cleanups
identified in the Phase 4 R7 review:

* **P0-1** — ``no_provider_call`` must be attested by a trusted
  Invoker/Adapter, NOT inferred from ``AgentResult.provider_metadata
  is None``.  A live Handler that omits ``provider_metadata``
  produces ``UNAVAILABLE``, not ``NO_PROVIDER_CALL``.
* **P0-2** — Invalid Receipts must produce a Usage Disposition
  (``UNAVAILABLE`` for both Token and Cost).  When a budget is
  configured, the run must fail-closed.
* **P0-3** — Token and Cost have INDEPENDENT coverage denominators.
  A cost-only adapter verifying Attempt B's cost cannot "offset"
  Attempt A's missing cost.
* **P0-4** — ``should_retry_result()`` uses ``error_code`` and
  ``retryable`` from the SAME :class:`AgentError`.  The old pattern
  of ``errors[0].error_code`` + ``any(e.retryable)`` could combine
  flags from different errors.
* **P0-5** — ``ProviderUsageVerifier.verify()`` is async and is
  bounded by the outer ``asyncio.wait_for``, so a slow verifier
  cannot block the event loop or exceed the run deadline.
* **P0-6** — Receipt ``usage_provenance.source_id`` must be in the
  Invoker's ``bound_source_ids`` — a receipt cannot claim provenance
  from an unbound source.
* **P1-1** — Legacy ``usage_trust`` / ``UsageTrustLevel`` is
  DEPRECATED; new code uses ``UsageProvenance`` and
  ``AttemptUsageDisposition``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentError,
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
from multi_agent.execution_errors import (
    ExecutionUsageUnavailableError,
    NonRetryableAgentError,
)
from multi_agent.invocation import (
    AgentInvocationReceipt,
    AttemptUsageDisposition,
    AttemptUsageRecord,
    DeterministicFakeInvoker,
    RegistryAgentInvoker,
    UsageProvenance,
    UsageVerificationCapabilities,
    VerifiedUsage,
)
from multi_agent.planning import (
    RetryPolicy,
)
from multi_agent.supervisor import (
    _BudgetAccountant,
    should_retry_result,
)


# ---------------------------------------------------------------------------
# Shared helpers
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
    errors: list[AgentError] | None = None,
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


# ---------------------------------------------------------------------------
# Verifier helpers (async — R7 P0-5)
# ---------------------------------------------------------------------------


class _TrackingVerifier:
    """Async fake verifier that records every ``verify()`` call."""

    source_id: str = "tracking_verifier"

    def __init__(self, *, result: VerifiedUsage | None = None) -> None:
        self._result = result or VerifiedUsage(
            tokens_used=200,
            cost_usd=None,
            tokens_verified=True,
        )
        self.call_count: int = 0

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        self.call_count += 1
        return self._result


class _SlowVerifier:
    """Async verifier that sleeps well beyond any reasonable deadline.

    R7 P0-5: proves the verifier is bounded by ``asyncio.wait_for``
    and cannot block the event loop or exceed the run deadline.
    """

    source_id: str = "slow_verifier"

    def __init__(self, delay_s: float = 10.0) -> None:
        self._delay_s = delay_s
        self.started = False
        self.completed = False

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        self.started = True
        await asyncio.sleep(self._delay_s)
        self.completed = True
        return VerifiedUsage(tokens_used=100, cost_usd=None, tokens_verified=True)


class _CostOnlyVerifier:
    """Async verifier that only verifies cost, not tokens."""

    source_id: str = "cost_only_verifier"

    def __init__(self, cost_usd: Decimal = Decimal("0.50")) -> None:
        self._cost_usd = cost_usd

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage:
        return VerifiedUsage(
            tokens_used=0,
            cost_usd=self._cost_usd,
            cost_verified=True,
        )


# ---------------------------------------------------------------------------
# Capability helpers
# ---------------------------------------------------------------------------


_REGISTRY_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="registry_agent_invoker",
    can_attest_no_provider_call=False,
    bound_source_ids=frozenset(),
)

_REGISTRY_CAPS_WITH_VERIFIER = UsageVerificationCapabilities(
    verifies_tokens=True,
    verifies_cost=True,
    source_id="registry_agent_invoker+provider_verifier",
    can_attest_no_provider_call=False,
    bound_source_ids=frozenset({"tracking_verifier"}),
)

_DETERMINISTIC_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="deterministic_fake_invoker",
    can_attest_no_provider_call=True,
    bound_source_ids=frozenset({"deterministic_fake_invoker"}),
)


# ===========================================================================
# P0-1: Trusted No-provider-call — Handler cannot self-attest
# ===========================================================================


class TestTrustedNoProviderCall:
    """R7 P0-1: ``NO_PROVIDER_CALL`` must be attested by a trusted
    Invoker, not inferred from ``provider_metadata is None``."""

    def test_missing_metadata_cannot_attest_no_provider_call(self):
        """A RegistryAgentInvoker (``can_attest_no_provider_call=False``)
        receiving a result without ``provider_metadata`` must produce
        ``UNAVAILABLE`` dispositions, NOT ``NO_PROVIDER_CALL``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_REGISTRY_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        record = accountant._attempt_records[-1]
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        # UNAVAILABLE counts as applicable for coverage
        assert accountant._token_usage_applicable_attempts == 1
        assert accountant._cost_usage_applicable_attempts == 1

    def test_live_handler_omission_fails_token_budget(self):
        """A live Handler that omits ``provider_metadata`` must
        trigger fail-closed when a token budget is configured."""
        budget = ExecutionBudget(token_budget=1000)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=_REGISTRY_CAPS,
                task_id=task.task_id,
                attempt=0,
            )
        # R8 P0-4: record_receipt raises without mutating state.  The
        # caller must call record_usage_unavailable to produce exactly
        # one AttemptUsageRecord and set the exceeded flag — this is
        # what _execute_task does after catching the exception.
        accountant.record_usage_unavailable(task_id=task.task_id, attempt=0)
        assert accountant.exceeded
        assert accountant.usage_unavailable
        assert accountant.exceeded_reason == "execution_usage_unavailable"

    def test_live_handler_omission_fails_cost_budget(self):
        """A live Handler that omits ``provider_metadata`` must
        trigger fail-closed when a cost budget is configured."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=_REGISTRY_CAPS,
                task_id=task.task_id,
                attempt=0,
            )
        # R8 P0-4: caller must call record_usage_unavailable after
        # catching the exception — see _execute_task for the pattern.
        accountant.record_usage_unavailable(task_id=task.task_id, attempt=0)
        assert accountant.exceeded
        assert accountant.usage_unavailable

    def test_trusted_deterministic_no_call_is_accepted(self):
        """A trusted deterministic Invoker
        (``can_attest_no_provider_call=True``) with no
        ``provider_metadata`` produces ``NO_PROVIDER_CALL`` and does
        NOT fail-closed even when budgets are configured."""
        budget = ExecutionBudget(
            token_budget=1000,
            cost_budget_usd=Decimal("1.00"),
        )
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            # R7 P0-6: source_id must match the deterministic invoker's
            # bound_source_ids so the receipt is not rejected as an
            # unbound provenance claim.
            usage_provenance=UsageProvenance(
                source_id="deterministic_fake_invoker",
                tokens_verified=False,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_DETERMINISTIC_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        record = accountant._attempt_records[-1]
        assert record.token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL
        assert record.cost_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL
        # NO_PROVIDER_CALL does not count as applicable
        assert accountant._token_usage_applicable_attempts == 0
        assert accountant._cost_usage_applicable_attempts == 0
        # No budget exceeded
        assert not accountant.exceeded
        assert not accountant.usage_unavailable

    def test_trusted_deterministic_with_provider_call_is_verified(self):
        """When a trusted deterministic Invoker has
        ``can_attest_no_provider_call=True`` but the receipt HAS
        ``provider_metadata``, the disposition is ``UNAVAILABLE`` (not
        ``NO_PROVIDER_CALL``) because a provider call was made."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=_provider_meta()),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            # R7 P0-6: source_id must match the deterministic invoker's
            # bound_source_ids.
            usage_provenance=UsageProvenance(
                source_id="deterministic_fake_invoker",
                tokens_verified=False,
                cost_verified=False,
            ),
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_DETERMINISTIC_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        record = accountant._attempt_records[-1]
        # provider_metadata is present → not NO_PROVIDER_CALL
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE


# ===========================================================================
# P0-2: Invalid Receipt marks Usage Unavailable
# ===========================================================================


class TestInvalidReceiptUsage:
    """R7 P0-2: Invalid Receipts must produce ``UNAVAILABLE``
    dispositions for both Token and Cost."""

    def test_invalid_receipt_marks_token_usage_unavailable(self):
        """An invalid receipt must mark token usage as UNAVAILABLE."""
        budget = ExecutionBudget(token_budget=1000)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        # record_usage_unavailable simulates the invalid-receipt path
        accountant.record_usage_unavailable(task_id="task_a", attempt=0)
        assert accountant._token_usage_applicable_attempts == 1
        assert accountant._verified_token_attempts == 0
        assert accountant.exceeded
        assert accountant.usage_unavailable

    def test_invalid_receipt_marks_cost_usage_unavailable(self):
        """An invalid receipt must mark cost usage as UNAVAILABLE."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_usage_unavailable(task_id="task_a", attempt=0)
        assert accountant._cost_usage_applicable_attempts == 1
        assert accountant._verified_cost_attempts == 0
        assert accountant.exceeded
        assert accountant.usage_unavailable

    def test_invalid_receipt_stops_budgeted_run(self):
        """An invalid receipt with a budget configured must set
        ``exceeded=True`` with reason ``execution_usage_unavailable``."""
        budget = ExecutionBudget(
            token_budget=1000,
            cost_budget_usd=Decimal("1.00"),
        )
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_usage_unavailable(task_id="task_a", attempt=0)
        assert accountant.exceeded
        assert accountant.exceeded_reason == "execution_usage_unavailable"

    def test_invalid_receipt_preserves_observed_tool_calls(self):
        """Observed tool calls are charged BEFORE the invalid receipt
        triggers ``record_usage_unavailable``.  The tool call count
        must be preserved."""
        budget = ExecutionBudget(max_tool_calls=10)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_observed_tool_calls(3)
        accountant.record_usage_unavailable(task_id="task_a", attempt=0)
        assert accountant.tool_calls == 3

    def test_invalid_receipt_without_budget_does_not_fail(self):
        """When NO budget is configured, an invalid receipt records
        UNAVAILABLE but does NOT set ``exceeded`` — the run can
        continue."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        accountant.record_usage_unavailable(task_id="task_a", attempt=0)
        assert accountant._token_usage_applicable_attempts == 1
        assert accountant._cost_usage_applicable_attempts == 1
        assert not accountant.exceeded
        assert not accountant.usage_unavailable


# ===========================================================================
# P0-3: Token and Cost have independent coverage denominators
# ===========================================================================


class TestPerDimensionCoverage:
    """R7 P0-3: Token and Cost coverage denominators are independent."""

    def test_token_and_cost_have_separate_denominators(self):
        """Two attempts where Attempt A verifies tokens only and
        Attempt B verifies cost only must report PARTIAL for both
        dimensions — not COMPLETE."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # Attempt A: tokens_verified=True, cost_verified=False
        receipt_a = AgentInvocationReceipt(
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
                source_id="tracking_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt_a,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=0,
        )

        # Attempt B: tokens_verified=False, cost_verified=True
        receipt_b = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
            ),
            tool_calls=0,
            tokens_used=None,
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                source_id="tracking_verifier",
                tokens_verified=False,
                cost_verified=True,
            ),
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt_b,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=1,
        )

        # Per-dimension denominators
        assert accountant._token_usage_applicable_attempts == 2
        assert accountant._cost_usage_applicable_attempts == 2
        assert accountant._verified_token_attempts == 1
        assert accountant._verified_cost_attempts == 1

        usage = accountant.usage
        # Both dimensions should be PARTIAL (1 of 2 verified)
        assert usage.tokens_usage_status == UsageAvailabilityStatus.PARTIAL
        assert usage.cost_usage_status == UsageAvailabilityStatus.PARTIAL

    def test_cost_adapter_cannot_offset_unknown_provider_cost(self):
        """A cost-only adapter verifying Attempt B's cost cannot
        "offset" Attempt A's missing cost.  Attempt A's cost remains
        UNAVAILABLE and the overall cost status is PARTIAL."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # Attempt A: provider call made, cost NOT verified
        receipt_a = AgentInvocationReceipt(
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
                source_id="tracking_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt_a,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=0,
        )

        # Attempt B: cost verified by a cost-only adapter
        cost_caps = UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=True,
            source_id="cost_only_verifier",
            can_attest_no_provider_call=False,
            bound_source_ids=frozenset({"cost_only_verifier"}),
        )
        receipt_b = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
            ),
            tool_calls=0,
            tokens_used=None,
            cost_usd=Decimal("0.50"),
            usage_provenance=UsageProvenance(
                source_id="cost_only_verifier",
                tokens_verified=False,
                cost_verified=True,
            ),
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt_b,
            invoker_capabilities=cost_caps,
            task_id=task.task_id,
            attempt=1,
        )

        # Cost: 2 applicable, 1 verified → PARTIAL (not COMPLETE)
        assert accountant._cost_usage_applicable_attempts == 2
        assert accountant._verified_cost_attempts == 1
        assert accountant.usage.cost_usage_status == UsageAvailabilityStatus.PARTIAL

    def test_usage_coverage_counts_are_internally_consistent(self):
        """``verified <= applicable`` for both dimensions."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # One verified attempt
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
                source_id="tracking_verifier",
                tokens_verified=True,
                cost_verified=True,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=0,
        )

        # One unavailable attempt
        accountant.record_usage_unavailable(task_id=task.task_id, attempt=1)

        assert (
            accountant._verified_token_attempts
            <= accountant._token_usage_applicable_attempts
        )
        assert (
            accountant._verified_cost_attempts
            <= accountant._cost_usage_applicable_attempts
        )

    def test_mixed_token_and_cost_sources_report_partial_correctly(self):
        """Mixed sources (some VERIFIED, some UNAVAILABLE) must report
        PARTIAL, not COMPLETE."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # Verified attempt
        receipt_a = AgentInvocationReceipt(
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
                source_id="tracking_verifier",
                tokens_verified=True,
                cost_verified=True,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
        )
        accountant.record_receipt(
            receipt_a,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=0,
        )

        # Unavailable attempt
        accountant.record_usage_unavailable(task_id=task.task_id, attempt=1)

        usage = accountant.usage
        assert usage.tokens_usage_status == UsageAvailabilityStatus.PARTIAL
        assert usage.cost_usage_status == UsageAvailabilityStatus.PARTIAL


# ===========================================================================
# P0-4: Multi-error Retry — code and flag from same AgentError
# ===========================================================================


class TestMultiErrorRetry:
    """R7 P0-4: ``should_retry_result()`` uses ``error_code`` and
    ``retryable`` from the SAME :class:`AgentError`."""

    def test_retryable_code_and_flag_are_taken_from_same_error(self):
        """A single error with ``retryable=True`` and code in the
        allowlist must be retried."""
        policy = RetryPolicy(
            max_retries=3,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        errors = [AgentError(error_code="custom_error", message="m", retryable=True)]
        assert should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_nonretryable_error_cannot_borrow_other_retryable_flag(self):
        """Error 1: ``custom_error`` in allowlist but ``retryable=False``.
        Error 2: ``other_error`` NOT in allowlist but ``retryable=True``.

        The old ``errors[0].error_code + any(e.retryable)`` pattern
        would incorrectly retry because ``custom_error`` is in the
        allowlist and ``any(e.retryable)`` is True.  The new
        ``should_retry_result`` must NOT retry — ``custom_error``'s
        ``retryable=False`` cannot be overridden by ``other_error``'s
        flag.
        """
        policy = RetryPolicy(
            max_retries=3,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        errors = [
            AgentError(error_code="custom_error", message="m", retryable=False),
            AgentError(error_code="other_error", message="m", retryable=True),
        ]
        assert not should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_retryable_second_error_uses_its_own_code(self):
        """Error 1: ``custom_error`` NOT retryable.
        Error 2: ``allowed_error`` retryable and in allowlist.

        The retry must succeed because Error 2's own code+flag pair
        matches the allowlist.
        """
        policy = RetryPolicy(
            max_retries=3,
            retryable_error_codes=frozenset({"allowed_error"}),
        )
        errors = [
            AgentError(error_code="custom_error", message="m", retryable=False),
            AgentError(error_code="allowed_error", message="m", retryable=True),
        ]
        assert should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_multiple_error_retry_is_order_invariant(self):
        """Retry decision must be the same regardless of error order."""
        policy = RetryPolicy(
            max_retries=3,
            retryable_error_codes=frozenset({"custom_error"}),
        )
        error_a = AgentError(error_code="custom_error", message="m", retryable=True)
        error_b = AgentError(error_code="other_error", message="m", retryable=False)
        assert should_retry_result(
            policy=policy, attempt_index=0, errors=[error_a, error_b]
        )
        assert should_retry_result(
            policy=policy, attempt_index=0, errors=[error_b, error_a]
        )

    def test_retry_allowlist_matches_same_error_record(self):
        """Only errors where BOTH ``retryable=True`` AND ``error_code``
        is in the allowlist contribute to the retry decision."""
        policy = RetryPolicy(
            max_retries=3,
            retryable_error_codes=frozenset({"allowed_a", "allowed_b"}),
        )
        # allowed_a retryable, allowed_b NOT retryable → only allowed_a counts
        errors = [
            AgentError(error_code="allowed_a", message="m", retryable=True),
            AgentError(error_code="allowed_b", message="m", retryable=False),
        ]
        assert should_retry_result(policy=policy, attempt_index=0, errors=errors)

        # Neither retryable
        errors_neither = [
            AgentError(error_code="allowed_a", message="m", retryable=False),
            AgentError(error_code="allowed_b", message="m", retryable=False),
        ]
        assert not should_retry_result(
            policy=policy, attempt_index=0, errors=errors_neither
        )

    def test_multiple_errors_outside_allowlist_not_retried(self):
        """All errors retryable but none in the allowlist → no retry."""
        policy = RetryPolicy(
            max_retries=3,
            retryable_error_codes=frozenset({"allowed_error"}),
        )
        errors = [
            AgentError(error_code="error_a", message="m", retryable=True),
            AgentError(error_code="error_b", message="m", retryable=True),
        ]
        assert not should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_never_retryable_error_blocks_retry_deterministically(self):
        """Any error in ``NEVER_RETRYABLE_ERROR_CODES`` blocks retry
        regardless of other errors' retryable flags."""
        policy = RetryPolicy(max_retries=3)
        errors = [
            AgentError(error_code="retryable_error", message="m", retryable=True),
            AgentError(error_code="invalid_receipt", message="m", retryable=False),
        ]
        assert not should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_empty_allowlist_retries_any_retryable_error(self):
        """With an empty allowlist, any ``retryable=True`` error is
        enough to retry."""
        policy = RetryPolicy(max_retries=3)
        errors = [AgentError(error_code="any_error", message="m", retryable=True)]
        assert should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_max_retries_zero_never_retries(self):
        """``max_retries=0`` → never retry, regardless of errors."""
        policy = RetryPolicy(max_retries=0)
        errors = [AgentError(error_code="any_error", message="m", retryable=True)]
        assert not should_retry_result(policy=policy, attempt_index=0, errors=errors)

    def test_attempt_index_exhausts_retries(self):
        """``attempt_index >= max_retries`` → no retry."""
        policy = RetryPolicy(max_retries=2)
        errors = [AgentError(error_code="any_error", message="m", retryable=True)]
        assert should_retry_result(policy=policy, attempt_index=0, errors=errors)
        assert should_retry_result(policy=policy, attempt_index=1, errors=errors)
        assert not should_retry_result(policy=policy, attempt_index=2, errors=errors)


# ===========================================================================
# P0-5: Async Verifier respects Deadline and Cancellation
# ===========================================================================


class TestAsyncVerifierDeadline:
    """R7 P0-5: The async verifier is bounded by ``asyncio.wait_for``
    and cannot block the event loop or exceed the run deadline."""

    @pytest.mark.asyncio
    async def test_slow_verifier_respects_deadline(self):
        """A slow verifier must be cancelled by the task timeout /
        run deadline — it cannot run indefinitely."""
        from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

        cap = _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            timeout_ms=200,  # 200ms task timeout
        )
        catalog = ToolCatalog(
            [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
        )
        reg = AgentRegistry(tool_catalog=catalog)
        reg.register(cap, _NoopHandler())

        verifier = _SlowVerifier(delay_s=10.0)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = _make_task(timeout_ms=200)
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

        start = time.monotonic()
        with pytest.raises((asyncio.TimeoutError, NonRetryableAgentError)):
            await asyncio.wait_for(
                invoker.invoke(
                    handler=_ProviderResultHandler(), task=task, context=ctx
                ),
                timeout=1.0,
            )
        elapsed = time.monotonic() - start
        # Must complete well within 10 seconds (the slow verifier's delay)
        assert elapsed < 5.0, (
            f"Slow verifier was not bounded by timeout; elapsed={elapsed:.2f}s"
        )
        # The verifier must NOT have completed
        assert not verifier.completed

    @pytest.mark.asyncio
    async def test_slow_verifier_does_not_block_sibling_tasks(self):
        """A slow verifier in one task must not prevent sibling tasks
        from making progress (the event loop is not blocked)."""
        from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

        cap = _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            timeout_ms=100,
        )
        catalog = ToolCatalog(
            [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
        )
        reg = AgentRegistry(tool_catalog=catalog)
        reg.register(cap, _NoopHandler())

        slow_invoker = RegistryAgentInvoker(
            reg, usage_verifier=_SlowVerifier(delay_s=5.0)
        )
        fast_invoker = DeterministicFakeInvoker(
            result=_ok_result(task=_make_task("fast_task", "agent_a"))
        )

        task_slow = _make_task("slow_task", "agent_a", timeout_ms=100)
        task_fast = _make_task("fast_task", "agent_a", timeout_ms=5000)
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

        async def slow_call():
            with pytest.raises((asyncio.TimeoutError, NonRetryableAgentError)):
                await asyncio.wait_for(
                    slow_invoker.invoke(
                        handler=_ProviderResultHandler(), task=task_slow, context=ctx
                    ),
                    timeout=0.5,
                )

        async def fast_call():
            await asyncio.sleep(0.05)  # let slow start first
            receipt = await asyncio.wait_for(
                fast_invoker.invoke(
                    handler=_NoopHandler(), task=task_fast, context=ctx
                ),
                timeout=2.0,
            )
            return receipt

        slow_task = asyncio.create_task(slow_call())
        fast_task = asyncio.create_task(fast_call())
        fast_receipt = await fast_task
        await slow_task

        assert fast_receipt is not None
        assert fast_receipt.result.task_id == "fast_task"

    @pytest.mark.asyncio
    async def test_verifier_can_be_cancelled(self):
        """Cancelling the invoke() coroutine must cancel the verifier
        — no orphan work remains."""
        from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

        cap = _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            timeout_ms=30_000,
        )
        catalog = ToolCatalog(
            [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
        )
        reg = AgentRegistry(tool_catalog=catalog)
        reg.register(cap, _NoopHandler())

        verifier = _SlowVerifier(delay_s=10.0)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = _make_task(timeout_ms=30_000)
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

        invoke_coro = invoker.invoke(
            handler=_ProviderResultHandler(), task=task, context=ctx
        )
        invoke_task = asyncio.create_task(invoke_coro)
        await asyncio.sleep(0.1)  # let it start
        invoke_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invoke_task
        # The verifier must NOT have completed
        assert not verifier.completed

    @pytest.mark.asyncio
    async def test_verifier_timeout_fails_closed(self):
        """When the verifier times out, the invoker must fail-closed
        (raise an exception), NOT return an unverified receipt."""
        from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

        cap = _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
            timeout_ms=100,
        )
        catalog = ToolCatalog(
            [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
        )
        reg = AgentRegistry(tool_catalog=catalog)
        reg.register(cap, _NoopHandler())

        verifier = _SlowVerifier(delay_s=10.0)
        invoker = RegistryAgentInvoker(reg, usage_verifier=verifier)

        task = _make_task(timeout_ms=100)
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

        with pytest.raises((asyncio.TimeoutError, NonRetryableAgentError)):
            await asyncio.wait_for(
                invoker.invoke(
                    handler=_ProviderResultHandler(), task=task, context=ctx
                ),
                timeout=0.5,
            )
        # Must NOT have completed — the verifier was interrupted
        assert not verifier.completed


# ===========================================================================
# P0-6: Provenance Source Binding
# ===========================================================================


class TestProvenanceSourceBinding:
    """R7 P0-6: Receipt ``usage_provenance.source_id`` must be in the
    Invoker's ``bound_source_ids``."""

    def test_provenance_source_matches_bound_verifier(self):
        """A receipt whose ``source_id`` is in ``bound_source_ids``
        is accepted."""
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
                source_id="tracking_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        # bound_source_ids includes "tracking_verifier"
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=0,
        )
        # Should succeed without raising
        assert accountant._verified_token_attempts == 1

    def test_unbound_source_id_is_rejected(self):
        """A receipt whose ``source_id`` is NOT in
        ``bound_source_ids`` must be rejected with
        ``ExecutionUsageUnavailableError``."""
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
                source_id="rogue_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        with pytest.raises(ExecutionUsageUnavailableError, match="unbound source"):
            accountant.record_receipt(
                receipt,
                invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
                task_id=task.task_id,
                attempt=0,
            )

    def test_empty_bound_source_ids_allows_any(self):
        """When ``bound_source_ids`` is empty, any ``source_id`` is
        accepted (backwards-compatible with Invokers that don't bind
        sources)."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                provider_metadata=_provider_meta(),
            ),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="any_source",
                tokens_verified=False,
                cost_verified=False,
            ),
        )
        # _REGISTRY_CAPS has empty bound_source_ids
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_REGISTRY_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # Should succeed — no source binding check when empty


# ===========================================================================
# P1-1: Legacy usage_trust is DEPRECATED
# ===========================================================================


class TestLegacyTrustDeprecation:
    """R7 P1-1: ``usage_trust`` / ``UsageTrustLevel`` is DEPRECATED.

    The runtime (``_BudgetAccountant``) only reads ``usage_provenance``,
    never ``usage_trust``.  The legacy field is auto-derived from
    ``usage_provenance`` and retained for backwards compatibility.
    """

    def test_runtime_reads_provenance_not_trust(self):
        """The ``_BudgetAccountant.record_receipt()`` method reads
        ``receipt.usage_provenance``, not ``receipt.usage_trust``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # Construct a receipt with verified provenance — the legacy
        # usage_trust is auto-derived.
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
                source_id="tracking_verifier",
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
        )
        # The auto-derived usage_trust should reflect the provenance
        assert receipt.usage_trust is not None

        accountant.record_receipt(
            receipt,
            invoker_capabilities=_REGISTRY_CAPS_WITH_VERIFIER,
            task_id=task.task_id,
            attempt=0,
        )
        # The accountant used provenance (not trust) to verify
        assert accountant._verified_token_attempts == 1
        assert accountant._tokens_used == 100

    def test_attempt_usage_record_uses_disposition_not_trust(self):
        """``AttemptUsageRecord`` stores ``AttemptUsageDisposition``,
        not the legacy ``UsageTrustLevel``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(task=task, provider_metadata=None),
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            # R7 P0-6: source_id must match the deterministic invoker's
            # bound_source_ids.
            usage_provenance=UsageProvenance(
                source_id="deterministic_fake_invoker",
                tokens_verified=False,
                cost_verified=False,
            ),
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_DETERMINISTIC_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        record = accountant._attempt_records[-1]
        assert isinstance(record, AttemptUsageRecord)
        assert isinstance(record.token_disposition, AttemptUsageDisposition)
        assert isinstance(record.cost_disposition, AttemptUsageDisposition)


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------


class _NoopHandler:
    async def run(
        self, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentResult:  # pragma: no cover
        raise RuntimeError("noop handler should not be called")


class _ProviderResultHandler:
    """Handler that returns a result with provider_metadata and token_usage."""

    def __init__(self, *, tokens: int = 100) -> None:
        self._tokens = tokens

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
