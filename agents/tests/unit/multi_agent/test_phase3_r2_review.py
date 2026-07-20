"""Phase 3 R2 review counterexample tests.

Covers the 5 new P0 issues + 3 P1 issues from the R2 review:

* P0-1: Plan task substitution must be detected (intent binding).
* P0-2: requested_tasks must be the primary source of truth for routing.
* P0-3: Agent candidate filtering must be tool-aware.
* P0-4: Multi-agent assignment must guarantee ≥2 distinct agents.
* P0-5: Tool call budget cannot be bypassed with zero estimate.
* P1-A: Customer Recovery Complexity domain must match template.
* P1-B: Unexpected Gate exceptions must not be swallowed.
* (Doc update is verified manually.)

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from multi_agent.complexity_gate import (
    CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    RuleBasedComplexityGate,
)
from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentTask,
    ComplexityDecision,
    ExecutionBudget,
    ToolAuthority,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    TaskIntent,
    effective_domains,
    effective_task_types,
    resolve_expected_intents,
)
from multi_agent.planning_errors import (
    PlanningInputError,
    UnsupportedCapabilityError,
)
from multi_agent.plan_validator import (
    PlanValidator,
    CODE_IDEMPOTENCY_KEY_MISMATCH,
    CODE_PLAN_INTENT_MISMATCH,
    CODE_PLANNED_TASK_REQUIRED_MISMATCH,
    CODE_UNSTABLE_TASK_ID,
    CODE_DUPLICATE_INTENT_ID,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

# Helpers ----------------------------------------------------------------


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    domains: frozenset[str] | None = None,
    supported_tasks: frozenset[str] | None = None,
    allowed_tools: frozenset[str] | None = None,
    enabled: bool = True,
    timeout_ms: int = 30_000,
    cost_class: str = "low",
    **overrides: Any,
) -> AgentCapability:
    defaults: dict[str, Any] = dict(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Agent {agent_id}",
        domains=domains or frozenset({"support"}),
        supported_tasks=supported_tasks or frozenset({"support_analysis"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_leads"}),
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=timeout_ms,
        max_retries=2,
        estimated_cost_class=cost_class,
        enabled=enabled,
    )
    defaults.update(overrides)
    return AgentCapability(**defaults)


class _FakeHandler:
    async def run(self, task: Any, context: Any) -> Any:  # pragma: no cover
        raise RuntimeError("Phase 3 tests never call handlers")


def _default_catalog() -> ToolCatalog:
    return ToolCatalog(
        [
            ToolDescriptor(
                tool_name="crm_reader.get_leads", authority=ToolAuthority.READ
            ),
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
            ToolDescriptor(
                tool_name="crm_writer.propose", authority=ToolAuthority.PROPOSE
            ),
            ToolDescriptor(
                tool_name="automation_executor.execute",
                authority=ToolAuthority.EXECUTE,
            ),
        ]
    )


def _make_registry(
    caps: list[AgentCapability],
    catalog: ToolCatalog | None = None,
) -> AgentRegistry:
    reg = AgentRegistry(tool_catalog=catalog or _default_catalog())
    for cap in caps:
        reg.register(cap, _FakeHandler())
    return reg


def _make_signals(**overrides: Any) -> PlanningSignals:
    defaults: dict[str, Any] = dict(
        domains=frozenset({"support"}),
        requested_task_types=frozenset({"support_analysis"}),
    )
    defaults.update(overrides)
    return PlanningSignals(**defaults)


def _make_request(
    registry: AgentRegistry,
    signals: PlanningSignals | None = None,
    **overrides: Any,
) -> PlanningRequest:
    defaults: dict[str, Any] = dict(
        run_id="run-001",
        tenant_id="t-001",
        actor_type="user",
        actor_id="user-001",
        objective="Analyse customer issue",
        signals=signals or _make_signals(),
        budget=ExecutionBudget(),
        registry_version=registry.snapshot().version,
    )
    defaults.update(overrides)
    return PlanningRequest(**defaults)


def _make_requested_task(
    intent_id: str = "rt-1",
    domain: str = "support",
    task_type: str = "support_analysis",
    objective: str = "Analyse issue",
    preferred_authority: AgentAuthority = AgentAuthority.READ,
    dependencies: list[str] | None = None,
    required: bool = True,
    required_tools: frozenset[str] | None = None,
    estimated_tool_calls: int = 1,
) -> RequestedTask:
    return RequestedTask(
        intent_id=intent_id,
        domain=domain,
        task_type=task_type,
        objective=objective,
        preferred_authority=preferred_authority,
        dependencies=dependencies or [],
        required=required,
        required_tools=required_tools or frozenset(),
        estimated_tool_calls=estimated_tool_calls,
    )


def _set_plan_tasks(plan: PlanDraft, tasks: list[PlannedTask]) -> PlanDraft:
    """Bypass validate_assignment to swap tasks, then recompute hash."""
    object.__setattr__(plan, "tasks", tasks)
    new_hash = plan.compute_plan_hash()
    object.__setattr__(plan, "plan_hash", new_hash)
    return plan


# ============================================================================
# P0-1: Plan task substitution must be detected (intent binding)
# ============================================================================


class TestPlanIntentBinding:
    """Validator must reject plans where PlannedTask content does not
    match the expected intent derived from the request."""

    def _make_multi_agent_plan(
        self,
    ) -> tuple[PlanDraft, AgentRegistry, PlanningRequest]:
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
        )
        reg = _make_registry([cap_a, cap_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            objective="Task A",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            objective="Task B",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        return plan, reg, request

    def test_plan_task_substitution_rejected(self):
        """Replace a task with a different (registry-supported) task →
        CODE_PLAN_INTENT_MISMATCH."""
        plan, reg, request = self._make_multi_agent_plan()
        original = plan.tasks[0]
        # Build a substituted task: same agent, but different domain/task_type.
        sub_task = AgentTask(
            task_id=original.task.task_id,
            agent_id=original.task.agent_id,
            task_type="task_b",  # was task_a
            objective="Substituted",
            tenant_id=original.task.tenant_id,
            dependencies=original.task.dependencies,
            required=original.task.required,
            idempotency_key=original.task.idempotency_key,
        )
        sub_pt = PlannedTask(
            intent_id=original.intent_id,
            domain="sales",  # was support
            preferred_authority=original.preferred_authority,
            required_tools=original.required_tools,
            estimated_tool_calls=original.estimated_tool_calls,
            required=original.required,
            task=sub_task,
        )
        _set_plan_tasks(plan, [sub_pt, plan.tasks[1]])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_PLAN_INTENT_MISMATCH in codes
        assert not report.valid

    def test_arbitrary_task_id_rejected(self):
        """Replace task_id with a random value → CODE_UNSTABLE_TASK_ID."""
        plan, reg, request = self._make_multi_agent_plan()
        original = plan.tasks[0]
        tampered_task = AgentTask(
            task_id="arbitrary-tampered-id-xx",
            agent_id=original.task.agent_id,
            task_type=original.task.task_type,
            objective=original.task.objective,
            tenant_id=original.task.tenant_id,
            dependencies=frozenset(),
            required=original.task.required,
            idempotency_key=original.task.idempotency_key,
        )
        tampered_pt = PlannedTask(
            intent_id=original.intent_id,
            domain=original.domain,
            preferred_authority=original.preferred_authority,
            required_tools=original.required_tools,
            estimated_tool_calls=original.estimated_tool_calls,
            required=original.required,
            task=tampered_task,
        )
        _set_plan_tasks(plan, [tampered_pt, plan.tasks[1]])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_UNSTABLE_TASK_ID in codes
        assert not report.valid

    def test_wrong_idempotency_key_rejected(self):
        """Replace idempotency_key → CODE_IDEMPOTENCY_KEY_MISMATCH."""
        plan, reg, request = self._make_multi_agent_plan()
        original = plan.tasks[0]
        tampered_task = AgentTask(
            task_id=original.task.task_id,
            agent_id=original.task.agent_id,
            task_type=original.task.task_type,
            objective=original.task.objective,
            tenant_id=original.task.tenant_id,
            dependencies=original.task.dependencies,
            required=original.task.required,
            idempotency_key="wrong-key",
        )
        tampered_pt = PlannedTask(
            intent_id=original.intent_id,
            domain=original.domain,
            preferred_authority=original.preferred_authority,
            required_tools=original.required_tools,
            estimated_tool_calls=original.estimated_tool_calls,
            required=original.required,
            task=tampered_task,
        )
        _set_plan_tasks(plan, [tampered_pt, plan.tasks[1]])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_IDEMPOTENCY_KEY_MISMATCH in codes
        assert not report.valid

    def test_wrapper_required_mismatch_rejected(self):
        """PlannedTask.required != AgentTask.required →
        CODE_PLANNED_TASK_REQUIRED_MISMATCH."""
        plan, reg, request = self._make_multi_agent_plan()
        original = plan.tasks[0]
        # Flip AgentTask.required but keep PlannedTask.required.
        tampered_task = AgentTask(
            task_id=original.task.task_id,
            agent_id=original.task.agent_id,
            task_type=original.task.task_type,
            objective=original.task.objective,
            tenant_id=original.task.tenant_id,
            dependencies=original.task.dependencies,
            required=not original.task.required,
            idempotency_key=original.task.idempotency_key,
        )
        tampered_pt = PlannedTask(
            intent_id=original.intent_id,
            domain=original.domain,
            preferred_authority=original.preferred_authority,
            required_tools=original.required_tools,
            estimated_tool_calls=original.estimated_tool_calls,
            required=original.required,
            task=tampered_task,
        )
        _set_plan_tasks(plan, [tampered_pt, plan.tasks[1]])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_PLANNED_TASK_REQUIRED_MISMATCH in codes
        assert not report.valid

    def test_duplicate_planned_intent_id_rejected(self):
        """Two PlannedTasks with the same intent_id →
        CODE_DUPLICATE_INTENT_ID."""
        plan, reg, request = self._make_multi_agent_plan()
        original = plan.tasks[0]
        # Duplicate the first task's intent_id on the second task.
        dup_task = AgentTask(
            task_id=plan.tasks[1].task.task_id,
            agent_id=plan.tasks[1].task.agent_id,
            task_type=plan.tasks[1].task.task_type,
            objective=plan.tasks[1].task.objective,
            tenant_id=plan.tasks[1].task.tenant_id,
            dependencies=plan.tasks[1].task.dependencies,
            required=plan.tasks[1].task.required,
            idempotency_key=plan.tasks[1].task.idempotency_key,
        )
        dup_pt = PlannedTask(
            intent_id=original.intent_id,  # duplicate!
            domain=plan.tasks[1].domain,
            preferred_authority=plan.tasks[1].preferred_authority,
            required_tools=plan.tasks[1].required_tools,
            estimated_tool_calls=plan.tasks[1].estimated_tool_calls,
            required=plan.tasks[1].required,
            task=dup_task,
        )
        _set_plan_tasks(plan, [plan.tasks[0], dup_pt])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DUPLICATE_INTENT_ID in codes
        assert not report.valid


# ============================================================================
# P0-2: requested_tasks must be the primary source of truth
# ============================================================================


class TestRequestedTasksAsSourceOfTruth:
    """effective_domains / effective_task_types derive from
    requested_tasks when present.  Single-agent route rejects multiple
    RequestedTasks."""

    def test_requested_tasks_only_routes_correctly(self):
        """Only requested_tasks provided (no domains/task_types) →
        routing still works."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
        )
        signals = PlanningSignals(requested_tasks=[rt])
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "single_agent"
        assert set(decision.domains) == {"support"}

    def test_multiple_requested_tasks_not_silently_dropped(self):
        """Two RequestedTasks with same domain+task_type but different
        intent_ids → multi_agent route (not silently dropped)."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_b"}),
        )
        reg = _make_registry([cap_a, cap_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="task_b",
        )
        signals = PlanningSignals(requested_tasks=[rt_a, rt_b])
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        # 2 task types → multi_agent
        assert decision.route == "multi_agent"
        plan = DeterministicPlanner().create_plan(request, reg)
        assert len(plan.tasks) == 2
        intent_ids = {pt.intent_id for pt in plan.tasks}
        assert intent_ids == {"rt-a", "rt-b"}

    def test_single_agent_rejects_multiple_requested_tasks(self):
        """single_agent route with 2 RequestedTasks → PlanningInputError
        from resolve_expected_intents."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="support_analysis",
        )
        # Only one task_type, one domain → single_agent route.
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "single_agent"
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_effective_domains_derived_from_requested_tasks(self):
        rt_a = _make_requested_task(intent_id="rt-a", domain="support")
        rt_b = _make_requested_task(intent_id="rt-b", domain="sales")
        signals = PlanningSignals(requested_tasks=[rt_a, rt_b])
        assert effective_domains(signals) == frozenset({"support", "sales"})

    def test_effective_task_types_derived_from_requested_tasks(self):
        rt_a = _make_requested_task(intent_id="rt-a", task_type="task_a")
        rt_b = _make_requested_task(intent_id="rt-b", task_type="task_b")
        signals = PlanningSignals(requested_tasks=[rt_a, rt_b])
        assert effective_task_types(signals) == frozenset({"task_a", "task_b"})


