"""Phase 3 R7 review counterexample tests.

Covers the 3 P0 issues from the R7 review:

* P0-1: ``ComplexityDecision`` accepted non-canonical values (duplicate
  domains/reasons, blank elements, mismatched confidence) because the
  Validator compared ``set(domains)`` / ``set(reasons)`` and skipped
  ``confidence``.  R7 introduces :func:`canonical_complexity_payload`
  as the single shared definition of Complexity equality, used by both
  ``compute_plan_hash`` and ``PlanValidator._check_complexity_decision``.
* P0-2: ``CustomerRecoveryTemplate`` was not truly frozen — it only
  inherited ``validate_assignment=True``, allowing runtime mutation of
  the global ``DEFAULT_CUSTOMER_RECOVERY_TEMPLATE`` singleton.  R7 adds
  ``frozen=True`` and embeds ``template_version`` in every intent's
  ``planning_metadata``.
* P0-3: ``_check_request_snapshot`` used raw Pydantic ``plan.request !=
  request`` comparison, which rejected semantically-identical requests
  that differed only in ``requested_tasks`` list order.  R7 uses
  :func:`canonical_request_payload` comparison, aligning the snapshot
  check with the hash check.

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
    ComplexityDecision,
    ExecutionBudget,
    ToolAuthority,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    canonical_complexity_payload,
    canonical_request_payload,
    compute_request_hash,
)
from multi_agent.plan_validator import (
    PlanValidator,
    CODE_COMPLEXITY_DECISION_MISMATCH,
    CODE_REQUEST_SNAPSHOT_MISMATCH,
)
from multi_agent.planning_templates import (
    CUSTOMER_RECOVERY_DOMAIN,
    CustomerRecoveryTemplate,
    DEFAULT_CUSTOMER_RECOVERY_TEMPLATE,
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


def _customer_recovery_caps() -> list[AgentCapability]:
    domain = CUSTOMER_RECOVERY_DOMAIN
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


def _make_customer_recovery_plan(
    **overrides: Any,
) -> tuple[PlanDraft, AgentRegistry, PlanningRequest]:
    reg = _make_registry(_customer_recovery_caps())
    signals = PlanningSignals(
        objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    )
    request = _make_request(reg, signals=signals, **overrides)
    plan = DeterministicPlanner().create_plan(request, reg)
    return plan, reg, request


def _make_two_task_multi_agent_plan(
    **overrides: Any,
) -> tuple[PlanDraft, AgentRegistry, PlanningRequest]:
    """Build a 2-task multi-agent plan: rt-b depends on rt-a."""
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
    )
    rt_b = _make_requested_task(
        intent_id="rt-b",
        domain="sales",
        task_type="task_b",
        required_tools=frozenset({"crm_reader.get_deals"}),
        dependencies=["rt-a"],
    )
    signals = _make_signals(
        domains=frozenset({"support", "sales"}),
        requested_task_types=frozenset({"task_a", "task_b"}),
        requested_tasks=[rt_a, rt_b],
    )
    request = _make_request(reg, signals=signals, **overrides)
    plan = DeterministicPlanner().create_plan(request, reg)
    return plan, reg, request


# ============================================================================
# P0-1: Canonical Complexity Payload
# ============================================================================


class TestCanonicalComplexityPayload:
    """R7 P0-1: ``canonical_complexity_payload`` is the single shared
    definition of Complexity equality.  It rejects duplicates, blanks,
    and includes ``confidence`` in comparison."""

    def test_complexity_confidence_mismatch_rejected(self):
        """A plan whose ``complexity.confidence`` differs from the
        Gate's output must be rejected with
        ``complexity_decision_mismatch``."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_confidence = plan.complexity.confidence
        tampered_confidence = 0.5 if original_confidence != 0.5 else 0.3
        tampered_complexity = ComplexityDecision(
            route=plan.complexity.route,
            domains=list(plan.complexity.domains),
            reasons=list(plan.complexity.reasons),
            confidence=tampered_confidence,
            requires_human_review=plan.complexity.requires_human_review,
        )
        object.__setattr__(plan, "complexity", tampered_complexity)
        new_hash = plan.compute_plan_hash()
        object.__setattr__(plan, "plan_hash", new_hash)

        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_duplicate_complexity_domains_rejected(self):
        """A ``ComplexityDecision`` with duplicate domains must be
        rejected.  ``canonical_complexity_payload`` raises
        ``ValueError``, which the Validator surfaces as
        ``complexity_decision_mismatch``."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_domains = list(plan.complexity.domains)
        if not original_domains:
            pytest.skip("Gate produced no domains")
        tampered_complexity = ComplexityDecision(
            route=plan.complexity.route,
            domains=original_domains + [original_domains[0]],
            reasons=list(plan.complexity.reasons),
            confidence=plan.complexity.confidence,
            requires_human_review=plan.complexity.requires_human_review,
        )
        object.__setattr__(plan, "complexity", tampered_complexity)

        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_duplicate_complexity_reasons_rejected(self):
        """A ``ComplexityDecision`` with duplicate reasons must be
        rejected."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_reasons = list(plan.complexity.reasons)
        if not original_reasons:
            pytest.skip("Gate produced no reasons")
        tampered_complexity = ComplexityDecision(
            route=plan.complexity.route,
            domains=list(plan.complexity.domains),
            reasons=original_reasons + [original_reasons[0]],
            confidence=plan.complexity.confidence,
            requires_human_review=plan.complexity.requires_human_review,
        )
        object.__setattr__(plan, "complexity", tampered_complexity)

        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_blank_complexity_reason_rejected(self):
        """A ``ComplexityDecision`` with a blank reason element must
        be rejected."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        tampered_complexity = ComplexityDecision(
            route=plan.complexity.route,
            domains=list(plan.complexity.domains),
            reasons=list(plan.complexity.reasons) + [""],
            confidence=plan.complexity.confidence,
            requires_human_review=plan.complexity.requires_human_review,
        )
        object.__setattr__(plan, "complexity", tampered_complexity)

        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_COMPLEXITY_DECISION_MISMATCH in codes
        assert not report.valid

    def test_complexity_order_is_invariant(self):
        """Two ``ComplexityDecision`` instances with the same elements
        but different list order must produce the same canonical
        payload."""
        d1 = ComplexityDecision(
            route="single_agent",
            domains=["sales", "support"],
            reasons=["reason_b", "reason_a"],
            confidence=0.8,
            requires_human_review=False,
        )
        d2 = ComplexityDecision(
            route="single_agent",
            domains=["support", "sales"],
            reasons=["reason_a", "reason_b"],
            confidence=0.8,
            requires_human_review=False,
        )
        assert canonical_complexity_payload(d1) == canonical_complexity_payload(d2)


# ============================================================================
# P0-2: Immutable Customer Recovery Template
# ============================================================================


class TestImmutableCustomerRecoveryTemplate:
    """R7 P0-2: ``CustomerRecoveryTemplate`` is now ``frozen=True`` and
    embeds ``template_version`` in every intent's metadata."""

    def test_default_template_is_frozen(self):
        """Mutating any field on ``DEFAULT_CUSTOMER_RECOVERY_TEMPLATE``
        must raise ``ValidationError``."""
        with pytest.raises(ValidationError):
            DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.name = "tampered"

    def test_template_flags_cannot_be_mutated(self):
        """Mutating ``support_analysis_required`` must raise
        ``ValidationError``."""
        with pytest.raises(ValidationError):
            DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.support_analysis_required = False
        with pytest.raises(ValidationError):
            DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.version = "tampered"

    def test_template_version_is_in_plan_metadata(self):
        """Every PlannedTask generated by the Customer Recovery template
        must carry ``template_version`` in ``planning_metadata``."""
        plan, _reg, _request = _make_customer_recovery_plan()
        assert len(plan.tasks) == 5
        for pt in plan.tasks:
            assert "template" in pt.planning_metadata
            assert "template_version" in pt.planning_metadata
            assert "phase" in pt.planning_metadata
            assert pt.planning_metadata["template"] == "customer_recovery"
            assert pt.planning_metadata["template_version"] == "ma-03.1.0"

    def test_template_version_changes_plan_hash(self):
        """Two plans built with different template versions must
        produce different ``plan_hash`` values."""
        plan1, reg1, request1 = _make_customer_recovery_plan()
        custom_template = CustomerRecoveryTemplate(version="ma-03.99.0")
        reg2 = _make_registry(_customer_recovery_caps())
        signals = PlanningSignals(
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request2 = _make_request(reg2, signals=signals, run_id="run-002")
        intents = custom_template.build_intents()
        from multi_agent.planning import (
            build_expected_planned_tasks,
            resolve_agent_assignment,
        )

        # Replace the template-derived intents in resolve_expected_intents
        # by directly building the plan with the custom template's intents.
        decision = RuleBasedComplexityGate().decide(request2, reg2)
        assignment = resolve_agent_assignment(request2, decision, intents, reg2)
        planned_tasks = build_expected_planned_tasks(request2, intents, assignment)
        request_hash = compute_request_hash(request2)
        plan2 = PlanDraft(
            request=request2,
            request_hash=request_hash,
            complexity=decision,
            tasks=planned_tasks,
            planner_version=PLANNER_VERSION,
            summary="",
            warnings=[],
        )
        assert plan1.plan_hash != plan2.plan_hash

    def test_global_template_cannot_change_runtime_behavior(self):
        """Attempting to mutate the global template must fail, and the
        plan must still use the original required flags."""
        original_required = DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.support_analysis_required
        with pytest.raises(ValidationError):
            DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.support_analysis_required = (
                not original_required
            )
        assert (
            DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.support_analysis_required
            == original_required
        )
        plan, _reg, _request = _make_customer_recovery_plan()
        support_task = next(
            pt for pt in plan.tasks if pt.intent_id == "support_analysis"
        )
        assert support_task.required == original_required


# ============================================================================
# P0-3: Canonical Request Snapshot Comparison
# ============================================================================


class TestCanonicalRequestSnapshot:
    """R7 P0-3: ``_check_request_snapshot`` uses
    ``canonical_request_payload`` comparison instead of raw Pydantic
    ``plan.request != request``.  Semantically-identical requests with
    permuted ``requested_tasks`` or ``dependencies`` are accepted;
    real semantic changes are still rejected."""

    def test_validator_accepts_permuted_requested_tasks(self):
        """A plan built with ``[rt_a, rt_b]`` must pass validation
        when the caller passes ``[rt_b, rt_a]``."""
        plan, reg, _request = _make_two_task_multi_agent_plan()
        # Rebuild the request with permuted requested_tasks order.
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            dependencies=["rt-a"],
        )
        permuted_signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_b, rt_a],
        )
        permuted_request = _make_request(reg, signals=permuted_signals)
        report = PlanValidator().validate(permuted_request, plan, reg)
        snapshot_issues = [
            i for i in report.issues if i.code == CODE_REQUEST_SNAPSHOT_MISMATCH
        ]
        assert snapshot_issues == []
        assert report.valid

    def test_validator_accepts_permuted_dependencies(self):
        """A task with dependencies ``["rt-a", "rt-b"]`` must pass
        validation when the caller passes ``["rt-b", "rt-a"]``."""
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
            domains=frozenset({"billing"}),
            supported_tasks=frozenset({"task_c"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap_a, cap_b, cap_c])
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
        )
        rt_c = _make_requested_task(
            intent_id="rt-c",
            domain="billing",
            task_type="task_c",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            dependencies=["rt-a", "rt-b"],
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales", "billing"}),
            requested_task_types=frozenset({"task_a", "task_b", "task_c"}),
            requested_tasks=[rt_a, rt_b, rt_c],
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)

        # Permute rt_c's dependencies.
        rt_c_permuted = _make_requested_task(
            intent_id="rt-c",
            domain="billing",
            task_type="task_c",
            required_tools=frozenset({"crm_reader.get_tickets"}),
            dependencies=["rt-b", "rt-a"],
        )
        permuted_signals = _make_signals(
            domains=frozenset({"support", "sales", "billing"}),
            requested_task_types=frozenset({"task_a", "task_b", "task_c"}),
            requested_tasks=[rt_a, rt_b, rt_c_permuted],
        )
        permuted_request = _make_request(reg, signals=permuted_signals)
        report = PlanValidator().validate(permuted_request, plan, reg)
        snapshot_issues = [
            i for i in report.issues if i.code == CODE_REQUEST_SNAPSHOT_MISMATCH
        ]
        assert snapshot_issues == []
        assert report.valid

    def test_validator_uses_canonical_request_payload(self):
        """``canonical_request_payload`` produces the same payload for
        semantically-identical requests with different list order."""
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
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
        )
        signals_a = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        signals_b = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_b, rt_a],
        )
        request_a = _make_request(reg, signals=signals_a)
        request_b = _make_request(reg, signals=signals_b)
        assert canonical_request_payload(request_a) == canonical_request_payload(
            request_b
        )
        assert compute_request_hash(request_a) == compute_request_hash(request_b)

    def test_validator_rejects_semantic_task_change(self):
        """Changing a requested task's domain must be rejected with
        ``request_snapshot_mismatch``."""
        plan, reg, _request = _make_two_task_multi_agent_plan()
        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="billing",
            task_type="task_a",
            required_tools=frozenset({"crm_reader.get_tickets"}),
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="task_b",
            required_tools=frozenset({"crm_reader.get_deals"}),
            dependencies=["rt-a"],
        )
        changed_signals = _make_signals(
            domains=frozenset({"billing", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        changed_request = _make_request(reg, signals=changed_signals)
        report = PlanValidator().validate(changed_request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUEST_SNAPSHOT_MISMATCH in codes
        assert not report.valid

    def test_validator_rejects_budget_change(self):
        """Changing the request budget must be rejected with
        ``request_snapshot_mismatch``."""
        plan, reg, _request = _make_two_task_multi_agent_plan()
        changed_request = _make_request(
            reg,
            signals=_make_signals(
                domains=frozenset({"support", "sales"}),
                requested_task_types=frozenset({"task_a", "task_b"}),
                requested_tasks=[
                    _make_requested_task(
                        intent_id="rt-a",
                        domain="support",
                        task_type="task_a",
                        required_tools=frozenset({"crm_reader.get_tickets"}),
                    ),
                    _make_requested_task(
                        intent_id="rt-b",
                        domain="sales",
                        task_type="task_b",
                        required_tools=frozenset({"crm_reader.get_deals"}),
                        dependencies=["rt-a"],
                    ),
                ],
            ),
            budget=ExecutionBudget(max_tasks=99),
        )
        report = PlanValidator().validate(changed_request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUEST_SNAPSHOT_MISMATCH in codes
        assert not report.valid


# ============================================================================
# R7.1 Hotfix — public export integrity
# ============================================================================


class TestPublicExportIntegrity:
    """R7.1 hotfix: ``canonical_complexity_payload`` was listed in
    ``__all__`` but never actually imported, so
    ``from multi_agent import canonical_complexity_payload`` failed.
    These tests guard against ``__all__`` / real-import drift forever
    after."""

    def test_canonical_complexity_payload_public_export(self) -> None:
        """The public export must be the exact same object as the
        source function in ``multi_agent.planning``."""
        from multi_agent import canonical_complexity_payload as public_export
        from multi_agent.planning import canonical_complexity_payload as source

        assert public_export is source

    def test_every_public_export_resolves(self) -> None:
        """Every name in ``multi_agent.__all__`` must be a real
        attribute on the package, so ``from multi_agent import *``
        never raises ``ImportError`` for any advertised name."""
        import multi_agent

        for name in multi_agent.__all__:
            assert hasattr(multi_agent, name), f"missing public export: {name}"
