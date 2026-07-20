"""Deterministic Planner tests — Phase 3.

All tests run under AI_MODE=deterministic; no network, no LLM, no
random IDs, no time-dependent hashes.
"""

from __future__ import annotations

from typing import Any

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    ExecutionBudget,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import PlanningRequest, PlanningSignals
from multi_agent.planning_errors import (
    UnsupportedCapabilityError,
)
from multi_agent.planning_templates import (
    INTENT_CUSTOMER_CONTEXT,
    INTENT_KNOWLEDGE_RECOMMENDATION,
    INTENT_RECOVERY_METRICS,
    INTENT_SALES_RISK_ANALYSIS,
    INTENT_SUPPORT_ANALYSIS,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor
from multi_agent.contracts import ToolAuthority
from multi_agent.complexity_gate import CUSTOMER_RECOVERY_OBJECTIVE_KIND

# Helpers ----------------------------------------------------------------


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    domains: frozenset[str] | None = None,
    supported_tasks: frozenset[str] | None = None,
    allowed_tools: frozenset[str] | None = None,
    enabled: bool = True,
    cost_class: str = "low",
    timeout_ms: int = 30_000,
    version: str = "1.0.0",
    **overrides: Any,
) -> AgentCapability:
    defaults: dict[str, Any] = dict(
        agent_id=agent_id,
        version=version,
        description=f"Agent {agent_id}",
        domains=domains or frozenset({"test"}),
        supported_tasks=supported_tasks or frozenset({"test_task"}),
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
                tool_name="crm_reader.get_leads", authority=ToolAuthority.READ
            ),
            ToolDescriptor(
                tool_name="automation_executor.execute", authority=ToolAuthority.EXECUTE
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
        event_type=None,
        domains=frozenset({"support"}),
        requested_task_types=frozenset({"support_analysis"}),
        requires_cross_domain=False,
        requires_write=False,
        requires_approval=False,
        has_conflicting_signals=False,
        missing_required_context=False,
        objective_kind=None,
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
        context_summary=None,
        registry_version=registry.snapshot().version,
    )
    defaults.update(overrides)
    return PlanningRequest(**defaults)


