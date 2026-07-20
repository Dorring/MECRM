"""Complexity Gate tests — Phase 3.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from multi_agent.complexity_gate import (
    CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    DETERMINISTIC_EVENT_TYPES,
    KAFKA_TOPIC_TO_EVENT_TYPE,
    REASON_CONFLICTING_SIGNALS,
    REASON_CROSS_DOMAIN_OBJECTIVE,
    REASON_CUSTOMER_RECOVERY_TEMPLATE,
    REASON_FIXED_EVENT_ALLOWLIST,
    REASON_MULTIPLE_TASK_TYPES,
    REASON_SINGLE_DOMAIN_SINGLE_TASK,
    RuleBasedComplexityGate,
)
from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    ExecutionBudget,
)
from multi_agent.planning import PlanningRequest, PlanningSignals
from multi_agent.planning_errors import (
    InsufficientContextError,
    PlanningInputError,
    RegistryVersionMismatchError,
    UnsupportedCapabilityError,
)
from multi_agent.registry import AgentRegistry, ToolCatalog

# Helpers ----------------------------------------------------------------


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    domains: frozenset[str] | None = None,
    supported_tasks: frozenset[str] | None = None,
    allowed_tools: frozenset[str] | None = None,
    enabled: bool = True,
    **overrides: Any,
) -> AgentCapability:
    defaults: dict[str, Any] = dict(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Agent {agent_id}",
        domains=domains or frozenset({"test"}),
        supported_tasks=supported_tasks or frozenset({"test_task"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_leads"}),
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=30_000,
        max_retries=2,
        estimated_cost_class="low",
        enabled=enabled,
    )
    defaults.update(overrides)
    return AgentCapability(**defaults)


class _FakeHandler:
    async def run(self, task: Any, context: Any) -> Any:  # pragma: no cover
        raise RuntimeError("Phase 3 tests never call handlers")


def _make_registry(
    caps: list[AgentCapability] | None = None,
    catalog: ToolCatalog | None = None,
) -> AgentRegistry:
    reg = AgentRegistry(tool_catalog=catalog or ToolCatalog.default_catalog())
    for cap in caps or []:
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


# Tests ------------------------------------------------------------------


class TestDeterministicWorkflowAllowlist:
    def test_fixed_event_routes_to_deterministic_workflow(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(event_type="ticket.sla_breached")
        request = _make_request(reg, signals=signals)

        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "deterministic_workflow"
        assert REASON_FIXED_EVENT_ALLOWLIST in decision.reasons

    def test_unknown_event_not_treated_as_fixed(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        # Unknown event_type should NOT trigger deterministic_workflow.
        signals = _make_signals(event_type="ticket.unknown_event")
        request = _make_request(reg, signals=signals)

        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route != "deterministic_workflow"

    def test_kafka_topic_mapping_is_explicit(self):
        # Phase 3 doesn't subscribe to Kafka, but the mapping must be explicit.
        assert "crm.tickets.sla-breached" in KAFKA_TOPIC_TO_EVENT_TYPE
        assert (
            KAFKA_TOPIC_TO_EVENT_TYPE["crm.tickets.sla-breached"]
            == "ticket.sla_breached"
        )
        assert all(
            v in DETERMINISTIC_EVENT_TYPES for v in KAFKA_TOPIC_TO_EVENT_TYPE.values()
        )


class TestRouteDecisions:
    def test_single_domain_single_task_routes_single_agent(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "single_agent"
        assert REASON_SINGLE_DOMAIN_SINGLE_TASK in decision.reasons

    def test_two_domains_route_multi_agent(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        cap2 = _make_capability(
            agent_id="sales_agent",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"sales_analysis"}),
        )
        reg = _make_registry([cap, cap2])
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_analysis"}),
        )
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "multi_agent"
        assert REASON_CROSS_DOMAIN_OBJECTIVE in decision.reasons

    def test_multiple_task_types_route_multi_agent(self):
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
        )
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "multi_agent"
        assert REASON_MULTIPLE_TASK_TYPES in decision.reasons

    def test_conflicting_signals_route_multi_agent(self):
        """Business-level conflicting signals → multi_agent (not fail-closed)."""
        cap = _make_capability(
            agent_id="support_agent",
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
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "multi_agent"
        assert REASON_CONFLICTING_SIGNALS in decision.reasons

    def test_customer_recovery_routes_multi_agent(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"customer_recovery"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"customer_recovery"}),
            requested_task_types=frozenset({"support_analysis"}),
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "multi_agent"
        assert REASON_CUSTOMER_RECOVERY_TEMPLATE in decision.reasons


class TestFailClosed:
    def test_missing_context_fails_closed(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(missing_required_context=True)
        request = _make_request(reg, signals=signals)
        with pytest.raises(InsufficientContextError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_registry_version_mismatch_fails(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals()
        request = _make_request(reg, signals=signals, registry_version="stale-version")
        with pytest.raises(RegistryVersionMismatchError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_no_capable_agent_fails_closed(self):
        """No READ/PROPOSE agent covers the requested domain."""
        cap = _make_capability(
            agent_id="other_agent",
            domains=frozenset({"other"}),
            supported_tasks=frozenset({"other_task"}),
        )
        reg = _make_registry([cap])
        # Request a domain that no agent covers.
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(UnsupportedCapabilityError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_execute_only_agent_not_considered_capable(self):
        """EXECUTE-only agents are filtered out of the capable check."""
        from multi_agent.registry import ToolCatalog, ToolDescriptor
        from multi_agent.contracts import ToolAuthority

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
        cap = _make_capability(
            agent_id="executor",
            authority=AgentAuthority.EXECUTE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"automation_executor.execute"}),
        )
        reg = _make_registry([cap], catalog=catalog)
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(UnsupportedCapabilityError):
            RuleBasedComplexityGate().decide(request, reg)


class TestStructuralInputContradictions:
    """Correction 4: structural input contradictions → PlanningInputError."""

    def test_cross_domain_with_single_domain_fails(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requires_cross_domain=True,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_approval_without_any_task_types_fails(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset(),
            requires_approval=True,
            requires_write=False,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            RuleBasedComplexityGate().decide(request, reg)


class TestDeterminismAndMetadata:
    def test_decision_is_deterministic(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        gate = RuleBasedComplexityGate()
        d1 = gate.decide(request, reg)
        d2 = gate.decide(request, reg)
        assert d1.route == d2.route
        assert d1.domains == d2.domains
        assert d1.reasons == d2.reasons
        assert d1.confidence == d2.confidence

    def test_decision_contains_reason_codes(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert len(decision.reasons) > 0
        # Every reason is a stable code, not free-form text.
        for r in decision.reasons:
            assert isinstance(r, str)
            assert r.islower() or r == r.lower() or "_" in r

    def test_decision_contains_no_chain_of_thought(self):
        """ComplexityDecision has no free-text reasoning field."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        request = _make_request(reg)
        decision = RuleBasedComplexityGate().decide(request, reg)
        # ComplexityDecision fields: route, domains, reasons, confidence,
        # requires_human_review.  No 'reasoning', 'thought', 'prompt' fields.
        fields = set(type(decision).model_fields.keys())
        assert "reasoning" not in fields
        assert "thought" not in fields
        assert "prompt" not in fields
        assert "chain_of_thought" not in fields
