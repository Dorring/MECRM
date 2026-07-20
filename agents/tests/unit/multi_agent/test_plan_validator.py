"""Plan Validator tests — Phase 3.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any


from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentTask,
    ExecutionBudget,
    ToolAuthority,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    PlanningSignals,
)
from multi_agent.plan_validator import (
    PlanValidator,
    CODE_AGENT_CALL_BUDGET_EXCEEDED,
    CODE_CYCLE,
    CODE_DEADLINE_EXCEEDED,
    CODE_DETERMINISTIC_HAS_TASKS,
    CODE_DISABLED_AGENT,
    CODE_DUPLICATE_TASK_ID,
    CODE_EXECUTE_AGENT,
    CODE_ITERATION_BUDGET_EXCEEDED,
    CODE_MISSING_DEPENDENCY,
    CODE_MULTI_AGENT_TOO_FEW_AGENTS,
    CODE_MULTI_AGENT_TOO_FEW_TASKS,
    CODE_PLAN_HASH_MISMATCH,
    CODE_REGISTRY_VERSION_MISMATCH,
    CODE_REQUIRED_DEPENDS_ON_OPTIONAL,
    CODE_RUN_ID_MISMATCH,
    CODE_SELF_DEPENDENCY,
    CODE_SINGLE_AGENT_NOT_ONE,
    CODE_TASK_BUDGET_EXCEEDED,
    CODE_TENANT_MISMATCH,
    CODE_TOOL_CALL_BUDGET_EXCEEDED,
    CODE_UNKNOWN_TOOL,
    CODE_UNSUPPORTED_TASK,
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
        domains=domains or frozenset({"test"}),
        supported_tasks=supported_tasks or frozenset({"test_task"}),
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


def _make_valid_plan(
    registry: AgentRegistry,
    request: PlanningRequest | None = None,
) -> PlanDraft:
    """Build a valid plan via the real planner."""
    if request is None:
        request = _make_request(registry)
    return DeterministicPlanner().create_plan(request, registry)


def _set_plan_tasks(plan: PlanDraft, tasks: list[PlannedTask]) -> PlanDraft:
    """Bypass validate_assignment to swap tasks, then recompute hash."""
    object.__setattr__(plan, "tasks", tasks)
    new_hash = plan.compute_plan_hash()
    object.__setattr__(plan, "plan_hash", new_hash)
    return plan


# Tests ------------------------------------------------------------------


class TestDagValidation:
    def test_duplicate_task_id_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Duplicate the single task.
        dup_tasks = list(plan.tasks) + list(plan.tasks)
        _set_plan_tasks(plan, dup_tasks)
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DUPLICATE_TASK_ID in codes
        assert not report.valid

    def test_missing_dependency_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Add a dependency to a non-existent task.
        task = plan.tasks[0].task
        new_deps = frozenset(task.dependencies | {"nonexistent-task-id"})
        object.__setattr__(task, "dependencies", new_deps)
        _set_plan_tasks(plan, list(plan.tasks))
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_MISSING_DEPENDENCY in codes
        assert not report.valid

    def test_self_dependency_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Make the task depend on itself.
        task = plan.tasks[0].task
        object.__setattr__(task, "dependencies", frozenset({task.task_id}))
        _set_plan_tasks(plan, list(plan.tasks))
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_SELF_DEPENDENCY in codes
        assert not report.valid

    def test_cycle_rejected(self):
        """Two tasks depending on each other form a cycle."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([cap])
        # Build two tasks with cross-dependencies (manual construction
        # because the planner would never create a cycle).
        task_a = AgentTask(
            task_id="task-a-id",
            agent_id="multi_agent",
            task_type="task_a",
            objective="Task A",
            tenant_id="t-001",
            dependencies=frozenset({"task-b-id"}),
        )
        task_b = AgentTask(
            task_id="task-b-id",
            agent_id="multi_agent",
            task_type="task_b",
            objective="Task B",
            tenant_id="t-001",
            dependencies=frozenset({"task-a-id"}),
        )
        from multi_agent.planning import compute_request_hash

        request = _make_request(
            reg,
            signals=_make_signals(
                domains=frozenset({"support"}),
                requested_task_types=frozenset({"task_a", "task_b"}),
            ),
        )
        # Build plan with cycle.
        planned = [
            PlannedTask(
                intent_id="a",
                domain="support",
                preferred_authority=AgentAuthority.READ,
                task=task_a,
            ),
            PlannedTask(
                intent_id="b",
                domain="support",
                preferred_authority=AgentAuthority.READ,
                task=task_b,
            ),
        ]
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=__import__("multi_agent").ComplexityDecision(
                route="multi_agent"
            ),
            tasks=planned,
            planner_version="test",
        )
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_CYCLE in codes
        assert not report.valid

    def test_topological_order_is_stable(self):
        """Same plan → same topological order."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        request = _make_request(reg)
        v = PlanValidator()
        r1 = v.validate(request, plan, reg)
        r2 = v.validate(request, plan, reg)
        assert r1.topological_order == r2.topological_order


class TestRouteConstraints:
    def test_single_agent_requires_exactly_one_task(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Remove all tasks → 0 tasks for single_agent route.
        _set_plan_tasks(plan, [])
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_SINGLE_AGENT_NOT_ONE in codes

    def test_multi_agent_requires_two_tasks(self):
        """multi_agent route with 1 task → error."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        # Build a plan with route=multi_agent but only 1 task.
        plan = _make_valid_plan(reg)
        from multi_agent.contracts import ComplexityDecision

        object.__setattr__(plan, "complexity", ComplexityDecision(route="multi_agent"))
        _set_plan_tasks(plan, list(plan.tasks))
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_MULTI_AGENT_TOO_FEW_TASKS in codes

    def test_multi_agent_requires_two_agents(self):
        """multi_agent route with 2 tasks but 1 agent → error."""
        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([cap])
        # Build a 2-task plan on same agent.
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

        task_a = AgentTask(
            task_id="task-a",
            agent_id="multi_agent",
            task_type="task_a",
            objective="A",
            tenant_id="t-001",
        )
        task_b = AgentTask(
            task_id="task-b",
            agent_id="multi_agent",
            task_type="task_b",
            objective="B",
            tenant_id="t-001",
        )
        request = _make_request(
            reg,
            signals=_make_signals(
                domains=frozenset({"support"}),
                requested_task_types=frozenset({"task_a", "task_b"}),
            ),
        )
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=[
                PlannedTask(
                    intent_id="a",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    task=task_a,
                ),
                PlannedTask(
                    intent_id="b",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    task=task_b,
                ),
            ],
            planner_version="test",
        )
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_MULTI_AGENT_TOO_FEW_AGENTS in codes

    def test_deterministic_route_rejects_tasks(self):
        """deterministic_workflow route must have 0 tasks."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        from multi_agent.contracts import ComplexityDecision

        object.__setattr__(
            plan, "complexity", ComplexityDecision(route="deterministic_workflow")
        )
        _set_plan_tasks(plan, list(plan.tasks))
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DETERMINISTIC_HAS_TASKS in codes


class TestRegistryValidation:
    def test_unsupported_task_rejected(self):
        """Agent doesn't support the task_type."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Mutate task_type to something unsupported.
        task = plan.tasks[0].task
        object.__setattr__(task, "task_type", "unsupported_task")
        _set_plan_tasks(plan, list(plan.tasks))
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_UNSUPPORTED_TASK in codes

    def test_disabled_agent_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Disable the agent after plan creation.
        reg.replace(
            _make_capability(
                agent_id="support_agent",
                domains=frozenset({"support"}),
                supported_tasks=frozenset({"support_analysis"}),
                enabled=False,
            ),
            _FakeHandler(),
        )
        # Use the original request (registry_version is now stale, but
        # we only care about the disabled-agent error here).  Note:
        # plan.registry_version is a read-only property delegating to
        # plan.request.registry_version, so we cannot mutate it.
        request = _make_request(reg, registry_version=plan.registry_version)
        _set_plan_tasks(plan, list(plan.tasks))
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DISABLED_AGENT in codes

    def test_execute_agent_rejected(self):
        """Plan containing an EXECUTE agent → error."""

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
        exec_cap = _make_capability(
            agent_id="exec_agent",
            authority=AgentAuthority.EXECUTE,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"automation_executor.execute"}),
        )
        reg = _make_registry([exec_cap], catalog=catalog)
        # Manually build a plan with EXECUTE agent.
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

        task = AgentTask(
            task_id="task-exec",
            agent_id="exec_agent",
            task_type="support_analysis",
            objective="Exec task",
            tenant_id="t-001",
        )
        request = _make_request(reg)
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="single_agent"),
            tasks=[
                PlannedTask(
                    intent_id="a",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    task=task,
                )
            ],
            planner_version="test",
        )
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_EXECUTE_AGENT in codes

    def test_unknown_tool_rejected(self):
        """Required tool not in catalog → error."""
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Add an unknown required tool.
        pt = plan.tasks[0]
        object.__setattr__(pt, "required_tools", frozenset({"unknown.tool"}))
        _set_plan_tasks(plan, list(plan.tasks))
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_UNKNOWN_TOOL in codes


