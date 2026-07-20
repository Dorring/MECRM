"""Phase 3 R3 review counterexample tests.

Covers the 5 P0 issues + 1 P1 issue from the R3 review:

* P0-1: Expected Intent校验遗漏 Dependencies — Validator must verify
  AgentTask.dependencies and required_evidence match the canonical
  reconstruction.
* P0-2: Validator不验证确定性的Agent选择结果 — Validator must recompute
  the deterministic agent assignment and reject any mismatch.
* P0-3: Task Timeout可被篡改 — Validator must verify timeout_ms,
  max_retries, status, started_at, completed_at, input_data, user_id,
  correlation_id match canonical values.
* P0-4: Customer Recovery会忽略冲突的显式输入 — Gate must reject
  conflicting domains / requested_task_types / requested_tasks when
  objective_kind == customer_recovery.
* P0-5: planner_version可以伪造 — Validator must verify
  plan.planner_version == PLANNER_VERSION.
* P1: 全局分配使用无界笛卡尔积 — resolve_agent_assignment must bound
  the search space and fail closed when exceeded.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

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
    MAX_ASSIGNMENT_COMBINATIONS,
    PLANNER_VERSION,
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    build_expected_planned_tasks,
    resolve_agent_assignment,
    resolve_candidate_agents,
    resolve_expected_intents,
)
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    PlanningInputError,
    UnsupportedCapabilityError,
)
from multi_agent.planning_templates import (
    CUSTOMER_RECOVERY_DOMAIN,
    DEFAULT_CUSTOMER_RECOVERY_TEMPLATE,
    INTENT_SUPPORT_ANALYSIS,
)
from multi_agent.plan_validator import (
    PlanValidator,
    CODE_AGENT_ASSIGNMENT_MISMATCH,
    CODE_DEPENDENCY_MISMATCH,
    CODE_PLANNER_VERSION_MISMATCH,
    CODE_REQUIRED_EVIDENCE_MISMATCH,
    CODE_TASK_FIELD_MISMATCH,
    CODE_TASK_LIFECYCLE_VIOLATION,
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


def _set_plan_tasks(plan: PlanDraft, tasks: list[PlannedTask]) -> PlanDraft:
    """Bypass validate_assignment to swap tasks, then recompute hash."""
    object.__setattr__(plan, "tasks", tasks)
    new_hash = plan.compute_plan_hash()
    object.__setattr__(plan, "plan_hash", new_hash)
    return plan


def _set_plan_field(plan: PlanDraft, field: str, value: Any) -> PlanDraft:
    """Bypass validate_assignment to set a top-level field, then
    recompute plan_hash."""
    object.__setattr__(plan, field, value)
    new_hash = plan.compute_plan_hash()
    object.__setattr__(plan, "plan_hash", new_hash)
    return plan


def _customer_recovery_caps() -> list[AgentCapability]:
    """Five agents covering the Customer Recovery template."""
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
    """Build a 2-task multi-agent plan with a dependency: rt-b depends on
    rt-a."""
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


def _rebuild_task(
    original_pt: PlannedTask,
    *,
    task_overrides: dict[str, Any] | None = None,
    pt_overrides: dict[str, Any] | None = None,
) -> PlannedTask:
    """Clone a PlannedTask with optional field overrides on the inner
    AgentTask and/or the outer PlannedTask wrapper."""
    task_fields = original_pt.task.model_dump()
    if task_overrides:
        task_fields.update(task_overrides)
    new_task = AgentTask(**task_fields)

    pt_fields = original_pt.model_dump(exclude={"task"})
    if pt_overrides:
        pt_fields.update(pt_overrides)
    return PlannedTask(**pt_fields, task=new_task)


# ============================================================================
# P0-2: Validator must verify deterministic Agent Assignment
# ============================================================================


class TestAgentAssignmentValidation:
    """Validator must recompute the deterministic agent assignment and
    reject any plan that picked a different agent."""

    def test_more_privileged_agent_substitution_rejected(self):
        """Replace a READ agent with a PROPOSE agent (both
        registry-supported) → CODE_AGENT_ASSIGNMENT_MISMATCH."""
        # read_agent: authority=READ, cost=low
        # propose_agent: authority=PROPOSE, cost=medium
        read_agent = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            cost_class="low",
        )
        propose_agent = _make_capability(
            agent_id="propose_agent",
            authority=AgentAuthority.PROPOSE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            cost_class="medium",
        )
        reg = _make_registry([read_agent, propose_agent])
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
        # Planner should have picked read_agent (lower authority rank).
        assert plan.tasks[0].task.agent_id == "read_agent"

        # Tamper: replace with propose_agent, recompute hashes.
        original_pt = plan.tasks[0]
        tampered_task = AgentTask(
            **{**original_pt.task.model_dump(), "agent_id": "propose_agent"}
        )
        # Recompute task_id and idempotency_key for the new agent.
        from multi_agent.planning import _stable_task_id

        new_task_id = _stable_task_id(
            run_id=request.run_id,
            intent_id=original_pt.intent_id,
            task_type=original_pt.task.task_type,
            agent_id="propose_agent",
        )
        tampered_task = AgentTask(
            **{
                **original_pt.task.model_dump(),
                "agent_id": "propose_agent",
                "task_id": new_task_id,
                "idempotency_key": f"{request.run_id}:{new_task_id}",
            }
        )
        tampered_pt = PlannedTask(
            **{
                **original_pt.model_dump(exclude={"task"}),
                "task": tampered_task,
            }
        )
        _set_plan_tasks(plan, [tampered_pt])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_AGENT_ASSIGNMENT_MISMATCH in codes
        assert not report.valid

    def test_more_expensive_agent_substitution_rejected(self):
        """Replace a low-cost agent with a medium-cost agent (same
        authority) → CODE_AGENT_ASSIGNMENT_MISMATCH."""
        cheap = _make_capability(
            agent_id="cheap_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            cost_class="low",
        )
        expensive = _make_capability(
            agent_id="expensive_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            cost_class="medium",
        )
        reg = _make_registry([cheap, expensive])
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
        assert plan.tasks[0].task.agent_id == "cheap_agent"

        # Tamper: replace with expensive_agent.
        original_pt = plan.tasks[0]
        from multi_agent.planning import _stable_task_id

        new_task_id = _stable_task_id(
            run_id=request.run_id,
            intent_id=original_pt.intent_id,
            task_type=original_pt.task.task_type,
            agent_id="expensive_agent",
        )
        tampered_task = AgentTask(
            **{
                **original_pt.task.model_dump(),
                "agent_id": "expensive_agent",
                "task_id": new_task_id,
                "idempotency_key": f"{request.run_id}:{new_task_id}",
            }
        )
        tampered_pt = PlannedTask(
            **{
                **original_pt.model_dump(exclude={"task"}),
                "task": tampered_task,
            }
        )
        _set_plan_tasks(plan, [tampered_pt])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_AGENT_ASSIGNMENT_MISMATCH in codes
        assert not report.valid

    def test_agent_version_substitution_rejected(self):
        """Substituting an agent with a different version (different
        agent_id) → CODE_AGENT_ASSIGNMENT_MISMATCH.

        Note: AgentTask doesn't carry agent_version directly.  Version
        drift between planning and validation is caught by the registry
        version check at the plan level.  This test verifies that
        substituting a different agent (which has a different version)
        is rejected via the agent_id mismatch check.
        """
        cap_v1 = _make_capability(
            agent_id="agent_v1",
            version="1.0.0",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_v2 = _make_capability(
            agent_id="agent_v2",
            version="2.0.0",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap_v1, cap_v2])
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
        # Planner picks agent_v1 (lexicographically first).
        assert plan.tasks[0].task.agent_id == "agent_v1"

        # Tamper: replace with agent_v2.
        original_pt = plan.tasks[0]
        from multi_agent.planning import _stable_task_id

        new_task_id = _stable_task_id(
            run_id=request.run_id,
            intent_id=original_pt.intent_id,
            task_type=original_pt.task.task_type,
            agent_id="agent_v2",
        )
        tampered_task = AgentTask(
            **{
                **original_pt.task.model_dump(),
                "agent_id": "agent_v2",
                "task_id": new_task_id,
                "idempotency_key": f"{request.run_id}:{new_task_id}",
            }
        )
        tampered_pt = PlannedTask(
            **{
                **original_pt.model_dump(exclude={"task"}),
                "task": tampered_task,
            }
        )
        _set_plan_tasks(plan, [tampered_pt])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_AGENT_ASSIGNMENT_MISMATCH in codes
        assert not report.valid

    def test_global_assignment_substitution_rejected(self):
        """In a multi-agent plan, swap the two agents' assignments →
        CODE_AGENT_ASSIGNMENT_MISMATCH for both tasks."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        original_b = next(pt for pt in plan.tasks if pt.intent_id == "rt-b")
        # Swap: rt-a gets agent_b, rt-b gets agent_a.
        from multi_agent.planning import _stable_task_id

        def _swap(pt: PlannedTask, new_agent_id: str) -> PlannedTask:
            new_task_id = _stable_task_id(
                run_id=request.run_id,
                intent_id=pt.intent_id,
                task_type=pt.task.task_type,
                agent_id=new_agent_id,
            )
            new_task = AgentTask(
                **{
                    **pt.task.model_dump(),
                    "agent_id": new_agent_id,
                    "task_id": new_task_id,
                    "idempotency_key": f"{request.run_id}:{new_task_id}",
                }
            )
            return PlannedTask(**{**pt.model_dump(exclude={"task"}), "task": new_task})

        swapped_a = _swap(original_a, original_b.task.agent_id)
        swapped_b = _swap(original_b, original_a.task.agent_id)
        _set_plan_tasks(plan, [swapped_a, swapped_b])
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert codes.count(CODE_AGENT_ASSIGNMENT_MISMATCH) >= 2
        assert not report.valid

    def test_canonical_plan_matches_planner_output(self):
        """Positive test: a plan produced by the planner must pass
        validation without any agent_assignment_mismatch issues."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_AGENT_ASSIGNMENT_MISMATCH not in codes
        assert report.valid


# ============================================================================
# P0-1: Validator must verify Intent Dependency binding
# ============================================================================


class TestDependencyValidation:
    """Validator must verify AgentTask.dependencies match the canonical
    intent dependencies (resolved to task IDs)."""

    def test_removed_expected_dependency_rejected(self):
        """Remove a dependency from a task that should have one →
        CODE_DEPENDENCY_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        # rt-b should depend on rt-a's task_id.
        original_b = next(pt for pt in plan.tasks if pt.intent_id == "rt-b")
        assert len(original_b.task.dependencies) == 1
        # Remove the dependency.
        tampered_pt = _rebuild_task(
            original_b,
            task_overrides={"dependencies": frozenset()},
        )
        tasks = [pt if pt.intent_id != "rt-b" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DEPENDENCY_MISMATCH in codes
        assert not report.valid

    def test_added_unexpected_dependency_rejected(self):
        """Add a dependency to a task that should have none →
        CODE_DEPENDENCY_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        # rt-a should have no dependencies.
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        assert len(original_a.task.dependencies) == 0
        # Add a dependency on rt-b's task_id (wrong direction).
        original_b = next(pt for pt in plan.tasks if pt.intent_id == "rt-b")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"dependencies": frozenset({original_b.task.task_id})},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DEPENDENCY_MISMATCH in codes
        assert not report.valid

    def test_wrong_dependency_target_rejected(self):
        """Point a dependency at the wrong task_id →
        CODE_DEPENDENCY_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_b = next(pt for pt in plan.tasks if pt.intent_id == "rt-b")
        # Replace the correct dependency with a bogus task_id.
        tampered_pt = _rebuild_task(
            original_b,
            task_overrides={"dependencies": frozenset({"bogus-task-id-xxx"})},
        )
        tasks = [pt if pt.intent_id != "rt-b" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DEPENDENCY_MISMATCH in codes
        assert not report.valid

    def test_required_evidence_mismatch_rejected(self):
        """Tamper required_evidence on a task →
        CODE_REQUIRED_EVIDENCE_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        # Add a bogus required_evidence entry.
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"required_evidence": ["bogus-evidence-id"]},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUIRED_EVIDENCE_MISMATCH in codes
        assert not report.valid

    def test_customer_recovery_root_edges_cannot_be_removed(self):
        """Customer Recovery: removing any child→root dependency edge
        must be rejected."""
        plan, reg, request = _make_customer_recovery_plan()
        # Pick a child task (e.g. support_analysis) and remove its
        # dependency on customer_context.
        child = next(pt for pt in plan.tasks if pt.intent_id == INTENT_SUPPORT_ANALYSIS)
        assert len(child.task.dependencies) == 1
        tampered_pt = _rebuild_task(
            child,
            task_overrides={"dependencies": frozenset()},
        )
        tasks = [
            pt if pt.intent_id != INTENT_SUPPORT_ANALYSIS else tampered_pt
            for pt in plan.tasks
        ]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DEPENDENCY_MISMATCH in codes
        assert not report.valid


# ============================================================================
# P0-3: Canonical AgentTask fields must not be tamperable
# ============================================================================


class TestCanonicalAgentTaskFields:
    """Validator must verify timeout_ms, max_retries, status,
    started_at, completed_at, input_data, user_id, correlation_id,
    priority match canonical values."""

    def test_lowered_timeout_rejected(self):
        """Lower timeout_ms below the capability's timeout →
        CODE_TASK_FIELD_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        # Lower the timeout to 1ms (capability timeout is 30000ms).
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"timeout_ms": 1},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid

    def test_deadline_budget_cannot_be_bypassed_by_timeout(self):
        """Lowering timeout to fit under a tight deadline budget is
        rejected — the canonical timeout is the capability's timeout,
        not a lowerable estimate."""
        # Use a request with a very tight deadline that would only
        # pass if the timeout were lowered.
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
            timeout_ms=10_000,
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
            timeout_ms=10_000,
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
            dependencies=["rt-a"],
        )
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # Deadline budget = 15000ms; longest path = 20000ms → exceeds.
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(deadline_ms=15_000),
        )
        # Planner should raise BudgetExceededPlanningError because the
        # canonical timeouts (10000ms each) exceed the deadline.
        with pytest.raises(BudgetExceededPlanningError):
            DeterministicPlanner().create_plan(request, reg)

    def test_changed_max_retries_rejected(self):
        """Change max_retries from the canonical 0 →
        CODE_TASK_FIELD_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"max_retries": 5},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid

    def test_non_pending_task_rejected(self):
        """Set status to 'running' → CODE_TASK_LIFECYCLE_VIOLATION."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"status": "running"},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_LIFECYCLE_VIOLATION in codes
        assert not report.valid

    def test_started_task_rejected(self):
        """Set started_at to a non-None value →
        CODE_TASK_LIFECYCLE_VIOLATION."""
        from datetime import datetime, timezone

        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"started_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_LIFECYCLE_VIOLATION in codes
        assert not report.valid

    def test_completed_task_rejected(self):
        """Set completed_at to a non-None value →
        CODE_TASK_LIFECYCLE_VIOLATION."""
        from datetime import datetime, timezone

        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"completed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_LIFECYCLE_VIOLATION in codes
        assert not report.valid

    def test_unexpected_input_data_rejected(self):
        """Set input_data to a non-empty dict → CODE_TASK_FIELD_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"input_data": {"secret": "leaked"}},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid

    def test_user_id_tampering_rejected(self):
        """Set user_id to a non-None value → CODE_TASK_FIELD_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"user_id": "tampered-user"},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid

    def test_correlation_id_tampering_rejected(self):
        """Set correlation_id to a non-None value →
        CODE_TASK_FIELD_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"correlation_id": "tampered-corr"},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid

    def test_priority_tampering_rejected(self):
        """Change priority from 'medium' to 'high' →
        CODE_TASK_FIELD_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        original_a = next(pt for pt in plan.tasks if pt.intent_id == "rt-a")
        tampered_pt = _rebuild_task(
            original_a,
            task_overrides={"priority": "high"},
        )
        tasks = [pt if pt.intent_id != "rt-a" else tampered_pt for pt in plan.tasks]
        _set_plan_tasks(plan, tasks)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_FIELD_MISMATCH in codes
        assert not report.valid


