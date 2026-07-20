"""Phase 3 R4 review counterexample tests.

Covers the 3 P0 issues from the R4 review:

* P0-1: Canonical Reconstruction must not crash on an invalid Intent
  graph.  ``validate_intent_graph`` is shared between Planner and
  Validator; both sides must reject ``duplicate_intent_id`` /
  ``missing_intent_dependency`` / ``intent_cycle`` with stable issue
  codes instead of letting ``KeyError`` escape during Canonical Plan
  reconstruction.
* P0-2: Agent Assignment must be **budget-aware**.  Structural budgets
  (``max_tasks`` / ``max_agent_calls`` / ``max_tool_calls`` /
  ``max_iterations``) are pre-checked before searching, and each
  candidate combination is filtered by DAG critical-path deadline
  before the deterministic sort picks the cheapest feasible combo.
* P0-3: READ Intent cannot carry a PROPOSE/EXECUTE tool.  Intent
  ``preferred_authority`` must cover the highest authority required
  by any of its ``required_tools``.  Silent auto-elevation is
  forbidden — :class:`PlanningInputError` fails closed.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    ComplexityDecision,
    ExecutionBudget,
    ToolAuthority,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    CODE_INTENT_CYCLE,
    CODE_INTENT_DUPLICATE_ID,
    CODE_INTENT_MISSING_DEPENDENCY,
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    TaskIntent,
    resolve_agent_assignment,
    resolve_expected_intents,
    validate_intent_graph,
    validate_intent_tool_authority,
)
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    PlanningInputError,
)
from multi_agent.plan_validator import (
    PlanValidator,
    CODE_TOOL_AUTHORITY_MISMATCH,
    CODE_TASK_FIELD_MISMATCH,
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
    version: str = "1.0.0",
    **overrides: Any,
) -> AgentCapability:
    defaults: dict[str, Any] = dict(
        agent_id=agent_id,
        version=version,
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


def _make_intent(
    intent_id: str = "i-1",
    domain: str = "support",
    task_type: str = "support_analysis",
    objective: str = "Analyse issue",
    preferred_authority: AgentAuthority = AgentAuthority.READ,
    dependencies: list[str] | None = None,
    required_tools: frozenset[str] | None = None,
    estimated_tool_calls: int = 1,
    metadata: dict[str, Any] | None = None,
) -> TaskIntent:
    return TaskIntent(
        intent_id=intent_id,
        domain=domain,
        task_type=task_type,
        objective=objective,
        preferred_authority=preferred_authority,
        dependencies=dependencies or [],
        required_tools=required_tools or frozenset(),
        estimated_tool_calls=estimated_tool_calls,
        metadata=metadata or {},
    )


# ============================================================================
# P0-1: Canonical Reconstruction must not crash on invalid Intent graph
# ============================================================================


class TestIntentGraphValidation:
    """R4 P0-1: Planner and Validator share ``validate_intent_graph``.
    A tampered request with missing deps / duplicate IDs / cycles must
    produce a stable Validation Issue instead of letting ``KeyError``
    escape during Canonical Plan reconstruction."""

    def test_validator_rejects_request_with_missing_intent_dependency(self):
        """Request contains a RequestedTask with a dependency on a
        non-existent intent_id.  Validator must return
        ``CODE_INTENT_MISSING_DEPENDENCY`` instead of letting
        ``build_expected_planned_tasks`` raise ``KeyError``."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap_a])
        # rt-a depends on "missing" — an intent_id that doesn't exist.
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
            dependencies=["missing"],
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a"}),
            requested_tasks=[rt_a],
        )
        request = _make_request(reg, signals=signals)
        # Build a minimal PlanDraft so the Validator has something to
        # walk; tasks list can be empty since the Intent-graph check
        # runs before Canonical Plan reconstruction.
        from multi_agent.planning import compute_request_hash

        draft = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="single_agent"),
            tasks=[],
            planner_version="ma-03.4.0",
            summary="",
            warnings=[],
        )
        report = PlanValidator().validate(request, draft, reg)
        codes = [i.code for i in report.issues]
        assert CODE_INTENT_MISSING_DEPENDENCY in codes
        assert not report.valid

    def test_validator_rejects_duplicate_request_intent_id(self):
        """Request contains two RequestedTasks with the same intent_id.
        Validator must return ``CODE_INTENT_DUPLICATE_ID``."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap_a])
        rt_a = _make_requested_task(
            intent_id="rt-dup",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-dup",  # duplicate
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        from multi_agent.planning import compute_request_hash

        draft = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=[],
            planner_version="ma-03.4.0",
            summary="",
            warnings=[],
        )
        report = PlanValidator().validate(request, draft, reg)
        codes = [i.code for i in report.issues]
        assert CODE_INTENT_DUPLICATE_ID in codes
        assert not report.valid

    def test_validator_rejects_request_intent_cycle(self):
        """Request contains two RequestedTasks that depend on each
        other.  Validator must return ``CODE_INTENT_CYCLE``."""
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
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
            dependencies=["rt-b"],  # cycle: a → b → a
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
            dependencies=["rt-a"],
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        from multi_agent.planning import compute_request_hash

        draft = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=[],
            planner_version="ma-03.4.0",
            summary="",
            warnings=[],
        )
        report = PlanValidator().validate(request, draft, reg)
        codes = [i.code for i in report.issues]
        assert CODE_INTENT_CYCLE in codes
        assert not report.valid

    def test_canonical_reconstruction_never_raises_key_error(self):
        """Direct call to ``validate_intent_graph`` on an invalid graph
        must return issue codes, never raise ``KeyError`` /
        ``IndexError`` / etc."""
        # Missing dependency.
        intents = [
            _make_intent(intent_id="i-1", dependencies=["missing"]),
        ]
        result = validate_intent_graph(intents)
        assert isinstance(result, list)
        assert CODE_INTENT_MISSING_DEPENDENCY in result

        # Duplicate intent_id.
        intents = [
            _make_intent(intent_id="i-dup"),
            _make_intent(intent_id="i-dup"),
        ]
        result = validate_intent_graph(intents)
        assert isinstance(result, list)
        assert CODE_INTENT_DUPLICATE_ID in result

        # Cycle.
        intents = [
            _make_intent(intent_id="i-a", dependencies=["i-b"]),
            _make_intent(intent_id="i-b", dependencies=["i-a"]),
        ]
        result = validate_intent_graph(intents)
        assert isinstance(result, list)
        assert CODE_INTENT_CYCLE in result

        # Empty list — no issues, no exception.
        assert validate_intent_graph([]) == []


# ============================================================================
# P0-2: Agent Assignment must be budget-aware
# ============================================================================


class TestBudgetAwareAssignment:
    """R4 P0-2: ``resolve_agent_assignment`` must filter candidate
    combinations by DAG critical-path deadline *before* the
    deterministic sort.  Structural budgets are pre-checked before
    searching."""

    def test_assignment_selects_deadline_feasible_agents(self):
        """Two parallel tasks, two candidate agents each.  The cheaper
        combination violates the deadline; the more expensive one
        satisfies it.  Planner must pick the feasible one."""
        # Task A: a_slow (cheap, slow), a_fast (expensive, fast).
        a_slow = _make_capability(
            agent_id="a_slow",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=9_000,
            cost_class="low",
        )
        a_fast = _make_capability(
            agent_id="a_fast",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=6_000,
            cost_class="medium",
        )
        # Task B: b_fast (cheap, fast), b_mid (expensive, mid).
        b_fast = _make_capability(
            agent_id="b_fast",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=1_000,
            cost_class="low",
        )
        b_mid = _make_capability(
            agent_id="b_mid",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=6_000,
            cost_class="medium",
        )
        reg = _make_registry([a_slow, a_fast, b_fast, b_mid])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # deadline=7000ms.  Tasks are parallel (no deps) → critical
        # path = max(timeout_a, timeout_b).
        # a_slow + b_fast → max(9000, 1000) = 9000 > 7000 (infeasible).
        # a_fast + b_fast → max(6000, 1000) = 6000 <= 7000 (feasible).
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(deadline_ms=7_000),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        assignment = resolve_agent_assignment(request, decision, intents, reg)
        assert assignment["rt-a"].agent_id == "a_fast"
        assert assignment["rt-b"].agent_id == "b_fast"

    def test_invalid_cheapest_assignment_is_skipped(self):
        """The cheapest combination by sort key (a_slow + b_fast) is
        infeasible.  The assignment must skip it and pick a feasible
        combo, even if the feasible combo has higher cost."""
        # Same setup as above but with an even tighter deadline to
        # force skipping the cheapest combo.
        a_slow = _make_capability(
            agent_id="a_slow",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=9_000,
            cost_class="low",
        )
        a_fast = _make_capability(
            agent_id="a_fast",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=6_000,
            cost_class="medium",
        )
        b_fast = _make_capability(
            agent_id="b_fast",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=1_000,
            cost_class="low",
        )
        b_mid = _make_capability(
            agent_id="b_mid",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=6_000,
            cost_class="medium",
        )
        reg = _make_registry([a_slow, a_fast, b_fast, b_mid])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # Cheapest: a_slow + b_fast → critical path 9000ms (infeasible).
        # Feasible: a_fast + b_fast → 6000ms (within 7000ms deadline).
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(deadline_ms=7_000),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        assignment = resolve_agent_assignment(request, decision, intents, reg)
        # The cheapest combo (a_slow + b_fast) must NOT be selected.
        assert assignment["rt-a"].agent_id != "a_slow"
        assert assignment["rt-a"].agent_id == "a_fast"

    def test_feasible_more_expensive_assignment_is_selected(self):
        """A more expensive combo that's deadline-feasible must be
        selected over a cheaper combo that's deadline-infeasible."""
        # Two parallel tasks.  Only the expensive combo is feasible.
        # cheap_a is cheap but slow; exp_a is expensive but fast.
        cheap_a = _make_capability(
            agent_id="cheap_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=10_000,
            cost_class="low",
        )
        exp_a = _make_capability(
            agent_id="exp_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=3_000,
            cost_class="high",
        )
        cheap_b = _make_capability(
            agent_id="cheap_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=1_000,
            cost_class="low",
        )
        reg = _make_registry([cheap_a, exp_a, cheap_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # Deadline 5000ms.
        # cheap_a + cheap_b → max(10000, 1000) = 10000 > 5000 (infeasible).
        # exp_a + cheap_b → max(3000, 1000) = 3000 <= 5000 (feasible).
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(deadline_ms=5_000),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        assignment = resolve_agent_assignment(request, decision, intents, reg)
        assert assignment["rt-a"].agent_id == "exp_a"
        assert assignment["rt-b"].agent_id == "cheap_b"

    def test_tool_budget_checked_before_assignment_search(self):
        """If ``sum(estimated_tool_calls) > max_tool_calls``, the
        Budget error must be raised before the search begins."""
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
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=5,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=5,
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # 5 + 5 = 10 > max_tool_calls=8.
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(max_tool_calls=8),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        with pytest.raises(BudgetExceededPlanningError):
            resolve_agent_assignment(request, decision, intents, reg)

    def test_iteration_budget_checked_before_assignment_search(self):
        """If ``longest_path_node_count(intents) > max_iterations``,
        the Budget error must be raised before the search begins."""
        # Build a linear chain of 3 intents: rt-a → rt-b → rt-c.
        # Longest path = 3 nodes.  Set max_iterations=2.
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
        cap_c = _make_capability(
            agent_id="agent_c",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_c"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap_a, cap_b, cap_c])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
            dependencies=["rt-a"],
        )
        rt_c = _make_requested_task(
            intent_id="rt-c",
            domain="support",
            task_type="task_c",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
            dependencies=["rt-b"],
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b", "task_c"}),
            requested_tasks=[rt_a, rt_b, rt_c],
        )
        # Chain length 3 > max_iterations=2.
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(max_iterations=2),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        with pytest.raises(BudgetExceededPlanningError):
            resolve_agent_assignment(request, decision, intents, reg)

    def test_no_budget_feasible_assignment_fails_closed(self):
        """When diverse assignments exist but none are
        deadline-feasible, the planner must raise
        ``BudgetExceededPlanningError`` (not
        ``UnsupportedCapabilityError``)."""
        # Two slow agents on different domains — every combo exceeds
        # the tight deadline.
        slow_a = _make_capability(
            agent_id="slow_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=10_000,
        )
        slow_b = _make_capability(
            agent_id="slow_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=10_000,
        )
        reg = _make_registry([slow_a, slow_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # Deadline 5000ms; both agents timeout 10000ms → infeasible.
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(deadline_ms=5_000),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        with pytest.raises(BudgetExceededPlanningError):
            resolve_agent_assignment(request, decision, intents, reg)


# ============================================================================
# P0-3: Intent Authority must cover Required Tool Authority
# ============================================================================


class TestIntentToolAuthorityAlignment:
    """R4 P0-3: a READ intent cannot carry a PROPOSE/EXECUTE tool.
    Silent auto-elevation is forbidden — :class:`PlanningInputError`
    fails closed."""

    def test_read_intent_cannot_require_propose_tool(self):
        """A READ intent with ``required_tools={'crm_writer.propose'}``
        must raise ``PlanningInputError``."""
        cap = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
            cost_class="medium",
        )
        reg = _make_registry([cap])
        # READ intent requires a PROPOSE tool — invalid.
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_writer.propose"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        # Planner must raise PlanningInputError before assignment.
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_propose_tool_requires_propose_intent(self):
        """A PROPOSE intent with ``required_tools={'crm_writer.propose'}``
        must succeed (positive case)."""
        cap = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
            cost_class="medium",
        )
        reg = _make_registry([cap])
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.PROPOSE,
            required_tools=frozenset({"crm_writer.propose"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        # Must not raise.
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "propose_agent"

    def test_tool_authority_and_intent_authority_are_consistent(self):
        """Direct call to ``validate_intent_tool_authority`` — READ
        tool + READ intent = OK; PROPOSE tool + READ intent = error;
        PROPOSE tool + PROPOSE intent = OK."""
        cap = _make_capability(
            agent_id="agent_x",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_leads", "crm_writer.propose"}),
        )
        reg = _make_registry([cap])

        # READ tool + READ intent → OK.
        read_intent = _make_intent(
            intent_id="i-read",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_reader.get_leads"}),
        )
        # Should not raise.
        validate_intent_tool_authority(read_intent, reg)

        # PROPOSE tool + READ intent → error.
        bad_intent = _make_intent(
            intent_id="i-bad",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_writer.propose"}),
        )
        with pytest.raises(PlanningInputError):
            validate_intent_tool_authority(bad_intent, reg)

        # PROPOSE tool + PROPOSE intent → OK.
        propose_intent = _make_intent(
            intent_id="i-propose",
            preferred_authority=AgentAuthority.PROPOSE,
            required_tools=frozenset({"crm_writer.propose"}),
        )
        # Should not raise.
        validate_intent_tool_authority(propose_intent, reg)

    def test_write_tool_cannot_be_hidden_inside_read_task(self):
        """End-to-end: a request with a READ task that hides a PROPOSE
        tool must be rejected by the Validator with
        ``CODE_TOOL_AUTHORITY_MISMATCH`` when the planner is bypassed
        (simulating a tampered request)."""
        cap = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
            cost_class="medium",
        )
        reg = _make_registry([cap])
        # READ intent with a hidden PROPOSE tool.
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_writer.propose"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        # Bypass the planner (which would raise PlanningInputError)
        # and feed the request directly to the Validator to confirm
        # the stable Issue code path.
        from multi_agent.planning import compute_request_hash

        draft = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="single_agent"),
            tasks=[],
            planner_version="ma-03.4.0",
            summary="",
            warnings=[],
        )
        report = PlanValidator().validate(request, draft, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TOOL_AUTHORITY_MISMATCH in codes
        assert not report.valid


# ============================================================================
# P1-1: PlannedTask.planning_metadata participates in Canonical Plan
# ============================================================================


class TestPlanningMetadata:
    """R4 P1-1: ``PlannedTask.planning_metadata`` is copied verbatim
    from ``TaskIntent.metadata`` by ``build_expected_planned_tasks``.
    It enters Plan Hash and Canonical Plan comparison so tampering
    with template/phase metadata is detectable."""

    def test_planning_metadata_mismatch_detected(self):
        """Tamper ``planning_metadata`` on a PlannedTask →
        ``CODE_TASK_FIELD_MISMATCH``."""
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
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            estimated_tool_calls=1,
        )
        # Embed metadata so it survives resolve_expected_intents.
        rt_a = rt_a.model_copy(update={"metadata": {"phase": "context"}})
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        original = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        assert original.planning_metadata == {"phase": "context"}

        # Tamper: replace planning_metadata with a different dict.
        from multi_agent.contracts import AgentTask

        tampered_task = AgentTask(
            **{
                **original.task.model_dump(),
            }
        )
        tampered_pt = PlannedTask(
            **{
                **original.model_dump(exclude={"task", "planning_metadata"}),
                "task": tampered_task,
                "planning_metadata": {"phase": "tampered"},
            }
        )
        # Recompute plan_hash with the tampered task list.
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        object.__setattr__(plan, "tasks", tasks)
        new_hash = plan.compute_plan_hash()
        object.__setattr__(plan, "plan_hash", new_hash)

        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid
