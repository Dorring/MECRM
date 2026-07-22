"""Phase 4 R10 regression tests.

Direct counter-examples for the five P0 concerns, one P1 cleanup item,
and four horizontal sync requirements identified in the Phase 4 R9
review, addressed by R10:

* **P0-1** — Failure Outcome ``NO_PROVIDER_CALL`` must validate the
  Invoker's frozen ``never_calls_provider`` capability.  A Live/Hybrid
  Invoker cannot self-attest ``NO_PROVIDER_CALL`` on the failure path.

* **P0-2** — Failure Outcome ``observed_tool_calls`` is constrained to
  ``ge=0`` and, when a Result is present, MUST equal
  ``len(result.tool_calls)``.  No under-reporting, negative-reporting,
  or hiding of tool calls.

* **P0-3** — Unified ``record_invocation_outcome`` preserves VERIFIED
  usage from a failure Outcome (e.g. Token=VERIFIED + Cost=UNAVAILABLE).

* **P0-4** — Failed/Degraded ``TaskAttemptRecord`` uses the Accountant's
  committed values, NOT the raw Receipt's declared values.

* **P0-5** — Shared ``validate_usage_dimension`` invariants across
  ``AttemptUsageRecord``, ``TaskAttemptRecord``, ``AgentInvocationOutcome``,
  and ``AgentInvocationReceipt``.  The R9 ``value == 0`` carve-out is
  removed.

* **P1-1** — Infrastructure exception audit: an unknown exception
  propagating to the Scheduler still produces an
  ``AttemptUsageRecord`` (UNAVAILABLE + infrastructure_exception).

* **Sync 1** — Public exports: Usage types come from
  :mod:`multi_agent.usage`, Invocation types from
  :mod:`multi_agent.invocation`.

* **Sync 2** — ``ExecutionUsage`` forward reference resolves regardless
  of import order.

* **Sync 3** — RunStore cache round-trip preserves usage audit
  (dispositions, Decimal cost, None tool calls, mixed usage).

* **Sync 4** — LangGraph Adapter propagates usage audit without
  duplicating Accountant logic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from multi_agent.complexity_gate import ComplexityDecision
from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentError,
    AgentErrorCategory,
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
    ExecutionTraceEvent,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TaskExecutionRecord,
)
from multi_agent.execution_errors import ExecutionUsageUnavailableError
from multi_agent.invocation import (
    AgentInvocationOutcome,
    AgentInvocationReceipt,
)
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    compute_request_hash,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.run_store import InMemoryRunStore
from multi_agent.serialization import deserialize_contract, serialize_contract
from multi_agent.state import MergedState
from multi_agent.supervisor import _BudgetAccountant
from multi_agent.supervisor_graph import (
    FakeSupervisorRuntime,
    SupervisorGraphState,
    build_supervisor_graph,
)
from multi_agent.usage import (
    ERROR_EXECUTION_USAGE_UNAVAILABLE,
    AttemptUsageDisposition,
    AttemptUsageRecord,
    UsageProvenance,
    UsageVerificationCapabilities,
    VerifiedUsage,
    validate_usage_dimension,
)


# ---------------------------------------------------------------------------
# Shared helpers (copied from test_supervisor_r9.py)
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
    # AgentResult validator requires at least one error when status="failed".
    if status == "failed" and not errors:
        errors = [
            AgentError(
                error_code="test_failure",
                message="test failure",
                category=AgentErrorCategory.UNKNOWN,
                retryable=False,
            )
        ]
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


class _NoopHandler:
    async def run(
        self, task: AgentTask, ctx: AgentExecutionContext
    ) -> AgentResult:  # pragma: no cover
        raise RuntimeError("noop handler should not be called")


def _three_independent_caps() -> list[AgentCapability]:
    return [
        _make_capability(
            "agent_a",
            frozenset({"test"}),
            frozenset({"root_task"}),
            frozenset({"tool.read"}),
        ),
    ]


def _three_independent_catalog() -> ToolCatalog:
    return ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
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
)

_LIVE_INVOKER_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="live_invoker",
    never_calls_provider=False,
)

_HYBRID_INVOKER_CAPS = UsageVerificationCapabilities(
    verifies_tokens=True,
    verifies_cost=True,
    source_id="hybrid_invoker",
    never_calls_provider=False,
    bound_token_source_ids=frozenset({"hybrid_invoker"}),
    bound_cost_source_ids=frozenset({"hybrid_invoker"}),
)


# ===========================================================================
# Group 1: Failure NO_PROVIDER_CALL requires never_calls_provider (P0-1)
# ===========================================================================


class TestFailureNoProviderCallRequiresCapability:
    """R10 P0-1: a failure Outcome declaring ``NO_PROVIDER_CALL`` must
    be validated against the Invoker's frozen
    ``never_calls_provider`` capability.  Live/Hybrid Invokers cannot
    self-attest ``NO_PROVIDER_CALL`` on the failure path."""

    def test_failure_no_provider_call_requires_never_calls_provider(self):
        """A deterministic Invoker (never_calls_provider=True) CAN
        attest ``NO_PROVIDER_CALL`` on the failure path — the outcome
        is accepted by ``record_invocation_outcome``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="deterministic_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        # Deterministic Invoker — should succeed.
        accountant.record_invocation_outcome(
            outcome,
            invoker_capabilities=_DETERMINISTIC_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL
        assert record.cost_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL

    def test_live_invoker_cannot_attest_no_provider_call_on_failure(self):
        """A Live Invoker (never_calls_provider=False) CANNOT attest
        ``NO_PROVIDER_CALL`` — ``record_invocation_outcome`` raises
        ``ExecutionUsageUnavailableError``."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="live_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_invocation_outcome(
                outcome,
                invoker_capabilities=_LIVE_INVOKER_CAPS,
                task_id=task.task_id,
                attempt=0,
            )

    def test_hybrid_invoker_no_call_outcome_rejected(self):
        """A Hybrid Invoker (never_calls_provider=False, verifies both)
        cannot attest ``NO_PROVIDER_CALL`` on the failure path."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="hybrid_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_invocation_outcome(
                outcome,
                invoker_capabilities=_HYBRID_INVOKER_CAPS,
                task_id=task.task_id,
                attempt=0,
            )

    def test_failure_outcome_checked_against_frozen_capabilities(self):
        """``record_usage_unavailable`` with an outcome declaring
        ``NO_PROVIDER_CALL`` from a Live Invoker falls back to
        ``UNAVAILABLE`` — the disposition is silently downgraded."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="live_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        # record_usage_unavailable does NOT raise — it downgrades.
        accountant.record_usage_unavailable(
            task_id=task.task_id,
            attempt=0,
            invoker_capabilities=_LIVE_INVOKER_CAPS,
            outcome=outcome,
        )
        record = accountant.last_attempt_record
        assert record is not None
        # NO_PROVIDER_CALL was downgraded to UNAVAILABLE.
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE


# ===========================================================================
# Group 2: Failure Tool Usage validation (P0-2)
# ===========================================================================


class TestFailureToolUsageValidation:
    """R10 P0-2: ``observed_tool_calls`` is constrained to ``ge=0`` and,
    when a Result is present, MUST equal ``len(result.tool_calls)``."""

    def test_failure_outcome_rejects_negative_tool_calls(self):
        """``observed_tool_calls=-1`` is rejected by the ``ge=0``
        constraint — a failure Outcome cannot negative-report tool
        calls."""
        with pytest.raises(ValidationError) as exc_info:
            AgentInvocationOutcome(
                error_code="failure",
                observed_tool_calls=-1,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            )
        assert (
            "ge" in str(exc_info.value).lower()
            or "greater" in str(exc_info.value).lower()
        )

    def test_failure_outcome_tool_count_matches_result(self):
        """When a Result is present, ``observed_tool_calls`` MUST equal
        ``len(result.tool_calls)``.  Under-reporting is rejected."""
        task = _make_task()
        result = _ok_result(
            task=task,
            tool_calls=[
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
            ],
        )
        # Correct count — accepted.
        outcome_ok = AgentInvocationOutcome(
            result=result,
            error_code=None,
            observed_tool_calls=2,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        assert outcome_ok.observed_tool_calls == 2

        # Under-reporting — rejected.
        with pytest.raises(ValidationError) as exc_info:
            AgentInvocationOutcome(
                result=result,
                error_code="failure",
                observed_tool_calls=0,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            )
        assert "observed_tool_calls" in str(exc_info.value)

    def test_failure_outcome_none_tool_calls_with_result_is_rejected(self):
        """R10.1 P1-3: when a Result is present, ``observed_tool_calls``
        MUST be non-None.  ``None`` (unknown) is no longer accepted
        when the Result is available — the Invoker MUST report the
        exact count from ``len(result.tool_calls)``."""
        task = _make_task()
        result = _ok_result(
            task=task,
            tool_calls=[
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
            ],
        )
        with pytest.raises(ValidationError, match="observed_tool_calls is None"):
            AgentInvocationOutcome(
                result=result,
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            )

    def test_hidden_failure_tool_calls_cannot_preserve_budget(self):
        """A failure Outcome with ``observed_tool_calls=0`` but a Result
        containing 2 tool calls is rejected — the Invoker cannot hide
        tool calls to preserve the tool-call budget."""
        task = _make_task()
        result = _ok_result(
            task=task,
            tool_calls=[
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
            ],
        )
        with pytest.raises(ValidationError):
            AgentInvocationOutcome(
                result=result,
                error_code="failure",
                observed_tool_calls=0,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            )


# ===========================================================================
# Group 3: Unified Failure Outcome Accounting (P0-3)
# ===========================================================================


class TestUnifiedFailureOutcomeAccounting:
    """R10 P0-3: ``record_invocation_outcome`` preserves VERIFIED usage
    from a failure Outcome, following the same three-phase pipeline as
    ``record_receipt``."""

    def test_failed_outcome_preserves_verified_tokens(self):
        """A failure Outcome with Token=VERIFIED + Cost=UNAVAILABLE
        preserves the verified token usage — the value is NOT
        discarded."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="partial_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=100,
            cost_usd=None,
            token_source_id="token_verifier",
            cost_source_id=None,
        )
        accountant.record_invocation_outcome(
            outcome,
            invoker_capabilities=_TOKEN_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # Token VERIFIED usage is preserved.
        assert accountant._tokens_used == 100
        assert accountant._verified_token_attempts == 1
        # Cost UNAVAILABLE triggers fail-closed.
        assert accountant.usage_unavailable
        assert accountant.exceeded
        assert accountant.exceeded_reason == ERROR_EXECUTION_USAGE_UNAVAILABLE
        # Record has mixed dispositions.
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.VERIFIED
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.tokens_used == 100
        assert record.cost_usd is None
        assert record.token_source_id == "token_verifier"
        assert record.cost_source_id is None

    def test_failed_outcome_preserves_verified_cost(self):
        """A failure Outcome with Token=UNAVAILABLE + Cost=VERIFIED
        preserves the verified cost usage."""
        budget = ExecutionBudget(token_budget=1000)
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="partial_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.VERIFIED,
            tokens_used=None,
            cost_usd=Decimal("0.50"),
            token_source_id=None,
            cost_source_id="cost_verifier",
        )
        accountant.record_invocation_outcome(
            outcome,
            invoker_capabilities=_COST_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # Cost VERIFIED usage is preserved.
        assert accountant._cost_usd == Decimal("0.50")
        assert accountant._verified_cost_attempts == 1
        # Token UNAVAILABLE triggers fail-closed.
        assert accountant.usage_unavailable
        assert accountant.exceeded
        # Record has mixed dispositions.
        record = accountant.last_attempt_record
        assert record is not None
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.VERIFIED
        assert record.tokens_used is None
        assert record.cost_usd == Decimal("0.50")

    def test_failed_mixed_usage_is_committed_before_fail_closed(self):
        """The mixed-dimension record is committed BEFORE fail-closed
        is triggered — commit-then-check semantics."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="partial_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=100,
            cost_usd=None,
            token_source_id="token_verifier",
            cost_source_id=None,
        )
        accountant.record_invocation_outcome(
            outcome,
            invoker_capabilities=_TOKEN_VERIFIER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        # The record IS committed (not rolled back).
        assert accountant.last_attempt_record is not None
        assert accountant.last_attempt_record.tokens_used == 100
        # Fail-closed happened AFTER the commit.
        assert accountant.exceeded

    def test_failed_outcome_validates_source_binding(self):
        """A failure Outcome with Token=VERIFIED but a source_id NOT in
        the Invoker's ``bound_token_source_ids`` is rejected."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="partial_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=100,
            cost_usd=None,
            token_source_id="rogue_verifier",  # NOT in bound_token_source_ids
            cost_source_id=None,
        )
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_invocation_outcome(
                outcome,
                invoker_capabilities=_TOKEN_VERIFIER_CAPS,
                task_id=task.task_id,
                attempt=0,
            )

    def test_failed_outcome_verifies_tokens_capability_required(self):
        """A failure Outcome with Token=VERIFIED from an Invoker that
        does NOT have ``verifies_tokens=True`` is rejected — the
        Outcome cannot self-elevate trust."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        outcome = AgentInvocationOutcome(
            error_code="partial_failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=100,
            cost_usd=None,
            token_source_id="live_invoker",
            cost_source_id=None,
        )
        # _LIVE_INVOKER_CAPS has verifies_tokens=False.
        with pytest.raises(ExecutionUsageUnavailableError):
            accountant.record_invocation_outcome(
                outcome,
                invoker_capabilities=_LIVE_INVOKER_CAPS,
                task_id=task.task_id,
                attempt=0,
            )


# ===========================================================================
# Group 4: Failed/Degraded TaskAttemptRecord (P0-4)
# ===========================================================================


class TestFailedDegradedAttemptRecord:
    """R10 P0-4: Failed/Degraded ``TaskAttemptRecord`` uses the
    Accountant's committed values, NOT the raw Receipt's declared
    values."""

    def test_failed_result_does_not_publish_unverified_tokens(self):
        """When a Handler self-reports ``TokenUsage(total_tokens=500)``
        but the Invoker boundary cannot verify it (UNAVAILABLE), the
        Accountant's record and the TaskAttemptRecord must have
        ``tokens_used=None`` — NOT 500.

        R10 P0-5: the Receipt itself must be internally consistent
        (UNAVAILABLE → value=None), so the Handler's self-reported
        ``token_usage`` in the Result is the only place 500 appears.
        The Accountant must NOT leak that self-report into the
        committed usage record."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                status="failed",
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=250, output_tokens=250, total_tokens=500
                ),
                errors=[],
            ),
            tool_calls=0,
            # R10 P0-5: UNAVAILABLE requires value=None — the Invoker
            # boundary cannot attest the Handler's self-reported 500.
            tokens_used=None,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="handler",
                tokens_verified=False,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        accountant.record_receipt(
            receipt,
            invoker_capabilities=_LIVE_INVOKER_CAPS,
            task_id=task.task_id,
            attempt=0,
        )
        last_usage = accountant.last_attempt_record
        assert last_usage is not None
        # The Accountant's committed values are None (UNAVAILABLE) —
        # the Handler's self-reported 500 does NOT leak through.
        assert last_usage.tokens_used is None
        assert last_usage.cost_usd is None
        # The TaskAttemptRecord built from these values must also be None.
        attempt_actual_tokens = (
            last_usage.tokens_used
            if last_usage.token_disposition == AttemptUsageDisposition.VERIFIED
            else None
        )
        attempt_actual_cost = (
            last_usage.cost_usd
            if last_usage.cost_disposition == AttemptUsageDisposition.VERIFIED
            else None
        )
        assert attempt_actual_tokens is None
        assert attempt_actual_cost is None

    def test_degraded_result_preserves_verified_usage_audit(self):
        """When a Receipt has verified tokens and the result status is
        ``degraded``, the TaskAttemptRecord's ``tokens_used`` must
        reflect the VERIFIED value — NOT None."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                status="degraded",
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(
                    input_tokens=50, output_tokens=50, total_tokens=100
                ),
                errors=[],
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
        last_usage = accountant.last_attempt_record
        assert last_usage is not None
        # VERIFIED token usage is preserved even for degraded results.
        assert last_usage.token_disposition == AttemptUsageDisposition.VERIFIED
        assert last_usage.tokens_used == 100
        # The TaskAttemptRecord's actual tokens must reflect the VERIFIED value.
        attempt_actual_tokens = (
            last_usage.tokens_used
            if last_usage.token_disposition == AttemptUsageDisposition.VERIFIED
            else None
        )
        assert attempt_actual_tokens == 100

    def test_failed_attempt_usage_matches_accountant_record(self):
        """The TaskAttemptRecord's dispositions and source IDs must
        match the Accountant's last committed AttemptUsageRecord —
        no duplicated disposition decision logic."""
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()
        receipt = AgentInvocationReceipt(
            result=_ok_result(
                task=task,
                status="failed",
                provider_metadata=_provider_meta(),
                token_usage=TokenUsage(total_tokens=100),
            ),
            tool_calls=0,
            tokens_used=100,
            # R10 P0-5: UNAVAILABLE requires value=None.
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
        last_usage = accountant.last_attempt_record
        assert last_usage is not None
        # The TaskAttemptRecord fields must match the Accountant's record.
        assert last_usage.token_disposition == AttemptUsageDisposition.VERIFIED
        assert last_usage.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert last_usage.token_source_id == "token_verifier"
        assert last_usage.cost_source_id is None
        assert last_usage.tokens_used == 100
        assert last_usage.cost_usd is None

    def test_failed_result_disposition_and_value_are_consistent(self):
        """A failed result's TaskAttemptRecord must NOT have
        ``token_disposition=UNAVAILABLE`` with ``tokens_used=500`` —
        that is a P0-5 invariant violation."""
        # This is enforced by the TaskAttemptRecord model_validator.
        with pytest.raises(ValidationError):
            TaskAttemptRecord(
                task_id="task_a",
                agent_id="agent_a",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="failed",
                duration_ms=10,
                error_code="some_error",
                agent_calls=1,
                tool_calls=0,
                tokens_used=500,  # Non-None value
                cost_usd=None,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,  # But UNAVAILABLE
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                token_source_id=None,
                cost_source_id=None,
            )


# ===========================================================================
# Group 5: Shared Usage Dimension Invariants (P0-5)
# ===========================================================================


class TestSharedUsageDimensionInvariants:
    """R10 P0-5: the shared ``validate_usage_dimension`` function
    enforces the SAME invariants across all four contracts.  The R9
    ``value == 0`` carve-out is REMOVED."""

    def test_unavailable_rejects_zero_actual_value(self):
        """``UNAVAILABLE`` with ``value=0`` is rejected — ``0`` is a
        real value, not None/unknown."""
        # AttemptUsageRecord
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="task_a",
                attempt=0,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=0,  # R10: rejected
                cost_usd=None,
            )
        # Direct function call
        with pytest.raises(ValueError):
            validate_usage_dimension(
                "token",
                AttemptUsageDisposition.UNAVAILABLE,
                0,
                None,
            )

    def test_no_provider_call_rejects_zero_actual_value(self):
        """``NO_PROVIDER_CALL`` with ``value=0`` is rejected — no
        provider call means no usage, not even zero."""
        with pytest.raises(ValidationError):
            AttemptUsageRecord(
                task_id="task_a",
                attempt=0,
                token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                tokens_used=0,  # R10: rejected
                cost_usd=None,
            )
        with pytest.raises(ValueError):
            validate_usage_dimension(
                "token",
                AttemptUsageDisposition.NO_PROVIDER_CALL,
                0,
                None,
            )

    def test_task_attempt_usage_invariants(self):
        """``TaskAttemptRecord`` enforces the shared per-dimension
        invariants via its model_validator."""
        # VERIFIED requires non-None value + source.
        with pytest.raises(ValidationError):
            TaskAttemptRecord(
                task_id="task_a",
                agent_id="agent_a",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="completed",
                duration_ms=10,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=None,  # Missing value
                cost_usd=None,
                token_source_id="verifier",
                cost_source_id=None,
            )
        # VERIFIED requires source_id.
        with pytest.raises(ValidationError):
            TaskAttemptRecord(
                task_id="task_a",
                agent_id="agent_a",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="completed",
                duration_ms=10,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=100,
                cost_usd=None,
                token_source_id=None,  # Missing source
                cost_source_id=None,
            )

    def test_outcome_usage_invariants(self):
        """``AgentInvocationOutcome`` enforces the shared per-dimension
        invariants via its model_validator."""
        # VERIFIED requires non-None value.
        with pytest.raises(ValidationError):
            AgentInvocationOutcome(
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=None,  # Missing value
                cost_usd=None,
                token_source_id="verifier",
                cost_source_id=None,
            )
        # NO_PROVIDER_CALL rejects non-None value.
        with pytest.raises(ValidationError):
            AgentInvocationOutcome(
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=0,  # Rejected
                cost_usd=None,
            )

    def test_all_usage_contracts_share_same_dimension_rules(self):
        """All four contracts (AttemptUsageRecord, TaskAttemptRecord,
        AgentInvocationOutcome, AgentInvocationReceipt) use the SAME
        ``validate_usage_dimension`` function — no drift."""
        # The same violation (UNAVAILABLE + value=0) must be rejected
        # by ALL four contracts.
        for contract_factory in [
            lambda: AttemptUsageRecord(
                task_id="t",
                attempt=0,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=0,
                cost_usd=None,
            ),
            lambda: TaskAttemptRecord(
                task_id="t",
                agent_id="a",
                attempt=0,
                started_at=_FIXED_TS,
                completed_at=_FIXED_TS,
                status="completed",
                duration_ms=0,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=0,
                cost_usd=None,
            ),
            lambda: AgentInvocationOutcome(
                error_code="x",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=0,
                cost_usd=None,
            ),
        ]:
            with pytest.raises(ValidationError):
                contract_factory()

    def test_verified_zero_is_accepted(self):
        """``VERIFIED`` with ``value=0`` is ACCEPTED — ``0`` is a
        legitimate verified value (e.g. a cached call)."""
        record = AttemptUsageRecord(
            task_id="t",
            attempt=0,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=0,
            cost_usd=None,
            token_source_id="verifier",
            cost_source_id=None,
        )
        assert record.tokens_used == 0
        assert record.token_disposition == AttemptUsageDisposition.VERIFIED


# ===========================================================================
# Group 6: Infrastructure Exception Audit (P1-1)
# ===========================================================================


class TestInfrastructureExceptionAudit:
    """R10 P1-1: an unknown exception propagating to the Scheduler
    still produces an ``AttemptUsageRecord`` (UNAVAILABLE +
    infrastructure_exception)."""

    def test_unknown_exception_still_creates_usage_audit_record(self):
        """When an unknown exception is raised after
        ``commit_agent_call``, the ``except Exception`` clause records
        an UNAVAILABLE usage record before re-raising.

        This test verifies the Accountant-level behavior: calling
        ``record_usage_unavailable`` after ``commit_agent_call``
        produces exactly one AttemptUsageRecord with UNAVAILABLE
        dispositions."""
        budget = ExecutionBudget(token_budget=1000, cost_budget_usd=Decimal("10.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        task = _make_task()

        # Simulate: commit agent call, then infrastructure exception.
        permit = accountant.issue_permit(task.task_id)
        accountant.commit_agent_call(permit)
        assert accountant._agent_calls == 1

        # The infrastructure exception path records UNAVAILABLE usage.
        accountant.record_observed_tool_calls(None)
        accountant.record_usage_unavailable(
            task_id=task.task_id,
            attempt=0,
        )

        # Every committed call produces exactly one AttemptUsageRecord.
        assert accountant.last_attempt_record is not None
        record = accountant.last_attempt_record
        assert record.token_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert record.tokens_used is None
        assert record.cost_usd is None
        assert record.token_source_id is None
        assert record.cost_source_id is None

        # Tool usage is unknown — fail-closed.
        assert accountant.tool_usage_unavailable
        assert accountant.usage_unavailable
        assert accountant.exceeded
        assert accountant.exceeded_reason == ERROR_EXECUTION_USAGE_UNAVAILABLE

    def test_infrastructure_exception_audit_is_best_effort(self):
        """The infrastructure exception audit is best-effort — if the
        recording itself fails, the original exception must still
        propagate.  This is verified at the code level: the ``except
        Exception`` clause wraps the recording in a try/except."""
        # This is a structural test — we verify that the
        # record_usage_unavailable call does NOT raise when given
        # minimal arguments (no task_id/attempt), which would be the
        # case if the audit code path encountered an internal error.
        budget = ExecutionBudget()
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        # This should NOT raise — it just doesn't create a record.
        accountant.record_usage_unavailable()
        assert accountant.last_attempt_record is None


# ===========================================================================
# Group 7: Public Export Source Uniqueness (Sync 1)
# ===========================================================================


class TestPublicExportSource:
    """R10 Sync 1: Usage types come from :mod:`multi_agent.usage`,
    Invocation types from :mod:`multi_agent.invocation`."""

    def test_every_public_export_resolves(self):
        """Every name in ``multi_agent.__all__`` is a resolvable
        attribute of the ``multi_agent`` package."""
        import multi_agent

        for name in multi_agent.__all__:
            assert hasattr(multi_agent, name), (
                f"multi_agent.{name} is in __all__ but not resolvable"
            )

    def test_usage_exports_come_from_usage_module(self):
        """Usage types imported from ``multi_agent`` are the SAME
        objects as those imported from ``multi_agent.usage`` —
        ``is`` identity, not just equality."""
        import multi_agent
        from multi_agent import usage as usage_mod

        for name in (
            "AttemptUsageDisposition",
            "AttemptUsageRecord",
            "UsageProvenance",
            "UsageVerificationCapabilities",
            "VerifiedUsage",
            "ProviderUsageVerifier",
            "UsageTrustLevel",
            "get_usage_capabilities",
            "validate_usage_dimension",
            "ERROR_TOOL_USAGE_UNAVAILABLE",
            "ERROR_EXECUTION_USAGE_UNAVAILABLE",
            "ERROR_INVALID_INVOCATION_OUTCOME",
            "ERROR_INFRASTRUCTURE_EXCEPTION",
            "ERROR_USAGE_SOURCE_MISMATCH",
        ):
            public_obj = getattr(multi_agent, name)
            canonical_obj = getattr(usage_mod, name)
            assert public_obj is canonical_obj, (
                f"multi_agent.{name} is not multi_agent.usage.{name} — "
                f"public export must come from the canonical source"
            )

    def test_invocation_exports_come_from_invocation_module(self):
        """Invocation types imported from ``multi_agent`` are the SAME
        objects as those imported from ``multi_agent.invocation``."""
        import multi_agent
        from multi_agent import invocation as inv_mod

        for name in (
            "AgentInvocationReceipt",
            "AgentInvocationFailure",
            "AgentInvocationOutcome",
            "AgentInvoker",
            "DeterministicFakeInvoker",
            "RegistryAgentInvoker",
            "validate_invocation_receipt",
        ):
            public_obj = getattr(multi_agent, name)
            canonical_obj = getattr(inv_mod, name)
            assert public_obj is canonical_obj, (
                f"multi_agent.{name} is not multi_agent.invocation.{name}"
            )


# ===========================================================================
# Group 8: ExecutionUsage Forward Reference (Sync 2)
# ===========================================================================


class TestExecutionUsageForwardReference:
    """R10 Sync 2: ``ExecutionUsage`` forward reference resolves
    regardless of import order.  The ``model_rebuild()`` call at the
    bottom of :mod:`multi_agent.usage` is a deliberate initialization
    point, not an accidental side effect."""

    def test_execution_usage_resolves_when_contracts_imported_first(self):
        """Importing ``contracts`` before ``usage`` works — the forward
        reference is resolved by ``usage.model_rebuild()``."""
        # We're already in a process where both are imported, so we
        # verify the annotation is resolved.
        from multi_agent.contracts import ExecutionUsage

        # Access the field annotation — this would raise if the forward
        # reference were unresolved.
        field_info = ExecutionUsage.model_fields["attempt_usage_records"]
        assert field_info is not None

    def test_execution_usage_resolves_when_usage_imported_first(self):
        """Importing ``usage`` before ``contracts`` works — the
        ``model_rebuild()`` at the bottom of ``usage`` runs after
        ``contracts`` is loaded (via ``usage``'s top-level import)."""
        # We verify by constructing an ExecutionUsage with
        # attempt_usage_records — this would fail if the forward
        # reference were unresolved.
        from multi_agent.contracts import ExecutionUsage

        record = AttemptUsageRecord(
            task_id="t",
            attempt=0,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        usage = ExecutionUsage(attempt_usage_records=[record])
        assert len(usage.attempt_usage_records) == 1
        assert usage.attempt_usage_records[0].task_id == "t"

    def test_execution_usage_json_schema_builds(self):
        """``ExecutionUsage.model_json_schema()`` succeeds — the forward
        reference is fully resolved and Pydantic can build the schema."""
        from multi_agent.contracts import ExecutionUsage

        schema = ExecutionUsage.model_json_schema()
        assert "attempt_usage_records" in schema["properties"]

    def test_execution_usage_model_validate_round_trip(self):
        """``ExecutionUsage`` can validate a dict with
        ``attempt_usage_records`` and the records survive the round
        trip."""
        from multi_agent.contracts import ExecutionUsage

        record = AttemptUsageRecord(
            task_id="t",
            attempt=0,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=100,
            cost_usd=None,
            token_source_id="verifier",
            cost_source_id=None,
        )
        usage = ExecutionUsage(attempt_usage_records=[record])
        # Serialize → deserialize round trip.
        data = usage.model_dump()
        usage2 = ExecutionUsage.model_validate(data)
        assert len(usage2.attempt_usage_records) == 1
        assert usage2.attempt_usage_records[0].tokens_used == 100
        assert (
            usage2.attempt_usage_records[0].token_disposition
            == AttemptUsageDisposition.VERIFIED
        )

    def test_independent_process_import_order_contracts_first(self):
        """In a fresh Python process, importing ``contracts`` first
        then ``usage`` resolves the forward reference."""
        code = (
            "import multi_agent.contracts; "
            "import multi_agent.usage; "
            "from multi_agent.contracts import ExecutionUsage; "
            "from multi_agent.usage import AttemptUsageRecord; "
            "r = AttemptUsageRecord(task_id='t', attempt=0, "
            "token_disposition='unavailable', cost_disposition='unavailable'); "
            "u = ExecutionUsage(attempt_usage_records=[r]); "
            "assert len(u.attempt_usage_records) == 1; "
            "print('OK')"
        )
        src_path = str(Path(__file__).resolve().parents[3] / "src")
        env = os.environ.copy()
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=r"e:\M-Agent-ECRM\agents",
            env=env,
        )
        assert result.returncode == 0, f"contracts-first import failed: {result.stderr}"

    def test_independent_process_import_order_usage_first(self):
        """In a fresh Python process, importing ``usage`` first then
        ``contracts`` resolves the forward reference."""
        code = (
            "import multi_agent.usage; "
            "import multi_agent.contracts; "
            "from multi_agent.contracts import ExecutionUsage; "
            "from multi_agent.usage import AttemptUsageRecord; "
            "r = AttemptUsageRecord(task_id='t', attempt=0, "
            "token_disposition='unavailable', cost_disposition='unavailable'); "
            "u = ExecutionUsage(attempt_usage_records=[r]); "
            "assert len(u.attempt_usage_records) == 1; "
            "print('OK')"
        )
        src_path = str(Path(__file__).resolve().parents[3] / "src")
        env = os.environ.copy()
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=r"e:\M-Agent-ECRM\agents",
            env=env,
        )
        assert result.returncode == 0, f"usage-first import failed: {result.stderr}"