# ============================================================================
# P0-3: Agent candidate filtering must be tool-aware
# ============================================================================


class TestToolAwareAgentSelection:
    """Planner must skip agents that lack required tools, and prefer
    tool-capable agents even if they are more expensive."""

    def test_planner_skips_agent_missing_required_tool(self):
        """cheap_agent lacks the required tool → planner selects the
        more expensive tool-capable agent."""
        cheap_no_tool = _make_capability(
            agent_id="cheap_no_tool",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_leads"}),  # no tickets
            cost_class="low",
        )
        expensive_with_tool = _make_capability(
            agent_id="expensive_with_tool",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            cost_class="medium",
        )
        reg = _make_registry([cheap_no_tool, expensive_with_tool])
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "expensive_with_tool"

    def test_planner_selects_tool_capable_candidate(self):
        """When multiple agents support the task, only those with the
        required tool are candidates."""
        cap_with_tool = _make_capability(
            agent_id="with_tool",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_without_tool = _make_capability(
            agent_id="without_tool",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_leads"}),
        )
        reg = _make_registry([cap_with_tool, cap_without_tool])
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "with_tool"

    def test_unknown_required_tool_fails_before_plan_creation(self):
        """Required tool not in catalog → UnsupportedCapabilityError."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap])
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            required_tools=frozenset({"nonexistent.tool"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(UnsupportedCapabilityError):
            DeterministicPlanner().create_plan(request, reg)

    def test_required_execute_tool_has_no_phase3_candidate(self):
        """Required tool with EXECUTE authority → no candidates
        (Phase 3 ceiling).

        Only EXECUTE-level agents can register EXECUTE tools (registry
        invariant), and the planner filters EXECUTE agents out of the
        candidate set.  Therefore a request that requires an EXECUTE
        tool has no feasible Phase 3 candidate.
        """
        cap = _make_capability(
            agent_id="support_agent",
            authority=AgentAuthority.EXECUTE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"automation_executor.execute"}),
        )
        reg = _make_registry([cap])
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            required_tools=frozenset({"automation_executor.execute"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(UnsupportedCapabilityError):
            DeterministicPlanner().create_plan(request, reg)


# ============================================================================
# P0-4: Multi-agent assignment must guarantee ≥2 distinct agents
# ============================================================================


class TestMultiAgentGlobalAssignment:
    """When a generalist agent supports all tasks, the planner must
    still diversify across ≥2 agents when feasible."""

    def test_multi_agent_diversifies_generalist_assignment(self):
        """generalist supports task_a + task_b (cost=low); specialists
        support one each (cost=medium).  Planner must pick the two
        specialists, not collapse to the generalist."""
        generalist = _make_capability(
            agent_id="generalist",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
            cost_class="low",
        )
        special_a = _make_capability(
            agent_id="special_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            cost_class="medium",
        )
        special_b = _make_capability(
            agent_id="special_b",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_b"}),
            cost_class="medium",
        )
        reg = _make_registry([generalist, special_a, special_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="task_b",
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        agents = {pt.task.agent_id for pt in plan.tasks}
        assert len(agents) >= 2, f"Expected ≥2 distinct agents, got {agents!r}"

    def test_multi_agent_assignment_is_deterministic(self):
        """Same request + registry → same agent assignment."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
        )
        reg = _make_registry([cap_a, cap_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan1 = DeterministicPlanner().create_plan(request, reg)
        plan2 = DeterministicPlanner().create_plan(request, reg)
        agents1 = [pt.task.agent_id for pt in plan1.tasks]
        agents2 = [pt.task.agent_id for pt in plan2.tasks]
        assert agents1 == agents2

    def test_no_feasible_diverse_assignment_fails_closed(self):
        """Only one agent supports all tasks → multi_agent_too_few_agents
        (fallback to greedy, Validator rejects)."""
        generalist = _make_capability(
            agent_id="generalist",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([generalist])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="task_b",
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        # Planner will fall back to greedy (single agent), Validator
        # will reject with multi_agent_too_few_agents, and the planner
        # raises PlanValidationError.
        from multi_agent.planning_errors import PlanValidationError

        with pytest.raises(PlanValidationError):
            DeterministicPlanner().create_plan(request, reg)


# ============================================================================
# P0-5: Tool call budget cannot be bypassed with zero estimate
# ============================================================================


class TestToolBudgetZeroBypass:
    """RequestedTask / TaskIntent with required_tools but
    estimated_tool_calls < len(required_tools) → ValidationError."""

    def test_required_tool_cannot_have_zero_estimated_calls(self):
        with pytest.raises(ValidationError):
            RequestedTask(
                intent_id="rt-1",
                domain="support",
                task_type="support_analysis",
                objective="Analyse",
                required_tools=frozenset({"crm_reader.get_tickets"}),
                estimated_tool_calls=0,
            )

    def test_tool_call_estimate_cannot_be_lower_than_required_tool_count(self):
        with pytest.raises(ValidationError):
            RequestedTask(
                intent_id="rt-1",
                domain="support",
                task_type="support_analysis",
                objective="Analyse",
                required_tools=frozenset(
                    {"crm_reader.get_tickets", "crm_reader.get_deals"}
                ),
                estimated_tool_calls=1,  # < 2
            )

    def test_tool_budget_zero_estimate_bypass_rejected(self):
        """Validator must not see estimated_tool_calls=0 when
        required_tools is non-empty — the contract rejects it."""
        # Construction itself rejects, so no plan can carry this.
        # We verify the contract invariant holds.
        rt = RequestedTask(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            objective="Analyse",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,  # valid
        )
        assert rt.estimated_tool_calls >= len(rt.required_tools)

    def test_task_intent_also_enforces_tool_budget(self):
        """TaskIntent (used by templates) must enforce the same rule."""
        with pytest.raises(ValidationError):
            TaskIntent(
                intent_id="ti-1",
                domain="support",
                task_type="support_analysis",
                objective="Analyse",
                required_tools=frozenset({"crm_reader.get_tickets"}),
                estimated_tool_calls=0,
            )


# ============================================================================
# P1-A: Customer Recovery Complexity domain must match template
# ============================================================================


class TestCustomerRecoveryDomainConsistency:
    """Customer Recovery Gate must include customer_recovery in the
    ComplexityDecision.domains, even when the caller omits
    signals.domains."""

    def test_customer_recovery_domain_matches_template(self):
        cap = _make_capability(
            agent_id="customer_context_specialist",
            domains=frozenset({"customer_recovery"}),
            supported_tasks=frozenset({"customer_context_summary"}),
            allowed_tools=frozenset({"crm_reader.get_customers"}),
        )
        reg = _make_registry([cap])
        # Omit signals.domains entirely — Gate must still include
        # customer_recovery.
        signals = PlanningSignals(
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert "customer_recovery" in decision.domains


# ============================================================================
# P1-B: Unexpected Gate exceptions must not be swallowed
# ============================================================================


class TestGateExceptionNotSwallowed:
    """Validator must only catch PlanningError from the Gate; unknown
    exceptions must propagate."""

    def test_unexpected_gate_exception_is_not_swallowed(self):
        """A Gate that raises a non-PlanningError must cause the
        Validator to propagate the exception, not silently downgrade
        it to a validation issue."""

        class ExplodingGate:
            def decide(self, request: Any, registry: Any) -> Any:
                raise RuntimeError("unexpected programming bug")

        plan_validator = PlanValidator(gate=ExplodingGate())  # type: ignore[arg-type]
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        # The Validator must propagate the RuntimeError, not catch it.
        with pytest.raises(RuntimeError):
            plan_validator.validate(request, plan, reg)


# ============================================================================
# resolve_expected_intents — shared pure function
# ============================================================================


class TestResolveExpectedIntents:
    """resolve_expected_intents is the single source of truth used by
    both Planner and Validator."""

    def test_resolve_expected_intents_deterministic_workflow_empty(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(event_type="ticket.sla_breached")
        request = _make_request(reg, signals=signals)
        decision = ComplexityDecision(route="deterministic_workflow")
        assert resolve_expected_intents(request, decision) == []

    def test_resolve_expected_intents_single_agent_one_task(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        decision = ComplexityDecision(route="single_agent")
        intents = resolve_expected_intents(request, decision)
        assert len(intents) == 1
        assert intents[0].intent_id == "primary"

    def test_resolve_expected_intents_multi_agent_from_requested_tasks(self):
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
        )
        reg = _make_registry([cap_a, cap_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        assert len(intents) == 2
        assert {i.intent_id for i in intents} == {"rt-a", "rt-b"}
