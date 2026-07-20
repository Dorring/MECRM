"""Phase 3 R5 review counterexample tests.

Covers the 3 P0 issues from the R5 review:

* P0-1: Validator must enforce write/approval requirements.  The
  rule that ``requires_write`` / ``requires_approval`` implies at
  least one PROPOSE intent was previously Planner-private; a
  hand-built :class:`PlanDraft` could bypass it.  R5 extracts the
  rule into the shared :func:`validate_write_approval_requirements`
  pure function; the Validator calls it during Canonical Plan
  reconstruction and returns the stable Issue Codes
  ``write_request_missing_propose_intent`` /
  ``approval_request_missing_propose_intent``.
* P0-2: :class:`PlanDraft` must hold a real deep snapshot of the
  caller's :class:`PlanningRequest`.  Previously Pydantic v2 reused
  the same nested model instance, so external mutation of the
  original request corrupted the plan.  ``build_execution_tasks()``
  (formerly ``agent_tasks()``) now returns fresh
  :class:`AgentTask` copies so Phase 4+ dispatch cannot invalidate
  ``plan_hash``.
* P0-3: Request Hash must be invariant under list-order
  permutations of ``requested_tasks`` / ``dependencies`` /
  ``domains`` / ``requested_task_types`` / ``required_tools``.
  R5 adds :func:`canonical_request_payload` which normalizes the
  ordering before hashing; the hash only changes when the request's
  *semantic* content changes.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

from typing import Any

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    ComplexityDecision,
    ExecutionBudget,
    ToolAuthority,
)
from multi_agent.planner import DeterministicPlanner
from multi_agent.planning import (
    CODE_APPROVAL_REQUEST_MISSING_PROPOSE,
    CODE_WRITE_REQUEST_MISSING_PROPOSE,
    PlanDraft,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    TaskIntent,
    compute_request_hash,
    validate_write_approval_requirements,
)
from multi_agent.plan_validator import PlanValidator
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
                tool_name="crm_reader.get_tickets", authority=ToolAuthority.READ
            ),
            ToolDescriptor(
                tool_name="crm_reader.get_deals", authority=ToolAuthority.READ
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


def _make_intent(
    intent_id: str = "i-1",
    domain: str = "support",
    task_type: str = "support_analysis",
    objective: str = "Analyse issue",
    preferred_authority: AgentAuthority = AgentAuthority.READ,
    dependencies: list[str] | None = None,
    required_tools: frozenset[str] | None = None,
    estimated_tool_calls: int = 1,
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
    )


# ============================================================================
# P0-1: Validator must enforce write/approval requirements
# ============================================================================


class TestWriteApprovalRequirementValidation:
    """R5 P0-1: Planner and Validator share
    :func:`validate_write_approval_requirements`.  A tampered request
    with ``requires_write=True`` but only READ intents must be
    rejected by the Validator with stable Issue Codes."""

    def test_validator_rejects_write_request_with_read_intent(self):
        """requires_write=True + only READ intents →
        ``CODE_WRITE_REQUEST_MISSING_PROPOSE``."""
        cap = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_leads"}),
        )
        reg = _make_registry([cap])
        rt = _make_requested_task(
            intent_id="rt-1",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_reader.get_leads"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
            requires_write=True,
        )
        request = _make_request(reg, signals=signals)
        # Build a minimal PlanDraft — the Validator's Canonical Plan
        # reconstruction will reject the request before any task check.
        draft = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="single_agent"),
            tasks=[],
            planner_version="ma-03.5.0",
            summary="",
            warnings=[],
        )
        report = PlanValidator().validate(request, draft, reg)
        codes = [i.code for i in report.issues]
        assert CODE_WRITE_REQUEST_MISSING_PROPOSE in codes
        assert not report.valid

    def test_validator_rejects_approval_request_with_read_intent(self):
        """requires_approval=True + only READ intents →
        ``CODE_APPROVAL_REQUEST_MISSING_PROPOSE``."""
        cap = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_leads"}),
        )
        reg = _make_registry([cap])
        rt = _make_requested_task(
            intent_id="rt-1",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_reader.get_leads"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
            requires_approval=True,
        )
        request = _make_request(reg, signals=signals)
        draft = PlanDraft(
            request=request,
            request_hash=compute_request_hash(request),
            complexity=ComplexityDecision(route="single_agent"),
            tasks=[],
            planner_version="ma-03.5.0",
            summary="",
            warnings=[],
        )
        report = PlanValidator().validate(request, draft, reg)
        codes = [i.code for i in report.issues]
        assert CODE_APPROVAL_REQUEST_MISSING_PROPOSE in codes
        assert not report.valid

    def test_write_request_accepts_propose_intent(self):
        """requires_write=True + at least one PROPOSE intent → valid."""
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
            preferred_authority=AgentAuthority.PROPOSE,
            required_tools=frozenset({"crm_writer.propose"}),
            estimated_tool_calls=1,
        )
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[rt],
            requires_write=True,
        )
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        assert plan.tasks[0].task.agent_id == "propose_agent"

    def test_planner_validator_share_write_requirement(self):
        """Direct call to :func:`validate_write_approval_requirements`
        must return the same Issue Codes the Planner and Validator
        use.  This guarantees the shared-pure-function contract."""
        cap = _make_capability(
            agent_id="read_agent",
            authority=AgentAuthority.READ,
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_leads"}),
        )
        reg = _make_registry([cap])

        # requires_write=True + READ intent → [write_request_missing_propose_intent]
        signals_w = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[
                _make_requested_task(
                    intent_id="rt-1",
                    preferred_authority=AgentAuthority.READ,
                )
            ],
            requires_write=True,
        )
        request_w = _make_request(reg, signals=signals_w)
        intents = [
            _make_intent(intent_id="rt-1", preferred_authority=AgentAuthority.READ)
        ]
        codes = validate_write_approval_requirements(request_w, intents)
        assert codes == [CODE_WRITE_REQUEST_MISSING_PROPOSE]

        # requires_approval=True + READ intent → [approval_request_missing_propose_intent]
        signals_a = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[
                _make_requested_task(
                    intent_id="rt-1",
                    preferred_authority=AgentAuthority.READ,
                )
            ],
            requires_approval=True,
        )
        request_a = _make_request(reg, signals=signals_a)
        codes = validate_write_approval_requirements(request_a, intents)
        assert codes == [CODE_APPROVAL_REQUEST_MISSING_PROPOSE]

        # requires_write=True + PROPOSE intent → []
        intents_p = [
            _make_intent(intent_id="rt-1", preferred_authority=AgentAuthority.PROPOSE)
        ]
        codes = validate_write_approval_requirements(request_w, intents_p)
        assert codes == []


# ============================================================================
# P0-2: PlanDraft must hold a real deep snapshot
# ============================================================================


class TestImmutablePlanSnapshot:
    """R5 P0-2: PlanDraft must deep-copy the caller's PlanningRequest
    at construction time.  External mutation of the original request
    (or its nested signals / requested_tasks) must NOT change the
    PlanDraft.  ``build_execution_tasks()`` must return fresh
    :class:`AgentTask` copies so Phase 4+ dispatch cannot invalidate
    ``plan_hash``."""

    def _make_plan(self) -> tuple[PlanDraft, AgentRegistry, PlanningRequest]:
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
        request = _make_request(reg, signals=signals)
        plan = DeterministicPlanner().create_plan(request, reg)
        return plan, reg, request

    def test_plan_request_is_deep_snapshot(self):
        """``plan.request is not original_request`` — PlanDraft must
        hold its own copy, not the caller's reference."""
        plan, _reg, request = self._make_plan()
        assert plan.request is not request

    def test_original_request_mutation_does_not_change_plan(self):
        """Mutating the caller's request.objective must not change
        ``plan.request.objective`` or ``plan.objective``."""
        plan, _reg, request = self._make_plan()
        original_objective = plan.objective
        request.objective = "tampered"
        assert plan.request.objective == original_objective
        assert plan.objective == original_objective

    def test_original_signals_mutation_does_not_change_plan(self):
        """Mutating the caller's signals.requires_write must not
        change ``plan.request.signals.requires_write``."""
        plan, _reg, request = self._make_plan()
        original_requires_write = plan.request.signals.requires_write
        request.signals.requires_write = True
        assert plan.request.signals.requires_write == original_requires_write

    def test_agent_tasks_returns_defensive_copies(self):
        """``build_execution_tasks()`` must return fresh AgentTask
        instances, not internal references."""
        plan, _reg, _request = self._make_plan()
        tasks = plan.build_execution_tasks()
        assert len(tasks) == len(plan.tasks)
        for returned, internal in zip(tasks, plan.tasks):
            assert returned is not internal.task

    def test_execution_task_mutation_does_not_change_plan(self):
        """Mutating a returned execution task must not invalidate
        ``plan_hash``."""
        plan, _reg, _request = self._make_plan()
        original_hash = plan.plan_hash
        tasks = plan.build_execution_tasks()
        tasks[0].status = "completed"  # type: ignore[attr-defined]
        # Plan hash must remain unchanged.
        assert plan.plan_hash == original_hash
        assert plan.compute_plan_hash() == original_hash


