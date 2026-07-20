"""Phase 3 R1 review counterexample tests.

Covers the 5 P0 issues + 2 P1 issues from the R1 review:

* P0-1: PlanDraft request_hash must bind the full request snapshot.
* P0-2: Validator must re-run the Complexity Gate.
* P0-3: Generic multi-agent planner must use explicit RequestedTask mapping.
* P0-4: Authority hierarchy must be enforced (agent >= task preferred).
* P0-5: Intent dependencies must fail-closed (no silent drops).
* P1-A: Kafka topic mapping must be semantically exact.
* P1-B: Planner error types must match validation issue codes.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from multi_agent.complexity_gate import (
    CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    KAFKA_TOPIC_TO_EVENT_TYPE,
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
)
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    PlanCycleError,
    PlanIntegrityError,
    PlanningInputError,
)
from multi_agent.plan_validator import (
    PlanValidator,
    CODE_COMPLEXITY_DECISION_MISMATCH,
    CODE_INSUFFICIENT_AGENT_AUTHORITY,
    CODE_REQUEST_HASH_MISMATCH,
    CODE_REQUEST_SNAPSHOT_MISMATCH,
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
        estimated_cost_class="low",
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
) -> RequestedTask:
    return RequestedTask(
        intent_id=intent_id,
        domain=domain,
        task_type=task_type,
        objective=objective,
        preferred_authority=preferred_authority,
        dependencies=dependencies or [],
        required=required,
    )


# ============================================================================
# P0-1: PlanDraft request_hash must bind the full request snapshot
# ============================================================================


class TestPlanDraftRequestBinding:
    """PlanDraft stores the full PlanningRequest snapshot so that
    actor/objective/signals/budget mutations are all detectable."""

    def _make_plan(self) -> tuple[PlanDraft, AgentRegistry, PlanningRequest]:
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        return plan, reg, request

    def test_actor_mutation_invalidates_plan(self):
        """Mutating request.actor_id after construction must fail
        verify_integrity() because request_hash no longer matches."""
        plan, reg, request = self._make_plan()
        # Mutate the request snapshot inside the plan.
        object.__setattr__(plan.request, "actor_id", "attacker")
        with pytest.raises(PlanIntegrityError):
            plan.verify_integrity()

    def test_objective_mutation_invalidates_plan(self):
        """Mutating request.objective after construction must fail
        verify_integrity() because request_hash no longer matches."""
        plan, reg, request = self._make_plan()
        object.__setattr__(plan.request, "objective", "tampered objective")
        with pytest.raises(PlanIntegrityError):
            plan.verify_integrity()

    def test_forged_request_hash_rejected(self):
        """Constructing a PlanDraft with a forged request_hash must fail."""
        plan, reg, request = self._make_plan()
        data = plan.model_dump(mode="json")
        data["request_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            PlanDraft(**data)

    def test_validator_rejects_request_hash_mismatch(self):
        """Validator must emit CODE_REQUEST_HASH_MISMATCH when
        plan.request_hash != compute_request_hash(request)."""
        plan, reg, request = self._make_plan()
        # Build a different request (different actor_id).
        different_request = _make_request(reg, actor_id="other-user")
        # plan.request_hash was computed from the original request.
        report = PlanValidator().validate(different_request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUEST_HASH_MISMATCH in codes
        assert CODE_REQUEST_SNAPSHOT_MISMATCH in codes
        assert not report.valid

    def test_validator_rejects_actor_mismatch(self):
        """Validator must reject when plan.request.actor_id != request.actor_id."""
        plan, reg, request = self._make_plan()
        different_request = _make_request(reg, actor_id="other-user")
        report = PlanValidator().validate(different_request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUEST_SNAPSHOT_MISMATCH in codes
        assert not report.valid

    def test_validator_rejects_objective_mismatch(self):
        """Validator must reject when plan.request.objective != request.objective."""
        plan, reg, request = self._make_plan()
        different_request = _make_request(reg, objective="different objective")
        report = PlanValidator().validate(different_request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUEST_SNAPSHOT_MISMATCH in codes
        assert not report.valid


# ============================================================================
# P0-2: Validator must re-run the Complexity Gate
# ============================================================================


class TestValidatorComplexityRecompute:
    """Validator injects a ComplexityGate and re-runs decide() to verify
    plan.complexity matches what the gate would produce."""

    def test_validator_recomputes_complexity(self):
        """A plan whose complexity.route doesn't match the gate's decision
        must be rejected with CODE_COMPLEXITY_DECISION_MISMATCH."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Forcibly swap route to multi_agent (gate says single_agent).
        object.__setattr__(
            plan,
            "complexity",
            ComplexityDecision(route="multi_agent"),
        )
        # Recompute plan_hash so the hash check doesn't fire.
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_cross_domain_request_rejects_single_route(self):
        """A cross-domain request (gate → multi_agent) must not accept a
        single_agent plan."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"sales_analysis"}),
        )
        reg = _make_registry([cap_a, cap_b])
        # Build a multi-agent request.
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="sales_analysis",
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_analysis"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Forcibly swap to single_agent with 1 task.
        object.__setattr__(
            plan,
            "complexity",
            ComplexityDecision(route="single_agent"),
        )
        _set_plan_tasks(plan, [plan.tasks[0]])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_customer_recovery_rejects_single_route(self):
        """Customer Recovery (gate → multi_agent) must reject single_agent."""
        from multi_agent.planning_templates import (
            CUSTOMER_RECOVERY_DOMAIN,
        )

        domain = CUSTOMER_RECOVERY_DOMAIN
        caps = [
            _make_capability(
                agent_id="customer_context_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"customer_context_summary"}),
                allowed_tools=frozenset({"crm_reader.get_customers"}),
            ),
            _make_capability(
                agent_id="support_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"support_analysis"}),
                allowed_tools=frozenset({"crm_reader.get_tickets"}),
            ),
            _make_capability(
                agent_id="sales_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"sales_risk_analysis"}),
                allowed_tools=frozenset({"crm_reader.get_deals"}),
            ),
            _make_capability(
                agent_id="knowledge_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"knowledge_recommendation"}),
                allowed_tools=frozenset({"vector_search.search"}),
            ),
            _make_capability(
                agent_id="analytics_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"recovery_metrics"}),
                allowed_tools=frozenset({"crm_reader.get_customers"}),
            ),
        ]
        reg = _make_registry(caps)
        signals = _make_signals(
            domains=frozenset({domain}),
            requested_task_types=frozenset({"support_analysis"}),
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Forcibly swap to single_agent.
        object.__setattr__(
            plan,
            "complexity",
            ComplexityDecision(route="single_agent"),
        )
        _set_plan_tasks(plan, [plan.tasks[0]])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_fixed_event_rejects_multi_route(self):
        """A fixed-event request (gate → deterministic_workflow) must not
        accept a multi_agent plan."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(event_type="ticket.sla_breached")
        request = _make_request(reg, signals=signals)
        # Gate says deterministic_workflow, planner returns empty plan.
        plan = DeterministicPlanner().create_plan(request, reg)
        # Forcibly swap to multi_agent with 0 tasks.
        object.__setattr__(
            plan,
            "complexity",
            ComplexityDecision(route="multi_agent"),
        )
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_reason_code_mismatch_rejected(self):
        """If the gate produces different reason codes, validator must reject."""
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
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Forcibly change reasons (route stays multi_agent).
        gate_decision = RuleBasedComplexityGate().decide(request, reg)
        tampered_complexity = ComplexityDecision(
            route=gate_decision.route,
            domains=gate_decision.domains,
            reasons=[],  # Wrong reasons.
            confidence=gate_decision.confidence,
            requires_human_review=gate_decision.requires_human_review,
        )
        object.__setattr__(plan, "complexity", tampered_complexity)
        object.__setattr__(plan, "plan_hash", plan.compute_plan_hash())
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid


def _set_plan_tasks(plan: PlanDraft, tasks: list[PlannedTask]) -> PlanDraft:
    """Bypass validate_assignment to swap tasks, then recompute hash."""
    object.__setattr__(plan, "tasks", tasks)
    new_hash = plan.compute_plan_hash()
    object.__setattr__(plan, "plan_hash", new_hash)
    return plan


# ============================================================================
# P0-3: Generic multi-agent planner must use explicit RequestedTask mapping
# ============================================================================


class TestMultiAgentTaskMapping:
    """Non-template multi_agent plans must come from explicit
    signals.requested_tasks.  Guessing domains or cartesian products is
    forbidden."""

    def test_two_domains_without_mapping_fails_closed(self):
        """multi_agent route with 2 domains but no requested_tasks →
        PlanningInputError (cannot infer domain→task mapping)."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support", "sales"}),
            supported_tasks=frozenset({"support_analysis", "sales_analysis"}),
        )
        reg = _make_registry([cap_a])
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_analysis"}),
        )
        request = _make_request(reg, signals=signals)
        # Gate → multi_agent (2 domains).  No requested_tasks → fail closed.
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_all_requested_domains_preserved(self):
        """When requested_tasks span 2 domains, the plan must include tasks
        in BOTH domains — not just sorted(domains)[0]."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"sales_analysis"}),
        )
        reg = _make_registry([cap_a, cap_b])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="sales_analysis",
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_analysis"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        plan_domains = {pt.domain for pt in plan.tasks}
        assert plan_domains == {"support", "sales"}
        assert len(plan.tasks) == 2

    def test_conflicting_signals_without_independent_tasks_fails_closed(self):
        """has_conflicting_signals=True routes to multi_agent, but without
        requested_tasks the planner cannot build independent tasks →
        PlanningInputError."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support", "sales"}),
            supported_tasks=frozenset({"support_analysis", "sales_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_analysis"}),
            has_conflicting_signals=True,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_requested_task_domain_mapping_is_preserved(self):
        """Each RequestedTask's domain must appear on the corresponding
        PlannedTask — no reassignment to sorted(domains)[0]."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"sales_analysis"}),
        )
        cap_c = _make_capability(
            agent_id="agent_c",
            domains=frozenset({"marketing"}),
            supported_tasks=frozenset({"marketing_analysis"}),
        )
        reg = _make_registry([cap_a, cap_b, cap_c])
        # Note: domains sorted → ["marketing", "sales", "support"].
        # If the planner picked sorted(domains)[0], all tasks would be
        # "marketing".  Explicit mapping must preserve per-task domain.
        rt_support = _make_requested_task(
            intent_id="rt-support",
            domain="support",
            task_type="support_analysis",
        )
        rt_sales = _make_requested_task(
            intent_id="rt-sales",
            domain="sales",
            task_type="sales_analysis",
        )
        rt_marketing = _make_requested_task(
            intent_id="rt-marketing",
            domain="marketing",
            task_type="marketing_analysis",
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales", "marketing"}),
            requested_task_types=frozenset(
                {
                    "support_analysis",
                    "sales_analysis",
                    "marketing_analysis",
                }
            ),
            requested_tasks=[rt_support, rt_sales, rt_marketing],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Map intent_id → domain.
        domain_by_intent = {pt.intent_id: pt.domain for pt in plan.tasks}
        assert domain_by_intent["rt-support"] == "support"
        assert domain_by_intent["rt-sales"] == "sales"
        assert domain_by_intent["rt-marketing"] == "marketing"


# ============================================================================
# P0-4: Authority hierarchy enforcement
# ============================================================================


class TestAuthorityEnforcement:
    """Validator must reject plans where the selected agent's authority is
    below the task's preferred_authority.  Planner must propagate
    preferred_authority from RequestedTask, and requires_write/approval
    must produce at least one PROPOSE task."""

    def test_propose_intent_rejects_read_agent(self):
        """A task with preferred_authority=PROPOSE assigned to a READ agent
        → CODE_INSUFFICIENT_AGENT_AUTHORITY."""
        cap_a = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_b = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
        )
        reg = _make_registry([cap_a, cap_b])
        # Build a multi-agent request where one task needs PROPOSE.
        # has_conflicting_signals forces multi_agent route (mixed authority
        # requirements are a form of conflicting signals).
        rt_read = _make_requested_task(
            intent_id="rt-read",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
        )
        rt_propose = _make_requested_task(
            intent_id="rt-propose",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.PROPOSE,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_read, rt_propose],
            has_conflicting_signals=True,
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Planner should select propose_agent for rt-propose; verify.
        propose_pt = next(pt for pt in plan.tasks if pt.intent_id == "rt-propose")
        assert propose_pt.task.agent_id == "propose_agent"
        # Now forcibly reassign the PROPOSE task to the READ agent.
        tampered_task = AgentTask(
            task_id=propose_pt.task.task_id,
            agent_id="read_agent",  # READ agent on a PROPOSE task.
            task_type=propose_pt.task.task_type,
            objective=propose_pt.task.objective,
            tenant_id=propose_pt.task.tenant_id,
            dependencies=propose_pt.task.dependencies,
        )
        tampered_pt = PlannedTask(
            intent_id=propose_pt.intent_id,
            domain=propose_pt.domain,
            preferred_authority=AgentAuthority.PROPOSE,
            required_tools=propose_pt.required_tools,
            estimated_tool_calls=propose_pt.estimated_tool_calls,
            required=propose_pt.required,
            task=tampered_task,
        )
        # Replace in plan and recompute hash.
        new_tasks = [
            pt if pt.intent_id != "rt-propose" else tampered_pt for pt in plan.tasks
        ]
        _set_plan_tasks(plan, new_tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_INSUFFICIENT_AGENT_AUTHORITY in codes
        assert not report.valid

    def test_multi_agent_write_request_contains_propose_task(self):
        """requires_write=True with explicit requested_tasks must include at
        least one PROPOSE-level task — otherwise PlanningInputError."""
        cap_read = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_propose = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
        )
        reg = _make_registry([cap_read, cap_propose])
        # has_conflicting_signals forces multi_agent route so both tasks
        # are preserved.  Two READ-only tasks with requires_write=True
        # → fail closed.
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_a, rt_b],
            requires_write=True,
            has_conflicting_signals=True,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_write_request_with_propose_task_succeeds(self):
        """requires_write=True with at least one PROPOSE task → valid plan."""
        cap_read = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_propose = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
        )
        reg = _make_registry([cap_read, cap_propose])
        # has_conflicting_signals forces multi_agent route so both tasks
        # (READ + PROPOSE) are preserved.
        rt_read = _make_requested_task(
            intent_id="rt-read",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
        )
        rt_propose = _make_requested_task(
            intent_id="rt-propose",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.PROPOSE,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_read, rt_propose],
            requires_write=True,
            has_conflicting_signals=True,
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        propose_pt = next(pt for pt in plan.tasks if pt.intent_id == "rt-propose")
        assert propose_pt.preferred_authority is AgentAuthority.PROPOSE
        assert propose_pt.task.agent_id == "propose_agent"

    def test_approval_request_without_propose_intent_fails_closed(self):
        """requires_approval=True with no PROPOSE task → PlanningInputError."""
        cap_read = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap_read])
        # has_conflicting_signals forces multi_agent route so both tasks
        # are preserved; neither is PROPOSE → fail closed.
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="support_analysis",
            preferred_authority=AgentAuthority.READ,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_a, rt_b],
            requires_approval=True,
            has_conflicting_signals=True,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_read_tasks_still_select_read_agent(self):
        """When all tasks are READ, the planner must prefer READ agents
        over PROPOSE agents (minimum privilege)."""
        # Two READ agents (one per task_type) + one PROPOSE agent covering
        # both task_types.  multi_agent route is triggered by 2 task_types.
        cap_read_a = _make_capability(
            agent_id="read_agent_a",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
        )
        cap_read_b = _make_capability(
            agent_id="read_agent_b",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_b"}),
        )
        cap_propose = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
        )
        reg = _make_registry([cap_read_a, cap_read_b, cap_propose])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            preferred_authority=AgentAuthority.READ,
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="task_b",
            preferred_authority=AgentAuthority.READ,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Both tasks should be assigned to READ agents, not propose_agent.
        for pt in plan.tasks:
            assert pt.task.agent_id in {"read_agent_a", "read_agent_b"}
            assert pt.task.agent_id != "propose_agent"


# ============================================================================
# P0-5: Intent dependency fail-closed
# ============================================================================


class TestIntentDependencyFailClosed:
    """Planner must validate intent_id uniqueness, dependency existence,
    and acyclicity BEFORE building AgentTasks.  Missing deps must never
    be silently dropped."""

    def test_missing_intent_dependency_fails_closed(self):
        """An intent referencing a non-existent dependency →
        PlanningInputError."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
            dependencies=["rt-missing"],  # References missing intent.
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_a],
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_duplicate_intent_id_fails_closed(self):
        """Two RequestedTasks with the same intent_id →
        PlanningInputError."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([cap])
        rt_a = _make_requested_task(
            intent_id="rt-dup",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-dup",  # Duplicate.
            domain="support",
            task_type="task_b",
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            DeterministicPlanner().create_plan(request, reg)

    def test_intent_cycle_fails_closed(self):
        """Two intents depending on each other form a cycle →
        PlanCycleError."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([cap])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            dependencies=["rt-b"],
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="task_b",
            dependencies=["rt-a"],
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanCycleError):
            DeterministicPlanner().create_plan(request, reg)

    def test_dependency_is_never_silently_dropped(self):
        """A valid dependency chain must be preserved end-to-end.

        rt-a ← rt-b ← rt-c (chain).  All deps must appear as task_id
        references in the final AgentTask.dependencies.
        """
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
        cap_c = _make_capability(
            agent_id="agent_c",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_c"}),
        )
        reg = _make_registry([cap_a, cap_b, cap_c])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="task_b",
            dependencies=["rt-a"],
        )
        rt_c = _make_requested_task(
            intent_id="rt-c",
            domain="support",
            task_type="task_c",
            dependencies=["rt-b"],
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b", "task_c"}),
            requested_tasks=[rt_a, rt_b, rt_c],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        # Map intent_id → task_id.
        intent_to_task = {pt.intent_id: pt.task.task_id for pt in plan.tasks}
        # rt-b must depend on rt-a's task_id.
        pt_b = next(pt for pt in plan.tasks if pt.intent_id == "rt-b")
        assert intent_to_task["rt-a"] in pt_b.task.dependencies
        # rt-c must depend on rt-b's task_id.
        pt_c = next(pt for pt in plan.tasks if pt.intent_id == "rt-c")
        assert intent_to_task["rt-b"] in pt_c.task.dependencies
        # rt-a must have no dependencies.
        pt_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        assert len(pt_a.task.dependencies) == 0


# ============================================================================
# P1-A: Kafka topic mapping semantic correctness
# ============================================================================


class TestKafkaTopicMapping:
    """KAFKA_TOPIC_TO_EVENT_TYPE must only contain semantically exact
    mappings.  Topics without a precise canonical event_type must be
    absent."""

    REMOVED_MAPPINGS = {
        "crm.knowledge.published": "audit.event_recorded",
        "crm.conversations.closed": "lifecycle.stage_changed",
        "crm.automation.simulation.requested": "automation.triggered",
    }

    def test_semantically_inexact_mappings_removed(self):
        """The three semantically inexact mappings from R1 initial must
        be absent."""
        for topic in self.REMOVED_MAPPINGS:
            assert topic not in KAFKA_TOPIC_TO_EVENT_TYPE, (
                f"Semantically inexact mapping {topic!r} should have been removed"
            )

    def test_all_remaining_mappings_are_exact(self):
        """Every remaining mapping's value must be a canonical
        deterministic_workflow event_type."""
        from multi_agent.complexity_gate import DETERMINISTIC_EVENT_TYPES

        for topic, event_type in KAFKA_TOPIC_TO_EVENT_TYPE.items():
            assert event_type in DETERMINISTIC_EVENT_TYPES, (
                f"Mapping {topic!r} → {event_type!r} is not in "
                f"DETERMINISTIC_EVENT_TYPES"
            )

    def test_deals_stage_changed_maps_to_lifecycle(self):
        """crm.deals.stage-changed → lifecycle.stage_changed (added in R1
        fix, semantically exact)."""
        assert (
            KAFKA_TOPIC_TO_EVENT_TYPE.get("crm.deals.stage-changed")
            == "lifecycle.stage_changed"
        )


# ============================================================================
# P1-B: Planner error type mapping
# ============================================================================


class TestPlannerErrorMapping:
    """Planner must raise the specific error type matching the first
    validation issue code, not always PlanValidationError."""

    def test_budget_issue_raises_budget_error(self):
        """Budget-exceeded issue → BudgetExceededPlanningError."""
        from multi_agent.planning_templates import (
            CUSTOMER_RECOVERY_DOMAIN,
        )

        domain = CUSTOMER_RECOVERY_DOMAIN
        caps = [
            _make_capability(
                agent_id="customer_context_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"customer_context_summary"}),
                allowed_tools=frozenset({"crm_reader.get_customers"}),
            ),
            _make_capability(
                agent_id="support_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"support_analysis"}),
                allowed_tools=frozenset({"crm_reader.get_tickets"}),
            ),
            _make_capability(
                agent_id="sales_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"sales_risk_analysis"}),
                allowed_tools=frozenset({"crm_reader.get_deals"}),
            ),
            _make_capability(
                agent_id="knowledge_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"knowledge_recommendation"}),
                allowed_tools=frozenset({"vector_search.search"}),
            ),
            _make_capability(
                agent_id="analytics_specialist",
                domains=frozenset({domain}),
                supported_tasks=frozenset({"recovery_metrics"}),
                allowed_tools=frozenset({"crm_reader.get_customers"}),
            ),
        ]
        reg = _make_registry(caps)
        signals = _make_signals(
            domains=frozenset({domain}),
            requested_task_types=frozenset({"support_analysis"}),
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(max_tasks=3),
        )
        with pytest.raises(BudgetExceededPlanningError):
            DeterministicPlanner().create_plan(request, reg)

    def test_cycle_issue_raises_cycle_error(self):
        """Cycle issue → PlanCycleError (raised by _validate_intents
        before Validator runs)."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        # has_conflicting_signals forces multi_agent route so both intents
        # are built; the cycle is detected before agent selection.
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
            dependencies=["rt-b"],
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="support",
            task_type="support_analysis",
            dependencies=["rt-a"],
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt_a, rt_b],
            has_conflicting_signals=True,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanCycleError):
            DeterministicPlanner().create_plan(request, reg)