# ===========================================================================
# Group 9: RunStore Cache Round-trip (Sync 3)
# ===========================================================================


def _make_usage_with_mixed_records() -> ExecutionUsage:
    """Build an ExecutionUsage with mixed-dimension attempt records."""
    record1 = AttemptUsageRecord(
        task_id="task_a",
        attempt=0,
        token_disposition=AttemptUsageDisposition.VERIFIED,
        cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        tokens_used=100,
        cost_usd=None,
        token_source_id="token_verifier",
        cost_source_id=None,
    )
    record2 = AttemptUsageRecord(
        task_id="task_b",
        attempt=0,
        token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
        tokens_used=None,
        cost_usd=None,
    )
    return ExecutionUsage(
        tasks_dispatched=2,
        agent_calls=2,
        tool_calls=3,
        tokens_used=100,
        cost_usd=Decimal("0.50"),
        token_usage_applicable_attempts=1,
        cost_usage_applicable_attempts=1,
        verified_token_attempts=1,
        verified_cost_attempts=0,
        attempt_usage_records=[record1, record2],
        tool_usage_unavailable=False,
        elapsed_ms=500,
    )


def _make_result_with_usage(
    *,
    run_id: str = "run-001",
    status: SupervisorRunStatus = SupervisorRunStatus.COMPLETED,
    usage: ExecutionUsage | None = None,
) -> SupervisorRunResult:
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
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=100,
                cost_usd=None,
                token_source_id="token_verifier",
                cost_source_id=None,
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
        plan_hash="a" * 64,
        registry_version="reg-v-001",
        status=status,
        task_records=[task_record],
        merged_state=MergedState(),
        usage=usage or _make_usage_with_mixed_records(),
        trace=[trace_event],
        started_at=_FIXED_TS,
        completed_at=_FIXED_TS,
        duration_ms=10,
    )