# ============================================================================
# P0-3: Request Hash must be invariant under list-order permutations
# ============================================================================


class TestSemanticRequestHash:
    """R5 P0-3: ``compute_request_hash`` uses
    :func:`canonical_request_payload` to normalize ordering of
    ``requested_tasks``, ``dependencies``, ``domains``,
    ``requested_task_types``, and ``required_tools`` before hashing.
    Two requests differing only in list order must produce the same
    hash; a real semantic change must produce a different hash."""

    def _make_request_with_tasks(
        self, reg: AgentRegistry, tasks: list[RequestedTask], **signal_overrides: Any
    ) -> PlanningRequest:
        # Derive domains/task_types from the task list so the
        # PlanningSignals consistency validator passes.
        task_domains = frozenset(t.domain for t in tasks)
        task_types = frozenset(t.task_type for t in tasks)
        signals = _make_signals(
            domains=task_domains,
            requested_task_types=task_types,
            requested_tasks=tasks,
            **signal_overrides,
        )
        return _make_request(reg, signals=signals)

    def test_request_hash_invariant_to_requested_task_order(self):
        """Two requests with the same requested_tasks in different
        list order must produce the same request_hash."""
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
        request_a = self._make_request_with_tasks(reg, [rt_a, rt_b])
        request_b = self._make_request_with_tasks(reg, [rt_b, rt_a])
        assert compute_request_hash(request_a) == compute_request_hash(request_b)

    def test_plan_hash_invariant_to_requested_task_order(self):
        """Two plans built from requests that differ only in
        requested_tasks list order must produce the same plan_hash."""
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
        plan_a = DeterministicPlanner().create_plan(
            self._make_request_with_tasks(reg, [rt_a, rt_b]), reg
        )
        plan_b = DeterministicPlanner().create_plan(
            self._make_request_with_tasks(reg, [rt_b, rt_a]), reg
        )
        assert plan_a.plan_hash == plan_b.plan_hash
        assert plan_a.request_hash == plan_b.request_hash

    def test_request_hash_invariant_to_dependency_order(self):
        """A task with ``dependencies=["a", "b"]`` must hash the same
        as ``dependencies=["b", "a"]`` — dependencies are a set."""
        cap = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap])
        # Three tasks: rt-a, rt-b (depends on a), rt-c (depends on a, b).
        rt_a = _make_requested_task(intent_id="rt-a", task_type="task_a")
        rt_b = _make_requested_task(
            intent_id="rt-b", task_type="task_a", dependencies=["rt-a"]
        )
        # Same logical dependency set, different list order.
        rt_c_ab = _make_requested_task(
            intent_id="rt-c",
            task_type="task_a",
            dependencies=["rt-a", "rt-b"],
        )
        rt_c_ba = _make_requested_task(
            intent_id="rt-c",
            task_type="task_a",
            dependencies=["rt-b", "rt-a"],
        )
        request_ab = self._make_request_with_tasks(reg, [rt_a, rt_b, rt_c_ab])
        request_ba = self._make_request_with_tasks(reg, [rt_a, rt_b, rt_c_ba])
        assert compute_request_hash(request_ab) == compute_request_hash(request_ba)

    def test_semantic_dag_change_changes_request_hash(self):
        """A real change to the dependency target (rt-c depends on
        rt-a vs rt-c depends on rt-b) must change the request_hash."""
        cap = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap])
        rt_a = _make_requested_task(intent_id="rt-a", task_type="task_a")
        rt_b = _make_requested_task(intent_id="rt-b", task_type="task_a")
        rt_c_dep_a = _make_requested_task(
            intent_id="rt-c", task_type="task_a", dependencies=["rt-a"]
        )
        rt_c_dep_b = _make_requested_task(
            intent_id="rt-c", task_type="task_a", dependencies=["rt-b"]
        )
        request_dep_a = self._make_request_with_tasks(reg, [rt_a, rt_b, rt_c_dep_a])
        request_dep_b = self._make_request_with_tasks(reg, [rt_a, rt_b, rt_c_dep_b])
        assert compute_request_hash(request_dep_a) != compute_request_hash(
            request_dep_b
        )

    def test_dependency_target_change_changes_plan_hash(self):
        """A real change to the dependency target must change the
        plan_hash (because it changes request_hash → plan_hash).

        Uses :func:`compute_plan_hash` directly so the test does not
        depend on the Complexity Gate routing 3 single-domain tasks
        (which would route to ``single_agent`` and reject >1
        RequestedTask).  The semantic invariant under test is purely
        about the hash pipeline."""
        cap = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        reg = _make_registry([cap])
        rt_a = _make_requested_task(intent_id="rt-a", task_type="task_a")
        rt_b = _make_requested_task(intent_id="rt-b", task_type="task_a")
        rt_c_dep_a = _make_requested_task(
            intent_id="rt-c", task_type="task_a", dependencies=["rt-a"]
        )
        rt_c_dep_b = _make_requested_task(
            intent_id="rt-c", task_type="task_a", dependencies=["rt-b"]
        )
        request_dep_a = self._make_request_with_tasks(reg, [rt_a, rt_b, rt_c_dep_a])
        request_dep_b = self._make_request_with_tasks(reg, [rt_a, rt_b, rt_c_dep_b])
        # request_hash differs (proven by the previous test) → plan_hash
        # must also differ, even with identical complexity + tasks +
        # planner_version.
        from multi_agent.planning import compute_plan_hash

        complexity = ComplexityDecision(route="multi_agent")
        plan_hash_a = compute_plan_hash(
            request_hash=compute_request_hash(request_dep_a),
            complexity=complexity,
            tasks=[],
            planner_version="ma-03.5.0",
        )
        plan_hash_b = compute_plan_hash(
            request_hash=compute_request_hash(request_dep_b),
            complexity=complexity,
            tasks=[],
            planner_version="ma-03.5.0",
        )
        assert plan_hash_a != plan_hash_b


# ============================================================================
# P1-1: TOOL_TO_AGENT_AUTHORITY must be statically populated
# ============================================================================


class TestStaticToolAuthorityMapping:
    """R5 P1-1: :data:`TOOL_TO_AGENT_AUTHORITY` must be populated at
    module load time, not lazily.  External code reading the mapping
    before any authority validation runs must see the full mapping."""

    def test_mapping_populated_at_import_time(self):
        """The mapping is non-empty immediately after import — no
        lazy init call required."""
        from multi_agent.planning import TOOL_TO_AGENT_AUTHORITY

        assert len(TOOL_TO_AGENT_AUTHORITY) == 3
        assert TOOL_TO_AGENT_AUTHORITY[ToolAuthority.READ] is AgentAuthority.READ
        assert TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] is AgentAuthority.PROPOSE
        assert TOOL_TO_AGENT_AUTHORITY[ToolAuthority.EXECUTE] is AgentAuthority.EXECUTE

    def test_no_lazy_init_function_remains(self):
        """The old ``_init_tool_authority_mapping`` function has been
        removed — there's no lazy init to forget to call."""
        from multi_agent import planning

        assert not hasattr(planning, "_init_tool_authority_mapping")
