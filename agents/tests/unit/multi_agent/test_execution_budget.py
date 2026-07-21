"""Phase 4 Execution Budget enforcement tests.

Covers:

* :class:`SupervisorConfig` defaults.
* :class:`TaskAttemptRecord` / :class:`TaskExecutionRecord` /
  :class:`ExecutionTraceEvent` schema.
* :func:`build_execution_context` — identity fields cannot be overridden.
* :func:`validate_agent_result` — boundary checks.
* :func:`final_status_priority` — priority ordering.
* Token / cost budget fail-closed when receipt reports ``None``.
* Deadline via ``time.monotonic()``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    Evidence,
    EvidenceType,
    ExecutionBudget,
    TokenUsage,
)
from multi_agent.execution import (
    ExecutionTraceEvent,
    FakeExecutionCancellation,
    SupervisorConfig,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TRACE_BUDGET_EXCEEDED,
    TRACE_PLAN_VALIDATED,
    TRACE_RUN_STARTED,
    final_status_priority,
    validate_agent_result,
)
from multi_agent.execution_errors import InvalidAgentResultError
from multi_agent.invocation import (
    AgentInvocationReceipt,
    UsageVerificationCapabilities,
)
from multi_agent.planning import PlanDraft, PlanningRequest, PlanningSignals
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

from multi_agent.contracts import AgentAuthority, AgentCapability, ToolAuthority


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# R4 P0-2: capability doubles used by direct ``_BudgetAccountant`` tests.
_TRUSTED_TOKEN_CAPS = UsageVerificationCapabilities(
    verifies_tokens=True,
    verifies_cost=False,
    source_id="test_trusted_token_invoker",
)
_TRUSTED_COST_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=True,
    source_id="test_trusted_cost_invoker",
)
_UNVERIFIED_CAPS = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="test_unverified_invoker",
)


def _make_proposal(
    proposal_id: str = "p-001",
    tenant_id: str = "t-001",
    agent_id: str = "agent_a",
) -> ActionProposal:
    return ActionProposal.create(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=agent_id,
        action_type="create",
        target_entity="ticket",
        priority="medium",
        risk_level=ActionRiskLevel.MEDIUM,
        evidence_ids=[],
        requires_approval=True,
        idempotency_key=f"ik-{proposal_id}",
        created_at=_FIXED_TS,
    )


def _make_evidence(
    evidence_id: str = "ev-001", tenant_id: str = "t-001", agent_id: str = "agent_a"
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TOOL_RESULT,
        tenant_id=tenant_id,
        source_agent=agent_id,
        created_at=_FIXED_TS,
    )


def _make_task(
    task_id: str = "task_001",
    agent_id: str = "agent_a",
    tenant_id: str = "t-001",
    **overrides: Any,
) -> AgentTask:
    defaults: dict[str, Any] = dict(
        task_id=task_id,
        agent_id=agent_id,
        task_type="test_task",
        objective="test objective",
        tenant_id=tenant_id,
        timeout_ms=10_000,
    )
    defaults.update(overrides)
    return AgentTask(**defaults)


def _make_result(
    result_id: str = "r-001",
    task_id: str = "task_001",
    agent_id: str = "agent_a",
    tenant_id: str = "t-001",
    status: str = "completed",
    proposals: list[ActionProposal] | None = None,
    evidence: list[Evidence] | None = None,
    **overrides: Any,
) -> AgentResult:
    defaults: dict[str, Any] = dict(
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        tenant_id=tenant_id,
        status=status,
        confidence=1.0,
        duration_ms=0.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        token_usage=TokenUsage(),
        completed_at=_FIXED_TS,
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


def _make_capability(agent_id: str = "agent_a") -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="Test agent",
        domains=frozenset({"test"}),
        supported_tasks=frozenset({"test_task"}),
        allowed_tools=frozenset({"tool.read"}),
        authority=AgentAuthority.READ,
        input_contract="in",
        output_contract="out",
        timeout_ms=30_000,
        max_retries=0,
        estimated_cost_class="low",
        enabled=True,
    )


def _make_registry(cap: AgentCapability | None = None) -> AgentRegistry:
    catalog = ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )
    reg = AgentRegistry(tool_catalog=catalog)

    class _NoopHandler:
        async def run(
            self, task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentResult:  # pragma: no cover
            raise RuntimeError("not used")

    reg.register(cap or _make_capability(), _NoopHandler())
    return reg


def _make_plan(
    task: AgentTask | None = None,
    tenant_id: str = "t-001",
    budget: ExecutionBudget | None = None,
    cap: AgentCapability | None = None,
) -> PlanDraft:
    reg = _make_registry(cap)
    task = task or _make_task(tenant_id=tenant_id)
    signals = PlanningSignals(
        event_type=None,
        domains=frozenset({"test"}),
        requested_task_types=frozenset({"test_task"}),
        requires_cross_domain=False,
        requires_write=False,
        requires_approval=False,
        has_conflicting_signals=False,
        missing_required_context=False,
        objective_kind=None,
    )
    request = PlanningRequest(
        run_id="run-001",
        tenant_id=tenant_id,
        actor_type="user",
        actor_id="user-001",
        objective="test objective",
        signals=signals,
        budget=budget or ExecutionBudget(),
        context_summary=None,
        registry_version=reg.snapshot().version,
    )

    from multi_agent.complexity_gate import ComplexityDecision
    from multi_agent.planning import PlannedTask, compute_request_hash

    complexity = ComplexityDecision(
        route="single_agent",
        domains=["test"],
        reasons=["test"],
        confidence=1.0,
        requires_human_review=False,
    )
    planned = PlannedTask(
        intent_id="intent_001",
        domain="test",
        task=task,
        preferred_authority=AgentAuthority.READ,
        planning_metadata={},
    )
    return PlanDraft(
        request=request,
        request_hash=compute_request_hash(request),
        complexity=complexity,
        tasks=[planned],
        planner_version="ma-03.6.0",
    )


# ---------------------------------------------------------------------------
# SupervisorConfig
# ---------------------------------------------------------------------------


class TestSupervisorConfig:
    def test_defaults_are_deterministic_friendly(self):
        cfg = SupervisorConfig()
        assert cfg.max_concurrency == 4
        assert cfg.retry_backoff_ms == 0

    def test_removed_config_fields_are_rejected(self):
        """R1 P1: ``continue_independent_branches`` and
        ``deterministic_mode`` were removed because they were never
        read.  ``extra='forbid'`` must reject them so callers do not
        silently pass no-op configuration."""
        from multi_agent.contracts import StrictContract

        # StrictContract sets extra='forbid' — any unknown kwarg raises.
        with pytest.raises(Exception):
            SupervisorConfig(continue_independent_branches=False)  # type: ignore[call-arg]
        with pytest.raises(Exception):
            SupervisorConfig(deterministic_mode=False)  # type: ignore[call-arg]
        # Sanity-check the base config.
        assert issubclass(SupervisorConfig, StrictContract)

    def test_max_concurrency_lower_bound(self):
        with pytest.raises(Exception):
            SupervisorConfig(max_concurrency=0)

    def test_max_concurrency_upper_bound(self):
        with pytest.raises(Exception):
            SupervisorConfig(max_concurrency=33)


# ---------------------------------------------------------------------------
# TaskAttemptRecord
# ---------------------------------------------------------------------------


class TestTaskAttemptRecord:
    def test_rejects_naive_started_at(self):
        with pytest.raises(Exception, match="timezone-aware"):
            TaskAttemptRecord(
                task_id="task_001",
                agent_id="agent_a",
                attempt=0,
                started_at=datetime(2025, 1, 1),  # naive
                status="running",
            )

    def test_accepts_utc_started_at(self):
        rec = TaskAttemptRecord(
            task_id="task_001",
            agent_id="agent_a",
            attempt=0,
            started_at=_FIXED_TS,
            status="running",
        )
        assert rec.started_at.tzinfo is not None

    def test_completed_at_may_be_none(self):
        rec = TaskAttemptRecord(
            task_id="task_001",
            agent_id="agent_a",
            attempt=0,
            started_at=_FIXED_TS,
            status="running",
            completed_at=None,
        )
        assert rec.completed_at is None

    def test_agent_calls_minimum_is_one(self):
        with pytest.raises(Exception):
            TaskAttemptRecord(
                task_id="task_001",
                agent_id="agent_a",
                attempt=0,
                started_at=_FIXED_TS,
                status="running",
                agent_calls=0,
            )


# ---------------------------------------------------------------------------
# ExecutionTraceEvent
# ---------------------------------------------------------------------------


class TestExecutionTraceEvent:
    def test_rejects_naive_occurred_at(self):
        with pytest.raises(Exception, match="timezone-aware"):
            ExecutionTraceEvent(
                sequence=0,
                event_type=TRACE_RUN_STARTED,
                run_id="run-001",
                occurred_at=datetime(2025, 1, 1),  # naive
            )

    def test_sequence_must_be_non_negative(self):
        with pytest.raises(Exception):
            ExecutionTraceEvent(
                sequence=-1,
                event_type=TRACE_RUN_STARTED,
                run_id="run-001",
                occurred_at=_FIXED_TS,
            )

    def test_data_defaults_to_empty_dict(self):
        ev = ExecutionTraceEvent(
            sequence=0,
            event_type=TRACE_PLAN_VALIDATED,
            run_id="run-001",
            occurred_at=_FIXED_TS,
        )
        assert ev.data == {}


# ---------------------------------------------------------------------------
# build_execution_context (tested indirectly via validate_agent_result)
# ---------------------------------------------------------------------------


class TestValidateAgentResult:
    def test_valid_result_passes(self):
        task = _make_task()
        plan = _make_plan(task=task)
        result = _make_result(task_id=task.task_id, agent_id=task.agent_id)
        validate_agent_result(result, task=task, plan=plan)

    def test_result_task_id_mismatch_rejected(self):
        task = _make_task(task_id="task_001")
        plan = _make_plan(task=task)
        result = _make_result(task_id="task_002")
        with pytest.raises(InvalidAgentResultError, match="task_id"):
            validate_agent_result(result, task=task, plan=plan)

    def test_result_agent_id_mismatch_rejected(self):
        task = _make_task(agent_id="agent_a")
        plan = _make_plan(task=task)
        result = _make_result(agent_id="agent_b")
        with pytest.raises(InvalidAgentResultError, match="agent_id"):
            validate_agent_result(result, task=task, plan=plan)

    def test_result_tenant_mismatch_rejected(self):
        task = _make_task(tenant_id="t-001")
        plan = _make_plan(task=task, tenant_id="t-001")
        # Manually construct a result with a foreign tenant.  AgentResult
        # itself rejects cross-tenant evidence/proposals, so we use
        # the minimal case: foreign tenant_id on the result itself.
        # We bypass the validator by constructing via model_construct.
        result = AgentResult.model_construct(
            result_id="r-001",
            task_id=task.task_id,
            agent_id=task.agent_id,
            agent_version="1.0.0",
            tenant_id="t-other",
            status="completed",
            confidence=1.0,
            duration_ms=0.0,
            summary="",
            output=None,
            findings=[],
            unresolved_questions=[],
            errors=[],
            evidence=[],
            action_proposals=[],
            token_usage=TokenUsage(),
            tool_calls=[],
            provider_metadata=None,
            started_at=None,
            completed_at=_FIXED_TS,
        )
        with pytest.raises(InvalidAgentResultError, match="tenant_id"):
            validate_agent_result(result, task=task, plan=plan)

    def test_proposal_created_by_agent_mismatch_rejected(self):
        task = _make_task(agent_id="agent_a")
        plan = _make_plan(task=task)
        # Build a proposal where created_by_agent != task.agent_id.
        # AgentResult rejects such proposals at construction, so we
        # bypass via model_construct.
        proposal = ActionProposal.model_construct(
            proposal_id="p-001",
            proposal_hash="x",
            tenant_id="t-001",
            created_by_agent="agent_b",  # mismatch
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={},
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            justification=None,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-p-001",
            created_at=_FIXED_TS,
        )
        result = AgentResult.model_construct(
            result_id="r-001",
            task_id=task.task_id,
            agent_id=task.agent_id,
            agent_version="1.0.0",
            tenant_id="t-001",
            status="completed",
            confidence=1.0,
            duration_ms=0.0,
            summary="",
            output=None,
            findings=[],
            unresolved_questions=[],
            errors=[],
            evidence=[],
            action_proposals=[proposal],
            token_usage=TokenUsage(),
            tool_calls=[],
            provider_metadata=None,
            started_at=None,
            completed_at=_FIXED_TS,
        )
        with pytest.raises(InvalidAgentResultError, match="created_by_agent"):
            validate_agent_result(result, task=task, plan=plan)

    def test_invalid_result_status_rejected(self):
        task = _make_task()
        plan = _make_plan(task=task)
        result = _make_result(status="completed")
        # Manually corrupt status — AgentResult would reject it, so
        # we use model_construct.
        result = AgentResult.model_construct(
            result_id="r-001",
            task_id=task.task_id,
            agent_id=task.agent_id,
            agent_version="1.0.0",
            tenant_id="t-001",
            status="bogus_status",
            confidence=1.0,
            duration_ms=0.0,
            summary="",
            output=None,
            findings=[],
            unresolved_questions=[],
            errors=[],
            evidence=[],
            action_proposals=[],
            token_usage=TokenUsage(),
            tool_calls=[],
            provider_metadata=None,
            started_at=None,
            completed_at=_FIXED_TS,
        )
        with pytest.raises(InvalidAgentResultError, match="status"):
            validate_agent_result(result, task=task, plan=plan)


# ---------------------------------------------------------------------------
# final_status_priority
# ---------------------------------------------------------------------------


class TestFinalStatusPriority:
    def test_priority_ordering(self):
        assert final_status_priority(
            SupervisorRunStatus.CANCELLED
        ) < final_status_priority(SupervisorRunStatus.BUDGET_EXCEEDED)
        assert final_status_priority(
            SupervisorRunStatus.BUDGET_EXCEEDED
        ) < final_status_priority(SupervisorRunStatus.FAILED)
        assert final_status_priority(
            SupervisorRunStatus.FAILED
        ) < final_status_priority(SupervisorRunStatus.NEEDS_INPUT)
        assert final_status_priority(
            SupervisorRunStatus.NEEDS_INPUT
        ) < final_status_priority(SupervisorRunStatus.PARTIAL_SUCCESS)
        assert final_status_priority(
            SupervisorRunStatus.PARTIAL_SUCCESS
        ) < final_status_priority(SupervisorRunStatus.COMPLETED)

    def test_pending_and_running_rank_below_terminals(self):
        assert final_status_priority(
            SupervisorRunStatus.PENDING
        ) > final_status_priority(SupervisorRunStatus.COMPLETED)
        assert final_status_priority(
            SupervisorRunStatus.RUNNING
        ) > final_status_priority(SupervisorRunStatus.COMPLETED)


# ---------------------------------------------------------------------------
# FakeExecutionCancellation
# ---------------------------------------------------------------------------


class TestFakeExecutionCancellation:
    @pytest.mark.asyncio
    async def test_starts_inactive(self):
        canc = FakeExecutionCancellation()
        assert not await canc.is_cancelled("run-001")
        assert not await canc.is_kill_switch_active("t-001")

    @pytest.mark.asyncio
    async def test_cancel_run_activates_only_that_run(self):
        canc = FakeExecutionCancellation()
        canc.cancel_run("run-001")
        assert await canc.is_cancelled("run-001")
        assert not await canc.is_cancelled("run-002")

    @pytest.mark.asyncio
    async def test_kill_switch_activates_only_that_tenant(self):
        canc = FakeExecutionCancellation()
        canc.activate_kill_switch("t-001")
        assert await canc.is_kill_switch_active("t-001")
        assert not await canc.is_kill_switch_active("t-002")


# ---------------------------------------------------------------------------
# Budget enforcement (via _BudgetAccountant indirectly)
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def test_token_budget_with_none_usage_fails_closed(self):
        """If ``token_budget`` is configured and the receipt reports
        ``None`` tokens_used, ``record_receipt`` must raise.

        R4 P0-2: the receipt's ``usage_trust`` is cross-checked
        against the invoker's capabilities.  To exercise the None
        branch specifically we pair a ``verified_provider`` receipt
        with a ``verifies_tokens=True`` invoker so the provenance
        check passes and the None check fires.
        """
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(token_budget=1000)
        acc = _BudgetAccountant(budget, start_monotonic=0.0)
        receipt = AgentInvocationReceipt(
            result=_make_result(),
            tool_calls=0,
            tokens_used=None,  # fail-closed
            usage_trust="verified_provider",
        )
        with pytest.raises(Exception, match="token_budget"):
            acc.record_receipt(
                receipt,
                invoker_capabilities=_TRUSTED_TOKEN_CAPS,
                task_id="test_task",
                attempt=0,
            )

    def test_cost_budget_with_none_usage_fails_closed(self):
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(cost_budget_usd=Decimal("1.00"))
        acc = _BudgetAccountant(budget, start_monotonic=0.0)
        receipt = AgentInvocationReceipt(
            result=_make_result(),
            tool_calls=0,
            cost_usd=None,  # fail-closed
            usage_trust="trusted_adapter",
        )
        with pytest.raises(Exception, match="cost_budget"):
            acc.record_receipt(
                receipt,
                invoker_capabilities=_TRUSTED_COST_CAPS,
                task_id="test_task",
                attempt=0,
            )

    def test_max_agent_calls_exceeded(self):
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(max_agent_calls=1)
        acc = _BudgetAccountant(budget, start_monotonic=0.0)
        assert acc.can_start_agent_call()
        acc.reserve_agent_call()
        # Second call must not be allowed.
        assert not acc.can_start_agent_call()
        assert acc.exceeded
        assert "max_agent_calls" in (acc.exceeded_reason or "")

    def test_max_tool_calls_exceeded(self):
        """R4 P0-3: tool calls are charged via
        :meth:`record_observed_tool_calls` *before* receipt validation,
        so an invalid receipt cannot erase already-consumed budget.
        ``record_receipt`` no longer accumulates tool calls."""
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(max_tool_calls=2)
        acc = _BudgetAccountant(budget, start_monotonic=0.0)
        # First observed count — OK.
        acc.record_observed_tool_calls(1)
        assert not acc.exceeded
        # Second observed count — exceeds.
        acc.record_observed_tool_calls(2)
        assert acc.exceeded
        assert "max_tool_calls" in (acc.exceeded_reason or "")

    def test_max_iterations_exceeded(self):
        """R1 P0-2: ``reserve_iteration`` is the new API.  The budget
        is checked *before* the wave is dispatched, so a violated
        budget stops new work immediately."""
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(max_iterations=2)
        acc = _BudgetAccountant(budget, start_monotonic=0.0)
        acc.reserve_iteration()
        acc.reserve_iteration()
        assert not acc.exceeded  # 2 == max_iterations — still OK.
        # Third wave would exceed — ``can_start_iteration`` returns
        # False and marks the accountant as exceeded.
        assert not acc.can_start_iteration()
        assert acc.exceeded
        assert "max_iterations" in (acc.exceeded_reason or "")

    def test_deadline_uses_monotonic_clock(self):
        from multi_agent.supervisor import _BudgetAccountant

        budget = ExecutionBudget(deadline_ms=1000)
        # Simulate that the run started at monotonic=0.0.
        acc = _BudgetAccountant(budget, start_monotonic=0.0)
        assert acc.has_time_for_attempt(0.5)  # 500ms elapsed — still has 500ms left
        assert not acc.has_time_for_attempt(2.0)  # 2000ms elapsed — no time left


# Silence unused-import for TRACE_BUDGET_EXCEEDED — used above.
_ = TRACE_BUDGET_EXCEEDED