class TestBudgetValidation:
    def _make_plan_with_n_tasks(self, reg: AgentRegistry, n: int) -> PlanDraft:
        """Build a multi-agent plan with n tasks for budget testing."""
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

        caps = []
        for i in range(n):
            caps.append(
                _make_capability(
                    agent_id=f"agent_{i}",
                    domains=frozenset({"support"}),
                    supported_tasks=frozenset({f"task_{i}"}),
                )
            )
        reg2 = _make_registry(caps)
        tasks = []
        for i in range(n):
            tasks.append(
                PlannedTask(
                    intent_id=f"intent_{i}",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    estimated_tool_calls=1,
                    task=AgentTask(
                        task_id=f"task-{i}",
                        agent_id=f"agent_{i}",
                        task_type=f"task_{i}",
                        objective=f"Task {i}",
                        tenant_id="t-001",
                        timeout_ms=10_000,
                    ),
                )
            )
        request = _make_request(reg2)
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent")
            if n >= 2
            else ComplexityDecision(route="single_agent"),
            tasks=tasks,
            planner_version="test",
        )
        return plan, request, reg2

    def test_task_budget_exceeded(self):
        reg = _make_registry([])
        plan, request, reg2 = self._make_plan_with_n_tasks(reg, 5)
        # Set max_tasks=2.
        object.__setattr__(request, "budget", ExecutionBudget(max_tasks=2))
        report = PlanValidator().validate(request, plan, reg2)
        codes = [i.code for i in report.issues]
        assert CODE_TASK_BUDGET_EXCEEDED in codes

    def test_agent_call_budget_exceeded(self):
        reg = _make_registry([])
        plan, request, reg2 = self._make_plan_with_n_tasks(reg, 5)
        object.__setattr__(request, "budget", ExecutionBudget(max_agent_calls=2))
        report = PlanValidator().validate(request, plan, reg2)
        codes = [i.code for i in report.issues]
        assert CODE_AGENT_CALL_BUDGET_EXCEEDED in codes

    def test_tool_call_budget_exceeded(self):
        reg = _make_registry([])
        plan, request, reg2 = self._make_plan_with_n_tasks(reg, 3)
        # Each task has estimated_tool_calls=1, so total=3.
        object.__setattr__(request, "budget", ExecutionBudget(max_tool_calls=2))
        report = PlanValidator().validate(request, plan, reg2)
        codes = [i.code for i in report.issues]
        assert CODE_TOOL_CALL_BUDGET_EXCEEDED in codes

    def test_iteration_budget_exceeded(self):
        """Build a chain of 5 tasks → longest path = 5 > max_iterations=2."""
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

        caps = []
        for i in range(5):
            caps.append(
                _make_capability(
                    agent_id=f"agent_{i}",
                    domains=frozenset({"support"}),
                    supported_tasks=frozenset({f"task_{i}"}),
                )
            )
        reg = _make_registry(caps)
        tasks = []
        for i in range(5):
            deps = frozenset({f"task-{i - 1}"}) if i > 0 else frozenset()
            tasks.append(
                PlannedTask(
                    intent_id=f"intent_{i}",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    task=AgentTask(
                        task_id=f"task-{i}",
                        agent_id=f"agent_{i}",
                        task_type=f"task_{i}",
                        objective=f"Task {i}",
                        tenant_id="t-001",
                        dependencies=deps,
                    ),
                )
            )
        request = _make_request(reg)
        object.__setattr__(request, "budget", ExecutionBudget(max_iterations=2))
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=tasks,
            planner_version="test",
        )
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_ITERATION_BUDGET_EXCEEDED in codes

    def test_deadline_exceeded(self):
        """Total timeout along longest path > deadline_ms."""
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

        cap = _make_capability(
            agent_id="slow_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            timeout_ms=60_000,
        )
        reg = _make_registry([cap])
        task = AgentTask(
            task_id="task-slow",
            agent_id="slow_agent",
            task_type="task_a",
            objective="Slow",
            tenant_id="t-001",
            timeout_ms=60_000,
        )
        request = _make_request(reg)
        object.__setattr__(request, "budget", ExecutionBudget(deadline_ms=30_000))
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="single_agent"),
            tasks=[
                PlannedTask(
                    intent_id="a",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    task=task,
                )
            ],
            planner_version="test",
        )
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_DEADLINE_EXCEEDED in codes


