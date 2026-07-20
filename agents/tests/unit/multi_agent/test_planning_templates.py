"""Customer Recovery template + integration tests — Phase 3.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from multi_agent.complexity_gate import (
    CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    RuleBasedComplexityGate,
)
from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    ExecutionBudget,
    ToolAuthority,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import PlanningRequest, PlanningSignals
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    UnsupportedCapabilityError,
)
from multi_agent.planning_templates import (
    CUSTOMER_RECOVERY_DOMAIN,
    DEFAULT_CUSTOMER_RECOVERY_TEMPLATE,
    INTENT_CUSTOMER_CONTEXT,
    INTENT_KNOWLEDGE_RECOMMENDATION,
    INTENT_RECOVERY_METRICS,
    INTENT_SALES_RISK_ANALYSIS,
    INTENT_SUPPORT_ANALYSIS,
    CustomerRecoveryTemplate,
)
from multi_agent.plan_validator import PlanValidator
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

# Helpers ----------------------------------------------------------------


def _make_capability(
    agent_id: str,
    domains: frozenset[str],
    supported_tasks: frozenset[str],
    allowed_tools: frozenset[str],
    enabled: bool = True,
    authority: AgentAuthority = AgentAuthority.READ,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Agent {agent_id}",
        domains=domains,
        supported_tasks=supported_tasks,
        allowed_tools=allowed_tools,
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=30_000,
        max_retries=2,
        estimated_cost_class="low",
        enabled=enabled,
    )


class _FakeHandler:
    async def run(self, task: Any, context: Any) -> Any:  # pragma: no cover
        raise RuntimeError("Phase 3 tests never call handlers")


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


def _customer_recovery_caps(
    *,
    exclude: str | None = None,
    disable: str | None = None,
) -> list[AgentCapability]:
    """Five agents covering the Customer Recovery template."""
    domain = CUSTOMER_RECOVERY_DOMAIN
    all_caps = [
        _make_capability(
            agent_id="customer_context_specialist",
            domains=frozenset({domain}),
            supported_tasks=frozenset({"customer_context_summary"}),
            allowed_tools=frozenset({"crm_reader.get_customers"}),
            enabled=disable != "customer_context_specialist",
        ),
        _make_capability(
            agent_id="support_specialist",
            domains=frozenset({domain}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            enabled=disable != "support_specialist",
        ),
        _make_capability(
            agent_id="sales_specialist",
            domains=frozenset({domain}),
            supported_tasks=frozenset({"sales_risk_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            enabled=disable != "sales_specialist",
        ),
        _make_capability(
            agent_id="knowledge_specialist",
            domains=frozenset({domain}),
            supported_tasks=frozenset({"knowledge_recommendation"}),
            allowed_tools=frozenset({"vector_search.search"}),
            enabled=disable != "knowledge_specialist",
        ),
        _make_capability(
            agent_id="analytics_specialist",
            domains=frozenset({domain}),
            supported_tasks=frozenset({"recovery_metrics"}),
            allowed_tools=frozenset({"crm_reader.get_customers"}),
            enabled=disable != "analytics_specialist",
        ),
    ]
    if exclude:
        all_caps = [c for c in all_caps if c.agent_id != exclude]
    return all_caps


def _make_registry(
    caps: list[AgentCapability],
    catalog: ToolCatalog | None = None,
) -> AgentRegistry:
    reg = AgentRegistry(tool_catalog=catalog or _default_catalog())
    for cap in caps:
        reg.register(cap, _FakeHandler())
    return reg


def _make_recovery_request(
    registry: AgentRegistry,
    budget: ExecutionBudget | None = None,
) -> PlanningRequest:
    signals = PlanningSignals(
        domains=frozenset({CUSTOMER_RECOVERY_DOMAIN}),
        requested_task_types=frozenset({"support_analysis"}),
        objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    )
    return PlanningRequest(
        run_id="run-recovery-001",
        tenant_id="t-001",
        actor_type="user",
        actor_id="user-001",
        objective="Customer recovery plan",
        signals=signals,
        budget=budget or ExecutionBudget(),
        registry_version=registry.snapshot().version,
    )


# Tests ------------------------------------------------------------------


class TestTemplateDescriptor:
    def test_template_emits_five_intents(self):
        intents = DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()
        assert len(intents) == 5

    def test_template_intent_ids(self):
        intents = DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()
        ids = {i.intent_id for i in intents}
        assert ids == {
            INTENT_CUSTOMER_CONTEXT,
            INTENT_SUPPORT_ANALYSIS,
            INTENT_SALES_RISK_ANALYSIS,
            INTENT_KNOWLEDGE_RECOMMENDATION,
            INTENT_RECOVERY_METRICS,
        }

    def test_template_required_flags(self):
        """context/support/sales required; knowledge/metrics optional."""
        intents = DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()
        by_id = {i.intent_id: i for i in intents}
        assert by_id[INTENT_CUSTOMER_CONTEXT].required is True
        assert by_id[INTENT_SUPPORT_ANALYSIS].required is True
        assert by_id[INTENT_SALES_RISK_ANALYSIS].required is True
        assert by_id[INTENT_KNOWLEDGE_RECOMMENDATION].required is False
        assert by_id[INTENT_RECOVERY_METRICS].required is False

    def test_template_dependencies(self):
        """All non-root intents depend on customer_context."""
        intents = DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()
        by_id = {i.intent_id: i for i in intents}
        assert by_id[INTENT_CUSTOMER_CONTEXT].dependencies == []
        for iid in [
            INTENT_SUPPORT_ANALYSIS,
            INTENT_SALES_RISK_ANALYSIS,
            INTENT_KNOWLEDGE_RECOMMENDATION,
            INTENT_RECOVERY_METRICS,
        ]:
            assert INTENT_CUSTOMER_CONTEXT in by_id[iid].dependencies

    def test_template_no_executor_intents(self):
        """Template must not emit reviewer/synthesizer/executor intents."""
        intents = DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()
        forbidden = {"reviewer", "synthesizer", "executor"}
        for intent in intents:
            assert intent.intent_id not in forbidden
            assert intent.task_type not in {"review", "synthesize", "execute"}
            assert intent.preferred_authority is not AgentAuthority.EXECUTE

    def test_template_has_no_customer_data(self):
        """Template must not carry customer IDs, tenant IDs, or secrets."""
        template = CustomerRecoveryTemplate()
        assert template.name == "customer_recovery"
        # No customer_id / tenant_id / secret fields on the descriptor.
        fields = set(type(template).model_fields.keys())
        assert "customer_id" not in fields
        assert "tenant_id" not in fields
        assert "secret" not in fields
        assert "api_key" not in fields


class TestCustomerRecoveryIntegration:
    def test_full_pipeline_gate_planner_validator(self):
        """Gate → multi_agent; Planner → 5 tasks; Validator → valid=True."""
        reg = _make_registry(_customer_recovery_caps())
        request = _make_recovery_request(reg)

        # Gate
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "multi_agent"

        # Planner
        plan = DeterministicPlanner().create_plan(request, reg)
        assert len(plan.tasks) == 5

        # Validator
        report = PlanValidator().validate(request, plan, reg)
        assert report.valid, (
            f"Expected valid plan, got issues: "
            f"{[(i.code, i.message) for i in report.issues]}"
        )

    def test_topological_order_customer_context_first(self):
        """customer_context must precede all other tasks in topo order."""
        reg = _make_registry(_customer_recovery_caps())
        request = _make_recovery_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        report = PlanValidator().validate(request, plan, reg)

        # Find the task_id of customer_context.
        ctx_pt = next(
            pt for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT
        )
        ctx_task_id = ctx_pt.task.task_id

        # customer_context must be at index 0 in topological order.
        assert report.topological_order[0] == ctx_task_id
        # All other tasks must come after.
        for pt in plan.tasks:
            if pt.intent_id == INTENT_CUSTOMER_CONTEXT:
                continue
            assert report.topological_order.index(pt.task.task_id) > 0

    def test_no_forbidden_roles_in_plan(self):
        """Plan must not contain reviewer/synthesizer/executor."""
        reg = _make_registry(_customer_recovery_caps())
        request = _make_recovery_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)

        forbidden_intents = {"reviewer", "synthesizer", "executor"}
        forbidden_task_types = {"review", "synthesize", "execute"}
        for pt in plan.tasks:
            assert pt.intent_id not in forbidden_intents
            assert pt.task.task_type not in forbidden_task_types
            assert pt.task.agent_id not in {
                "reviewer_agent",
                "synthesizer_agent",
                "executor_agent",
            }

    def test_no_execute_tools_required(self):
        """No PlannedTask may require an EXECUTE-level tool."""
        reg = _make_registry(_customer_recovery_caps())
        request = _make_recovery_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        for pt in plan.tasks:
            for tool_name in pt.required_tools:
                tool = reg.tool_catalog.resolve(tool_name)
                assert tool.authority is not ToolAuthority.EXECUTE

    def test_five_distinct_agents(self):
        """All 5 tasks should use 5 distinct agents."""
        reg = _make_registry(_customer_recovery_caps())
        request = _make_recovery_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        agent_ids = {pt.task.agent_id for pt in plan.tasks}
        assert len(agent_ids) == 5


class TestFailClosedScenarios:
    def test_missing_sales_specialist_fails_closed(self):
        """Without sales_specialist, planner fails closed."""
        reg = _make_registry(_customer_recovery_caps(exclude="sales_specialist"))
        request = _make_recovery_request(reg)
        with pytest.raises(UnsupportedCapabilityError):
            DeterministicPlanner().create_plan(request, reg)

    def test_disabled_support_specialist_fails_closed(self):
        """With support_specialist disabled, planner fails closed."""
        reg = _make_registry(_customer_recovery_caps(disable="support_specialist"))
        request = _make_recovery_request(reg)
        with pytest.raises(UnsupportedCapabilityError):
            DeterministicPlanner().create_plan(request, reg)

    def test_max_tasks_budget_exceeded(self):
        """max_tasks=3 with 5 tasks → BudgetExceededPlanningError."""
        reg = _make_registry(_customer_recovery_caps())
        request = _make_recovery_request(
            reg,
            budget=ExecutionBudget(max_tasks=3),
        )
        with pytest.raises(BudgetExceededPlanningError):
            DeterministicPlanner().create_plan(request, reg)


class TestTemplateCustomization:
    def test_knowledge_can_be_required(self):
        """Template can be configured to make knowledge required."""
        template = CustomerRecoveryTemplate(knowledge_recommendation_required=True)
        intents = template.build_intents()
        knowledge = next(
            i for i in intents if i.intent_id == INTENT_KNOWLEDGE_RECOMMENDATION
        )
        assert knowledge.required is True

    def test_metrics_can_be_required(self):
        """Template can be configured to make metrics required."""
        template = CustomerRecoveryTemplate(recovery_metrics_required=True)
        intents = template.build_intents()
        metrics = next(i for i in intents if i.intent_id == INTENT_RECOVERY_METRICS)
        assert metrics.required is True