# ============================================================================
# P0-4: Customer Recovery template input exclusivity
# ============================================================================


class TestCustomerRecoveryInputExclusivity:
    """When objective_kind == customer_recovery, the template owns the
    domain, task types, and intent list.  Caller-provided signals for
    these fields must be empty or exactly match the template's
    canonical values."""

    def test_customer_recovery_conflicting_domain_rejected(self):
        """signals.domains = {"support"} with customer_recovery →
        PlanningInputError."""
        reg = _make_registry(_customer_recovery_caps())
        signals = PlanningSignals(
            domains=frozenset({"support"}),
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_customer_recovery_requested_tasks_rejected(self):
        """signals.requested_tasks non-empty with customer_recovery →
        PlanningInputError."""
        reg = _make_registry(_customer_recovery_caps())
        rt = _make_requested_task(
            intent_id="rt-1",
            domain="customer_recovery",
            task_type="customer_context_summary",
        )
        signals = PlanningSignals(
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
            requested_tasks=[rt],
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_customer_recovery_conflicting_task_types_rejected(self):
        """signals.requested_task_types doesn't match template →
        PlanningInputError."""
        reg = _make_registry(_customer_recovery_caps())
        signals = PlanningSignals(
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
            requested_task_types=frozenset({"support_analysis"}),
        )
        request = _make_request(reg, signals=signals)
        with pytest.raises(PlanningInputError):
            RuleBasedComplexityGate().decide(request, reg)

    def test_customer_recovery_domain_is_canonical(self):
        """Customer Recovery decision.domains must be exactly
        ["customer_recovery"] and match template/plan domains."""
        plan, reg, request = _make_customer_recovery_plan()
        # Complexity decision domains.
        assert plan.complexity.domains == ["customer_recovery"]
        # Expected intent domains.
        intents = resolve_expected_intents(request, plan.complexity)
        intent_domains = {i.domain for i in intents}
        assert intent_domains == {"customer_recovery"}
        # PlannedTask domains.
        pt_domains = {pt.domain for pt in plan.tasks}
        assert pt_domains == {"customer_recovery"}

    def test_customer_recovery_accepts_matching_task_types(self):
        """signals.requested_task_types matching the template set is
        accepted (not rejected)."""
        reg = _make_registry(_customer_recovery_caps())
        template_types = {
            intent.task_type
            for intent in DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()
        }
        signals = PlanningSignals(
            objective_kind=CUSTOMER_RECOVERY_OBJECTIVE_KIND,
            requested_task_types=frozenset(template_types),
        )
        request = _make_request(reg, signals=signals)
        decision = RuleBasedComplexityGate().decide(request, reg)
        assert decision.route == "multi_agent"
        assert decision.domains == ["customer_recovery"]


# ============================================================================
# P0-5: Planner Version validation
# ============================================================================


class TestPlannerVersionValidation:
    """Validator must verify plan.planner_version == PLANNER_VERSION."""

    def test_unknown_planner_version_rejected(self):
        """Set planner_version to a random string →
        CODE_PLANNER_VERSION_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        _set_plan_field(plan, "planner_version", "evil-version")
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_PLANNER_VERSION_MISMATCH in codes
        assert not report.valid

    def test_stale_planner_version_rejected(self):
        """Set planner_version to an old version string →
        CODE_PLANNER_VERSION_MISMATCH."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        _set_plan_field(plan, "planner_version", "ma-03.2.0")
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_PLANNER_VERSION_MISMATCH in codes
        assert not report.valid

    def test_current_planner_version_accepted(self):
        """The planner's own output must pass the version check."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        assert plan.planner_version == PLANNER_VERSION
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_PLANNER_VERSION_MISMATCH not in codes
        assert report.valid


# ============================================================================
# P1: Assignment search space must be bounded
# ============================================================================


class TestAssignmentSearchBounded:
    """resolve_agent_assignment must bound the cartesian-product search
    and fail closed when the limit is exceeded."""

    def test_assignment_search_is_bounded(self):
        """MAX_ASSIGNMENT_COMBINATIONS is defined and reasonable."""
        assert MAX_ASSIGNMENT_COMBINATIONS > 0
        assert MAX_ASSIGNMENT_COMBINATIONS <= 10_000_000

    def test_assignment_limit_fails_closed(self):
        """When the search space exceeds MAX_ASSIGNMENT_COMBINATIONS,
        resolve_agent_assignment raises UnsupportedCapabilityError."""
        # Two intents, each with two candidate agents → 2 * 2 = 4
        # combinations. Patching MAX to 3 makes 4 > 3, triggering
        # the fail-closed path without a huge registry.
        cap_a1 = _make_capability(
            agent_id="agent_a1",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_a2 = _make_capability(
            agent_id="agent_a2",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_b1 = _make_capability(
            agent_id="agent_b1",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
        )
        cap_b2 = _make_capability(
            agent_id="agent_b2",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"task_b"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
        )
        reg = _make_registry([cap_a1, cap_a2, cap_b1, cap_b2])
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
        request = _make_request(reg, signals=signals)
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)

        # Patch MAX_ASSIGNMENT_COMBINATIONS to 3 so the search space
        # (2 * 2 = 4 combinations) exceeds it.
        with patch("multi_agent.planning.MAX_ASSIGNMENT_COMBINATIONS", 3):
            with pytest.raises(UnsupportedCapabilityError):
                resolve_agent_assignment(request, decision, intents, reg)

    def test_budget_checked_before_assignment_search(self):
        """If len(intents) > budget.max_tasks, the budget error is
        raised before the assignment search begins."""
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
        signals = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        # Budget with max_tasks=1 — but we have 2 intents.
        request = _make_request(
            reg,
            signals=signals,
            budget=ExecutionBudget(max_tasks=1),
        )
        decision = ComplexityDecision(route="multi_agent")
        intents = resolve_expected_intents(request, decision)
        assert len(intents) == 2
        # resolve_agent_assignment must raise BudgetExceededPlanningError
        # (pre-check) before attempting any candidate search.
        with pytest.raises(BudgetExceededPlanningError):
            resolve_agent_assignment(request, decision, intents, reg)

    def test_no_diverse_assignment_fails_before_plan_creation(self):
        """When no feasible ≥2-agent assignment exists, the planner
        raises UnsupportedCapabilityError BEFORE creating a plan (no
        greedy fallback)."""
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
        # Planner must raise UnsupportedCapabilityError, not
        # PlanValidationError (no greedy fallback + Validator rejection).
        with pytest.raises(UnsupportedCapabilityError):
            DeterministicPlanner().create_plan(request, reg)

    def test_resolve_candidate_agents_is_tool_aware(self):
        """resolve_candidate_agents must filter out agents that lack
        required tools, even if they support the task_type and domain."""
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
        intent = resolve_expected_intents(
            _make_request(
                reg,
                signals=_make_signals(
                    domains=frozenset({"support"}),
                    requested_task_types=frozenset({"support_analysis"}),
                    requested_tasks=[
                        _make_requested_task(
                            intent_id="rt-1",
                            domain="support",
                            task_type="support_analysis",
                            required_tools=frozenset({"crm_reader.get_tickets"}),
                            estimated_tool_calls=1,
                        )
                    ],
                ),
            ),
            ComplexityDecision(route="single_agent"),
        )[0]
        candidates = resolve_candidate_agents(intent, reg)
        agent_ids = [c.agent_id for c in candidates]
        assert "with_tool" in agent_ids
        assert "without_tool" not in agent_ids


# ============================================================================
# Shared pure functions — Planner and Validator produce identical output
# ============================================================================


class TestSharedPureFunctions:
    """The shared pure functions (resolve_expected_intents,
    resolve_agent_assignment, build_expected_planned_tasks) must
    produce identical output when called by the Planner and the
    Validator."""

    def test_planner_and_validator_share_assignment(self):
        """The agent assignment computed by the Planner (via
        create_plan) must match the assignment recomputed by the
        Validator (via resolve_agent_assignment)."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        # Recompute the assignment using the shared function.
        intents = resolve_expected_intents(request, plan.complexity)
        assignment = resolve_agent_assignment(request, plan.complexity, intents, reg)
        # The plan's agent_ids must match the shared assignment.
        for pt in plan.tasks:
            expected_cap = assignment[pt.intent_id]
            assert pt.task.agent_id == expected_cap.agent_id

    def test_build_expected_planned_tasks_is_canonical(self):
        """build_expected_planned_tasks produces the exact same tasks
        as the Planner."""
        plan, reg, request = _make_two_task_multi_agent_plan()
        intents = resolve_expected_intents(request, plan.complexity)
        assignment = resolve_agent_assignment(request, plan.complexity, intents, reg)
        expected_tasks = build_expected_planned_tasks(request, intents, assignment)
        # Compare task_id, agent_id, timeout_ms, etc.
        actual_by_intent = {pt.intent_id: pt for pt in plan.tasks}
        expected_by_intent = {pt.intent_id: pt for pt in expected_tasks}
        assert set(actual_by_intent.keys()) == set(expected_by_intent.keys())
        for intent_id in actual_by_intent:
            actual_pt = actual_by_intent[intent_id]
            expected_pt = expected_by_intent[intent_id]
            assert actual_pt.task.task_id == expected_pt.task.task_id
            assert actual_pt.task.agent_id == expected_pt.task.agent_id
            assert actual_pt.task.timeout_ms == expected_pt.task.timeout_ms
            assert actual_pt.task.max_retries == expected_pt.task.max_retries
            assert actual_pt.task.status == expected_pt.task.status
            assert actual_pt.task.priority == expected_pt.task.priority
            assert actual_pt.task.input_data == expected_pt.task.input_data