class TestRequiredVsOptional:
    def test_required_task_cannot_depend_on_missing_optional_task(self):
        """A required task depending on an optional task → error."""
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

        cap = _make_capability(
            agent_id="multi_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
        )
        reg = _make_registry([cap])
        task_optional = AgentTask(
            task_id="task-optional",
            agent_id="multi_agent",
            task_type="task_a",
            objective="Optional",
            tenant_id="t-001",
        )
        task_required = AgentTask(
            task_id="task-required",
            agent_id="multi_agent",
            task_type="task_b",
            objective="Required",
            tenant_id="t-001",
            dependencies=frozenset({"task-optional"}),
        )
        request = _make_request(reg)
        plan = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=[
                PlannedTask(
                    intent_id="opt",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    required=False,
                    task=task_optional,
                ),
                PlannedTask(
                    intent_id="req",
                    domain="support",
                    preferred_authority=AgentAuthority.READ,
                    required=True,
                    task=task_required,
                ),
            ],
            planner_version="test",
        )
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REQUIRED_DEPENDS_ON_OPTIONAL in codes


class TestIdentityValidation:
    def test_tenant_mismatch_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Change request tenant_id.
        request = _make_request(reg, tenant_id="other-tenant")
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_TENANT_MISMATCH in codes

    def test_run_id_mismatch_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        request = _make_request(reg, run_id="other-run")
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_RUN_ID_MISMATCH in codes

    def test_registry_version_mismatch_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Stale request version.
        request = _make_request(reg, registry_version="stale")
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_REGISTRY_VERSION_MISMATCH in codes

    def test_plan_hash_mismatch_rejected(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        # Tamper with hash.
        object.__setattr__(plan, "plan_hash", "deadbeef" * 8)
        request = _make_request(reg)
        report = PlanValidator().validate(request, plan, reg)
        codes = [i.code for i in report.issues]
        assert CODE_PLAN_HASH_MISMATCH in codes


class TestOrderIndependence:
    def test_validation_is_order_independent(self):
        """Same tasks in different order → same valid result and topo order."""
        from multi_agent.planning import compute_request_hash
        from multi_agent.contracts import ComplexityDecision

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

        task_a = AgentTask(
            task_id="task-a",
            agent_id="agent_a",
            task_type="task_a",
            objective="A",
            tenant_id="t-001",
        )
        task_b = AgentTask(
            task_id="task-b",
            agent_id="agent_b",
            task_type="task_b",
            objective="B",
            tenant_id="t-001",
            dependencies=frozenset({"task-a"}),
        )
        pt_a = PlannedTask(
            intent_id="a",
            domain="support",
            preferred_authority=AgentAuthority.READ,
            task=task_a,
        )
        pt_b = PlannedTask(
            intent_id="b",
            domain="support",
            preferred_authority=AgentAuthority.READ,
            task=task_b,
        )

        request = _make_request(reg)
        plan1 = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=[pt_a, pt_b],
            planner_version="test",
        )
        plan2 = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="multi_agent"),
            tasks=[pt_b, pt_a],  # Reversed order
            planner_version="test",
        )
        v = PlanValidator()
        r1 = v.validate(request, plan1, reg)
        r2 = v.validate(request, plan2, reg)
        assert r1.valid == r2.valid
        assert r1.topological_order == r2.topological_order

    def test_validation_does_not_mutate_plan(self):
        cap = _make_capability(
            agent_id="support_agent",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
        )
        reg = _make_registry([cap])
        plan = _make_valid_plan(reg)
        request = _make_request(reg)
        dump_before = plan.model_dump(mode="json")
        _ = PlanValidator().validate(request, plan, reg)
        dump_after = plan.model_dump(mode="json")
        assert dump_before == dump_after