class TestRunStoreCacheRoundTrip:
    """R10 Sync 3: RunStore cache round-trip preserves usage audit —
    dispositions, Decimal cost, None tool calls, mixed usage all
    survive serialization and defensive copy."""

    @pytest.mark.asyncio
    async def test_cached_result_preserves_attempt_usage_records(self):
        """The cached result's ``attempt_usage_records`` list survives
        the ``complete`` → ``begin`` round-trip with full type
        fidelity."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result_with_usage()
        await store.complete(lease, result)

        lease2 = await store.begin("run-001", "a" * 64)
        assert lease2.is_cached
        assert lease2.cached_result is not None
        cached = lease2.cached_result
        assert len(cached.usage.attempt_usage_records) == 2
        record = cached.usage.attempt_usage_records[0]
        assert record.token_disposition == AttemptUsageDisposition.VERIFIED
        assert record.tokens_used == 100
        assert record.token_source_id == "token_verifier"

    @pytest.mark.asyncio
    async def test_cached_result_preserves_mixed_usage_dispositions(self):
        """Mixed-dimension dispositions (VERIFIED + UNAVAILABLE,
        NO_PROVIDER_CALL) survive the cache round-trip."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result_with_usage()
        await store.complete(lease, result)

        lease2 = await store.begin("run-001", "a" * 64)
        cached = lease2.cached_result
        assert cached is not None
        records = cached.usage.attempt_usage_records
        # Record 0: Token=VERIFIED + Cost=UNAVAILABLE
        assert records[0].token_disposition == AttemptUsageDisposition.VERIFIED
        assert records[0].cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        # Record 1: both NO_PROVIDER_CALL
        assert records[1].token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL
        assert records[1].cost_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL

    @pytest.mark.asyncio
    async def test_cached_result_preserves_unknown_tool_usage(self):
        """``tool_usage_unavailable=True`` survives the cache
        round-trip."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        usage = _make_usage_with_mixed_records()
        usage.tool_usage_unavailable = True
        result = _make_result_with_usage(usage=usage)
        await store.complete(lease, result)

        lease2 = await store.begin("run-001", "a" * 64)
        cached = lease2.cached_result
        assert cached is not None
        assert cached.usage.tool_usage_unavailable is True

    @pytest.mark.asyncio
    async def test_cached_result_is_deep_copy_after_usage_schema_change(self):
        """The cached result is a deep copy — mutating it does NOT
        affect the store's internal state.

        ``AttemptUsageRecord`` is a frozen model, so we mutate the
        non-frozen ``ExecutionUsage`` fields (``tool_calls``) and
        replace a record in the list to verify deep-copy semantics."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result_with_usage()
        await store.complete(lease, result)

        lease2 = await store.begin("run-001", "a" * 64)
        cached = lease2.cached_result
        assert cached is not None
        # Mutate the non-frozen ExecutionUsage fields.
        cached.usage.tool_calls = 999
        # Replace a record in the list (list mutation, not frozen-model mutation).
        original_record = cached.usage.attempt_usage_records[0]
        cached.usage.attempt_usage_records[0] = AttemptUsageRecord(
            task_id=original_record.task_id,
            attempt=original_record.attempt,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=999,
            cost_usd=None,
            token_source_id="token_verifier",
            cost_source_id=None,
        )

        # The store's internal state is NOT affected.
        lease3 = await store.begin("run-001", "a" * 64)
        assert lease3.cached_result is not None
        assert lease3.cached_result.usage.attempt_usage_records[0].tokens_used == 100
        assert lease3.cached_result.usage.tool_calls == 3

    @pytest.mark.asyncio
    async def test_supervisor_result_json_round_trip_preserves_usage_audit(self):
        """``model_dump()`` → ``model_validate()`` round-trip
        preserves Decimal cost, StrEnum disposition, None tool_calls,
        and source IDs."""
        result = _make_result_with_usage()
        data = result.model_dump()
        result2 = SupervisorRunResult.model_validate(data)

        # Decimal cost is preserved (not degraded to float).
        assert isinstance(result2.usage.cost_usd, Decimal)
        assert result2.usage.cost_usd == Decimal("0.50")
        # StrEnum disposition is preserved.
        assert (
            result2.usage.attempt_usage_records[0].token_disposition
            == AttemptUsageDisposition.VERIFIED
        )
        assert isinstance(
            result2.usage.attempt_usage_records[0].token_disposition,
            AttemptUsageDisposition,
        )
        # None tool_calls is preserved.
        assert result2.usage.attempt_usage_records[1].tokens_used is None
        # Source IDs are preserved.
        assert (
            result2.usage.attempt_usage_records[0].token_source_id == "token_verifier"
        )