def _customer_recovery_caps() -> list[AgentCapability]:
    """Five agents covering the Customer Recovery template."""
    domain = "customer_recovery"
    return [
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


# Tests ------------------------------------------------------------------


class TestDeterministicWorkflowPlan:
    def test_deterministic_workflow_produces_empty_plan(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(event_type="ticket.sla_breached")
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.complexity.route == "deterministic_workflow"
        assert plan.tasks == []
        assert plan.plan_hash


class TestSingleAgentPlan:
    def test_single_agent_plan_has_one_task(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.complexity.route == "single_agent"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].task.agent_id == "support_agent"


class TestCustomerRecoveryPlan:
    def test_customer_recovery_plan_has_expected_tasks(self):
        reg = _make_registry(_customer_recovery_caps())
        signals = _make_signals(
            domains=frozenset({"customer_recovery"}),
            requested_task_types=frozenset({"support_analysis"}),
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.complexity.route == "multi_agent"
        assert len(plan.tasks) == 5

        intent_ids = {pt.intent_id for pt in plan.tasks}
        assert intent_ids == {
            INTENT_CUSTOMER_CONTEXT,
            INTENT_SUPPORT_ANALYSIS,
            INTENT_SALES_RISK_ANALYSIS,
            INTENT_KNOWLEDGE_RECOMMENDATION,
            INTENT_RECOVERY_METRICS,
        }

        # customer_context must be the root — no dependencies.
        ctx = next(pt for pt in plan.tasks if pt.intent_id == INTENT_CUSTOMER_CONTEXT)
        assert len(ctx.task.dependencies) == 0
        assert ctx.required is True

        # All other tasks depend on customer_context.
        for pt in plan.tasks:
            if pt.intent_id == INTENT_CUSTOMER_CONTEXT:
                continue
            assert INTENT_CUSTOMER_CONTEXT in [
                pt.task.task_id
                for pt in plan.tasks
                if pt.intent_id == INTENT_CUSTOMER_CONTEXT
            ] or any(
                dep
                in {
                    t.task.task_id
                    for t in plan.tasks
                    if t.intent_id == INTENT_CUSTOMER_CONTEXT
                }
                for dep in pt.task.dependencies
            )

        # support and sales are required; knowledge and metrics are optional.
        support = next(
            pt for pt in plan.tasks if pt.intent_id == INTENT_SUPPORT_ANALYSIS
        )
        sales = next(
            pt for pt in plan.tasks if pt.intent_id == INTENT_SALES_RISK_ANALYSIS
        )
        knowledge = next(
            pt for pt in plan.tasks if pt.intent_id == INTENT_KNOWLEDGE_RECOMMENDATION
        )
        metrics = next(
            pt for pt in plan.tasks if pt.intent_id == INTENT_RECOVERY_METRICS
        )
        assert support.required is True
        assert sales.required is True
        assert knowledge.required is False
        assert metrics.required is False


class TestAgentSelection:
    def test_planner_uses_least_privileged_agent(self):
        """READ agent preferred over PROPOSE when both satisfy."""
        read_cap = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        propose_cap = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_writer.propose"}),
        )
        # Need PROPOSE tool in catalog.
        catalog = ToolCatalog(
            [
                ToolDescriptor(
                    tool_name="crm_reader.get_leads", authority=ToolAuthority.READ
                ),
                ToolDescriptor(
                    tool_name="crm_writer.propose", authority=ToolAuthority.PROPOSE
                ),
            ]
        )
        reg = _make_registry([read_cap, propose_cap], catalog=catalog)
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "read_agent"

    def test_planner_does_not_select_execute_agent(self):
        """EXECUTE agents are filtered out, not failed on sight."""

        # An EXECUTE agent that covers the same domain.
        # Registration of EXECUTE agent with EXECUTE tool should succeed.
        exec_cap = _make_capability(
            agent_id="exec_agent",
            authority=AgentAuthority.EXECUTE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"automation_executor.execute"}),
        )
        read_cap = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        catalog = ToolCatalog(
            [
                ToolDescriptor(
                    tool_name="crm_reader.get_leads", authority=ToolAuthority.READ
                ),
                ToolDescriptor(
                    tool_name="automation_executor.execute",
                    authority=ToolAuthority.EXECUTE,
                ),
            ]
        )
        reg = _make_registry([exec_cap, read_cap], catalog=catalog)
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        # READ agent should be selected, not EXECUTE.
        assert plan.tasks[0].task.agent_id == "read_agent"

    def test_planner_uses_cost_as_tiebreaker(self):
        """When authority is equal, lower cost class wins."""
        low_cost = _make_capability(
            agent_id="agent_a",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            cost_class="low",
        )
        high_cost = _make_capability(
            agent_id="agent_b",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            cost_class="high",
        )
        reg = _make_registry([high_cost, low_cost])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "agent_a"

    def test_planner_uses_agent_id_as_final_tiebreaker(self):
        """When authority + cost are equal, agent_id lexicographic order wins."""
        cap_b = _make_capability(
            agent_id="agent_b",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap_a = _make_capability(
            agent_id="agent_a",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        # Register in reverse order to ensure sort, not insertion order.
        reg = _make_registry([cap_b, cap_a])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "agent_a"

    def test_no_matching_agent_fails_closed(self):
        """No READ/PROPOSE agent → UnsupportedCapabilityError."""
        cap = _make_capability(
            agent_id="other_agent",
            domains=frozenset({"other"}),
            supported_tasks=frozenset({"other_task"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        with pytest.raises(UnsupportedCapabilityError):
            DeterministicPlanner().create_plan(request, reg)

    def test_disabled_agent_not_selected(self):
        """Disabled agents are filtered out."""
        disabled = _make_capability(
            agent_id="disabled_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            enabled=False,
        )
        enabled = _make_capability(
            agent_id="enabled_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([disabled, enabled])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "enabled_agent"


class TestStability:
    def test_task_ids_are_stable(self):
        """Same input → same task IDs."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan1 = DeterministicPlanner().create_plan(request, reg)
        plan2 = DeterministicPlanner().create_plan(request, reg)
        assert [pt.task.task_id for pt in plan1.tasks] == [
            pt.task.task_id for pt in plan2.tasks
        ]

    def test_plan_hash_is_stable(self):
        """Same input → same plan_hash."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan1 = DeterministicPlanner().create_plan(request, reg)
        plan2 = DeterministicPlanner().create_plan(request, reg)
        assert plan1.plan_hash == plan2.plan_hash

    def test_registry_version_changes_plan_hash(self):
        """Different registry → different plan_hash."""
        cap_v1 = _make_capability(
            agent_id="support_agent",
            version="1.0.0",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg_v1 = _make_registry([cap_v1])
        request_v1 = _make_request(reg_v1)

        cap_v2 = _make_capability(
            agent_id="support_agent",
            version="2.0.0",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg_v2 = _make_registry([cap_v2])
        request_v2 = _make_request(reg_v2)

        plan1 = DeterministicPlanner().create_plan(request_v1, reg_v1)
        plan2 = DeterministicPlanner().create_plan(request_v2, reg_v2)
        assert plan1.plan_hash != plan2.plan_hash

    def test_forged_plan_hash_rejected(self):
        """Manually-set wrong plan_hash must fail construction."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)

        # Reconstruct with a forged hash.
        data = plan.model_dump(mode="json")
        data["plan_hash"] = "deadbeef" * 8
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            type(plan)(**data)

    def test_mutated_task_invalidates_plan(self):
        """Mutating a task after construction must fail verify_integrity()."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        plan = DeterministicPlanner().create_plan(request, reg)

        # Mutate the task objective (bypass frozen via object.__setattr__).
        task = plan.tasks[0].task
        object.__setattr__(task, "objective", "tampered")
        from multi_agent.planning_errors import PlanIntegrityError

        with pytest.raises(PlanIntegrityError):
            plan.verify_integrity()


class TestNoSideEffects:
    def test_planner_does_not_mutate_request(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        request_dump_before = request.model_dump(mode="json")
        _ = DeterministicPlanner().create_plan(request, reg)
        request_dump_after = request.model_dump(mode="json")
        assert request_dump_before == request_dump_after

    def test_planner_does_not_mutate_registry(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        snapshot_before = reg.snapshot()
        request = _make_request(reg)
        _ = DeterministicPlanner().create_plan(request, reg)
        snapshot_after = reg.snapshot()
        assert snapshot_before.version == snapshot_after.version

    def test_planner_makes_no_network_calls(self):
        """Planner must not open any socket."""
        import socket

        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)

        original_socket = socket.socket
        call_count = {"n": 0}

        class _GuardSocket:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                call_count["n"] += 1
                raise RuntimeError("network access forbidden in Phase 3 tests")

        try:
            socket.socket = _GuardSocket  # type: ignore[assignment]
            _ = DeterministicPlanner().create_plan(request, reg)
        finally:
            socket.socket = original_socket  # type: ignore[assignment]
        assert call_count["n"] == 0, "Planner opened a socket"