# ===========================================================================
# Group 10: LangGraph Adapter Propagation (Sync 4)
# ===========================================================================


def _make_registry_for_graph() -> AgentRegistry:
    return AgentRegistry(
        tool_catalog=ToolCatalog(
            [
                ToolDescriptor(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                )
            ]
        )
    )


def _make_plan_for_graph() -> PlanDraft:
    task = AgentTask(
        task_id="task-001",
        agent_id="agent_001",
        task_type="root_task",
        objective="test",
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
        run_id="run-001",
        tenant_id="t-001",
        actor_type="user",
        actor_id="user-001",
        objective="graph test",
        signals=signals,
        budget=ExecutionBudget(),
        context_summary=None,
        registry_version="reg-v-001",
    )
    complexity = ComplexityDecision(
        route="single_agent",
        domains=["test"],
        reasons=["test"],
        confidence=1.0,
        requires_human_review=False,
    )
    planned = [
        PlannedTask(
            intent_id="intent_a",
            domain="test",
            task=task,
            preferred_authority=AgentAuthority.READ,
            planning_metadata={},
        )
    ]
    return PlanDraft(
        request=request,
        request_hash=compute_request_hash(request),
        complexity=complexity,
        tasks=planned,
        planner_version=PLANNER_VERSION,
    )


class TestLangGraphAdapterPropagation:
    """R10 Sync 4: the LangGraph Adapter propagates usage audit —
    attempt_usage_records, mixed-dimension dispositions,
    tool_usage_unavailable, budget_exceeded reason, and
    infrastructure exception audit — without duplicating Accountant
    logic."""

    @pytest.mark.asyncio
    async def test_graph_preserves_attempt_usage_records(self):
        """The graph returns a result whose ``attempt_usage_records``
        list is fully preserved."""
        registry = _make_registry_for_graph()
        plan = _make_plan_for_graph()
        result = _make_result_with_usage()
        runtime = FakeSupervisorRuntime(result=result)
        graph = build_supervisor_graph(runtime)
        state = SupervisorGraphState(plan=plan, registry=registry)
        output = await graph.ainvoke(state)
        assert output["result"] is not None
        assert len(output["result"].usage.attempt_usage_records) == 2
        assert output["result"].usage.attempt_usage_records[0].tokens_used == 100

    @pytest.mark.asyncio
    async def test_graph_preserves_mixed_usage_audit(self):
        """The graph preserves mixed-dimension dispositions (VERIFIED +
        UNAVAILABLE, NO_PROVIDER_CALL)."""
        registry = _make_registry_for_graph()
        plan = _make_plan_for_graph()
        result = _make_result_with_usage()
        runtime = FakeSupervisorRuntime(result=result)
        graph = build_supervisor_graph(runtime)
        state = SupervisorGraphState(plan=plan, registry=registry)
        output = await graph.ainvoke(state)
        records = output["result"].usage.attempt_usage_records
        assert records[0].token_disposition == AttemptUsageDisposition.VERIFIED
        assert records[0].cost_disposition == AttemptUsageDisposition.UNAVAILABLE
        assert records[1].token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL

    @pytest.mark.asyncio
    async def test_graph_propagates_tool_usage_unavailable(self):
        """The graph propagates ``tool_usage_unavailable=True`` without
        loss."""
        registry = _make_registry_for_graph()
        plan = _make_plan_for_graph()
        usage = _make_usage_with_mixed_records()
        usage.tool_usage_unavailable = True
        result = _make_result_with_usage(usage=usage)
        runtime = FakeSupervisorRuntime(result=result)
        graph = build_supervisor_graph(runtime)
        state = SupervisorGraphState(plan=plan, registry=registry)
        output = await graph.ainvoke(state)
        assert output["result"].usage.tool_usage_unavailable is True

    @pytest.mark.asyncio
    async def test_graph_cached_result_preserves_usage_schema(self):
        """The graph's result can be cached in a RunStore and retrieved
        with full usage schema fidelity."""
        store = InMemoryRunStore()
        lease = await store.begin("run-001", "a" * 64)
        result = _make_result_with_usage()
        await store.complete(lease, result)

        registry = _make_registry_for_graph()
        plan = _make_plan_for_graph()
        lease2 = await store.begin("run-001", "a" * 64)
        cached = lease2.cached_result
        assert cached is not None
        runtime = FakeSupervisorRuntime(result=cached)
        graph = build_supervisor_graph(runtime)
        state = SupervisorGraphState(plan=plan, registry=registry)
        output = await graph.ainvoke(state)
        assert len(output["result"].usage.attempt_usage_records) == 2
        assert output["result"].usage.attempt_usage_records[0].tokens_used == 100

    @pytest.mark.asyncio
    async def test_graph_does_not_duplicate_accountant(self):
        """The graph delegates to the Runtime — it does NOT re-implement
        Accountant logic.  Verified by checking the FakeSupervisorRuntime
        was called exactly once."""
        registry = _make_registry_for_graph()
        plan = _make_plan_for_graph()
        result = _make_result_with_usage()
        runtime = FakeSupervisorRuntime(result=result)
        graph = build_supervisor_graph(runtime)
        state = SupervisorGraphState(plan=plan, registry=registry)
        await graph.ainvoke(state)
        assert len(runtime.calls) == 1


# ===========================================================================
# R10.1 P0-1: Receipt Serialization Round-trip
# ===========================================================================


class TestReceiptSerializationRoundTrip:
    """R10.1 P0-1: ``AgentInvocationReceipt`` must survive a full
    serialization round-trip via ``model_dump()``, ``model_dump_json()``,
    and :func:`serialize_contract` / :func:`deserialize_contract`.

    The root cause was that ``usage_trust`` (legacy, auto-derived) was
    included in the serialized form alongside ``usage_provenance``.  On
    deserialization, the ``_sync_trust_provenance`` validator rejected
    the simultaneous presence of both fields — even though ``usage_trust``
    was internally derived, not explicitly provided by the caller.

    Fix: ``usage_trust`` now has ``exclude=True`` so it never appears
    in ``model_dump()`` / ``model_dump_json()`` / canonical serialization.
    """

    def _make_receipt(self) -> AgentInvocationReceipt:
        task = _make_task()
        result = _ok_result(task=task)
        return AgentInvocationReceipt(
            result=result,
            tool_calls=0,
            tokens_used=None,
            cost_usd=None,
            usage_provenance=UsageProvenance(),
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )

    def test_receipt_model_dump_round_trip(self):
        """``model_dump()`` → ``model_validate()`` succeeds."""
        receipt = self._make_receipt()
        restored = AgentInvocationReceipt.model_validate(
            receipt.model_dump(mode="python")
        )
        assert restored == receipt

    def test_receipt_model_dump_json_round_trip(self):
        """``model_dump_json()`` → ``model_validate_json()`` succeeds."""
        receipt = self._make_receipt()
        restored = AgentInvocationReceipt.model_validate_json(receipt.model_dump_json())
        assert restored == receipt

    def test_receipt_serialize_contract_round_trip(self):
        """``serialize_contract()`` → ``deserialize_contract()`` succeeds."""
        receipt = self._make_receipt()
        raw = serialize_contract(receipt)
        restored = deserialize_contract(raw, AgentInvocationReceipt)
        assert restored == receipt

    def test_legacy_trust_not_serialized(self):
        """``usage_trust`` does NOT appear in any serialization output."""
        receipt = self._make_receipt()
        dump = receipt.model_dump(mode="python")
        assert "usage_trust" not in dump
        json_str = receipt.model_dump_json()
        assert "usage_trust" not in json_str
        canonical = serialize_contract(receipt)
        assert "usage_trust" not in canonical

    def test_explicit_legacy_and_provenance_conflict_rejected(self):
        """Explicitly providing BOTH ``usage_trust`` and
        ``usage_provenance`` is still a ``ValidationError`` — the
        ``exclude=True`` fix does NOT weaken the input validation."""
        task = _make_task()
        result = _ok_result(task=task)
        with pytest.raises(ValidationError, match="simultaneously providing"):
            AgentInvocationReceipt(
                result=result,
                tool_calls=0,
                usage_trust="unverified",
                usage_provenance=UsageProvenance(),
            )

    def test_legacy_trust_input_still_works(self):
        """Providing ONLY ``usage_trust`` (legacy input) is still
        accepted — the field is excluded from OUTPUT, not from INPUT."""
        task = _make_task()
        result = _ok_result(task=task)
        receipt = AgentInvocationReceipt(
            result=result,
            tool_calls=0,
            usage_trust="unverified",
        )
        # The attribute is accessible.
        assert receipt.usage_trust == "unverified"
        # But it is NOT in the serialized form.
        assert "usage_trust" not in receipt.model_dump(mode="python")

    def test_verified_receipt_round_trip(self):
        """A Receipt with VERIFIED usage also survives round-trip."""
        task = _make_task()
        result = _ok_result(
            task=task,
            provider_metadata=_provider_meta(),
            token_usage=TokenUsage(total_tokens=100),
        )
        receipt = AgentInvocationReceipt(
            result=result,
            tool_calls=0,
            tokens_used=100,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                token_source_id="verifier",
                cost_source_id=None,
                tokens_verified=True,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        raw = serialize_contract(receipt)
        restored = deserialize_contract(raw, AgentInvocationReceipt)
        assert restored == receipt
        assert restored.tokens_used == 100
        assert restored.token_disposition == AttemptUsageDisposition.VERIFIED


# ===========================================================================
# R10.1 P0-2: Negative Failure Outcome Usage Rejected
# ===========================================================================


class TestNegativeOutcomeUsageRejected:
    """R10.1 P0-2: ``AgentInvocationOutcome`` rejects negative
    ``tokens_used`` / ``cost_usd`` at construction time via ``ge=0``.

    The shared :func:`validate_usage_dimension` also defensively
    rejects negative VERIFIED values so it is the complete common
    authority for all four Contracts.
    """

    def test_outcome_rejects_negative_verified_tokens(self):
        """Negative ``tokens_used`` with VERIFIED disposition is
        rejected at the Outcome boundary — NOT at the Accountant."""
        with pytest.raises(ValidationError) as exc_info:
            AgentInvocationOutcome(
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=-1,
                cost_usd=None,
                token_source_id="verifier",
                cost_source_id=None,
            )
        assert "tokens_used" in str(exc_info.value).lower()

    def test_outcome_rejects_negative_verified_cost(self):
        """Negative ``cost_usd`` with VERIFIED disposition is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentInvocationOutcome(
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.VERIFIED,
                tokens_used=None,
                cost_usd=Decimal("-0.01"),
                token_source_id=None,
                cost_source_id="cost_verifier",
            )
        assert "cost_usd" in str(exc_info.value).lower()

    def test_negative_usage_never_reaches_accountant(self):
        """A negative-value Outcome cannot be constructed, so it never
        reaches ``record_invocation_outcome`` — the error is surfaced
        at the Outcome boundary, not as an infrastructure exception
        inside the Accountant."""
        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        accountant = _BudgetAccountant(budget, start_monotonic=time.monotonic())
        # The Outcome itself rejects the negative value — we never
        # get to call record_invocation_outcome.
        with pytest.raises(ValidationError):
            AgentInvocationOutcome(
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.VERIFIED,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
                tokens_used=-100,
                cost_usd=None,
                token_source_id="verifier",
                cost_source_id=None,
            )
        # Accountant state is untouched — no record was committed.
        assert len(accountant._attempt_records) == 0

    def test_validate_usage_dimension_rejects_negative_verified(self):
        """The shared :func:`validate_usage_dimension` function also
        defensively rejects negative VERIFIED values — it is the
        COMPLETE common authority for all four Contracts."""
        from multi_agent.usage import validate_usage_dimension

        with pytest.raises(ValueError, match="negative"):
            validate_usage_dimension(
                "token",
                AttemptUsageDisposition.VERIFIED,
                -1,
                "verifier",
            )
        with pytest.raises(ValueError, match="negative"):
            validate_usage_dimension(
                "cost",
                AttemptUsageDisposition.VERIFIED,
                Decimal("-0.01"),
                "cost_verifier",
            )

    def test_zero_verified_value_is_accepted(self):
        """Zero is a legitimate VERIFIED value (e.g. a cached call that
        the Verifier confirmed cost nothing) — it must NOT be rejected."""
        outcome = AgentInvocationOutcome(
            error_code="failure",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.VERIFIED,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            tokens_used=0,
            cost_usd=None,
            token_source_id="verifier",
            cost_source_id=None,
        )
        assert outcome.tokens_used == 0


# ===========================================================================
# R10.1 P1-1: VerifiedUsage Symmetric Invariants
# ===========================================================================


class TestVerifiedUsageSymmetricInvariants:
    """R10.1 P1-1: :class:`VerifiedUsage` now enforces SYMMETRIC
    invariants — ``verified=False`` requires ``value=None`` for BOTH
    dimensions.

    Previously, only the forward direction was checked
    (``verified=True → value is not None``).  This allowed a Verifier
    to return ``tokens_verified=False, tokens_used=100`` — an
    unverified dimension carrying a numeric value.  The Invoker would
    then pass that value to the Receipt, which would fail at the
    Receipt boundary with an uncontrolled ``ValidationError`` because
    ``UNAVAILABLE`` disposition requires ``value=None``.
    """

    def test_unverified_tokens_with_value_rejected(self):
        """``tokens_verified=False, tokens_used=100`` is rejected."""
        with pytest.raises(ValidationError, match="tokens_verified=False"):
            VerifiedUsage(
                tokens_verified=False,
                tokens_used=100,
            )

    def test_unverified_cost_with_value_rejected(self):
        """``cost_verified=False, cost_usd=Decimal('2.00')`` is rejected."""
        with pytest.raises(ValidationError, match="cost_verified=False"):
            VerifiedUsage(
                cost_verified=False,
                cost_usd=Decimal("2.00"),
            )

    def test_both_unverified_with_values_rejected(self):
        """Both dimensions unverified but carrying values is rejected."""
        with pytest.raises(ValidationError):
            VerifiedUsage(
                tokens_verified=False,
                cost_verified=False,
                tokens_used=100,
                cost_usd=Decimal("1.00"),
            )

    def test_unverified_with_none_values_accepted(self):
        """Both dimensions unverified with ``None`` values is the
        correct default and must be accepted."""
        v = VerifiedUsage(
            tokens_verified=False,
            cost_verified=False,
            tokens_used=None,
            cost_usd=None,
        )
        assert v.tokens_used is None
        assert v.cost_usd is None
        assert v.verified is False

    def test_verified_with_values_accepted(self):
        """Both dimensions verified with non-None values is accepted."""
        v = VerifiedUsage(
            tokens_verified=True,
            cost_verified=True,
            tokens_used=100,
            cost_usd=Decimal("1.50"),
        )
        assert v.tokens_used == 100
        assert v.cost_usd == Decimal("1.50")
        assert v.verified is True

    def test_mixed_verified_unverified_accepted(self):
        """Token verified with value + Cost unverified with None is
        the valid mixed case."""
        v = VerifiedUsage(
            tokens_verified=True,
            cost_verified=False,
            tokens_used=200,
            cost_usd=None,
        )
        assert v.tokens_verified is True
        assert v.cost_verified is False
        assert v.verified is True


# ===========================================================================
# R10.1 P1-3: Result Requires observed_tool_calls Alignment
# ===========================================================================


class TestResultRequiresToolCountAlignment:
    """R10.1 P1-3: when ``result`` is present on an
    :class:`AgentInvocationOutcome`, ``observed_tool_calls`` MUST be
    non-None AND equal ``len(result.tool_calls)``.

    Previously, ``observed_tool_calls=None`` was accepted even when a
    Result was present, which misclassified a call with a complete
    Result as ``tool_usage_unavailable``.
    """

    def test_result_with_none_tool_calls_rejected(self):
        """``result`` present + ``observed_tool_calls=None`` is rejected."""
        task = _make_task()
        result = _ok_result(
            task=task,
            tool_calls=[
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
            ],
        )
        with pytest.raises(ValidationError, match="observed_tool_calls is None"):
            AgentInvocationOutcome(
                result=result,
                error_code="failure",
                observed_tool_calls=None,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            )

    def test_result_with_matching_tool_calls_accepted(self):
        """``result`` present + ``observed_tool_calls=len(tool_calls)``
        is accepted."""
        task = _make_task()
        result = _ok_result(
            task=task,
            tool_calls=[
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
            ],
        )
        outcome = AgentInvocationOutcome(
            result=result,
            error_code="failure",
            observed_tool_calls=2,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        assert outcome.observed_tool_calls == 2

    def test_result_with_mismatched_tool_calls_rejected(self):
        """``result`` present + ``observed_tool_calls != len(tool_calls)``
        is rejected."""
        task = _make_task()
        result = _ok_result(
            task=task,
            tool_calls=[
                ToolCallRecord(
                    tool_name="tool.read",
                    authority=ToolAuthority.READ,
                ),
            ],
        )
        with pytest.raises(ValidationError, match="does not match"):
            AgentInvocationOutcome(
                result=result,
                error_code="failure",
                observed_tool_calls=5,
                token_disposition=AttemptUsageDisposition.UNAVAILABLE,
                cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
            )

    def test_no_result_with_none_tool_calls_accepted(self):
        """No Result + ``observed_tool_calls=None`` is still accepted —
        ``None`` means "unknown" and is valid when no Result is
        available (e.g. timeout, exception before any receipt)."""
        outcome = AgentInvocationOutcome(
            result=None,
            error_code="timeout",
            observed_tool_calls=None,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        assert outcome.observed_tool_calls is None

    def test_no_result_with_concrete_tool_calls_accepted(self):
        """No Result + ``observed_tool_calls=3`` is accepted — the
        Invoker may attest a concrete count even without a Result
        (e.g. it observed 3 tool calls before the exception)."""
        outcome = AgentInvocationOutcome(
            result=None,
            error_code="partial_failure",
            observed_tool_calls=3,
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
        )
        assert outcome.observed_tool_calls == 3
