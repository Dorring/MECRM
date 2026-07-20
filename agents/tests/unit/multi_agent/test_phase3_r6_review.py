"""Phase 3 R6 review counterexample tests.

Covers the 4 P0 issues from the R6 review:

* P0-1: ``plan_hash`` and ``RegistrySnapshot.version`` must be stable
  across ``PYTHONHASHSEED`` values.  Previously the Canonicalizer and
  Registry Snapshot used ``model_dump(mode="json")``, which converted
  ``frozenset`` fields to plain lists with process-random iteration
  order.  Tests spawn real subprocesses with different seeds.
* P0-2: Agent Assignment must be invariant under list-order
  permutations of ``requested_tasks``.  Previously the assignment
  tie-breaker sorted ``agent_ids`` and ``versions`` independently,
  losing the intent→agent mapping and producing different assignments
  for semantically-identical requests.
* P0-3: ``PlanDraft`` must deep-copy ``complexity`` and ``tasks`` at
  the contract boundary, not just ``request``.  Previously
  ``plan.complexity is original_complexity`` held, and mutating the
  caller's ``ComplexityDecision`` or ``AgentTask`` corrupted the plan.
* P0-4: ``TOOL_TO_AGENT_AUTHORITY`` must be immutable.  Previously it
  was a plain ``dict`` exported via ``multi_agent.__init__``, so a
  single ``TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] =
  AgentAuthority.READ`` would silently downgrade the authority
  boundary for the entire process.

All tests run under AI_MODE=deterministic; no network, no LLM.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
    PLANNER_VERSION,
    PlanDraft,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
    TaskIntent,
    TOOL_TO_AGENT_AUTHORITY,
    build_expected_planned_tasks,
    compute_plan_hash,
    compute_request_hash,
    resolve_agent_assignment,
    resolve_expected_intents,
    validate_intent_tool_authority,
)
from multi_agent.planning_errors import PlanningInputError
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


# ============================================================================
# P0-1: Cross-process Hash Stability (subprocess tests)
# ============================================================================


_CHILD_SCRIPT = """
import sys
sys.path.insert(0, "src")

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    ComplexityDecision,
    ExecutionBudget,
    ToolAuthority,
    ToolDescriptor,
)
from multi_agent.planning import (
    PLANNER_VERSION,
    compute_plan_hash,
    compute_request_hash,
    build_expected_planned_tasks,
    resolve_agent_assignment,
    resolve_expected_intents,
    PlanningRequest,
    PlanningSignals,
    RequestedTask,
)
from multi_agent.registry import AgentRegistry, ToolCatalog


catalog = ToolCatalog([
    ToolDescriptor(tool_name="crm_reader.get_tickets", authority=ToolAuthority.READ),
    ToolDescriptor(tool_name="crm_reader.get_deals", authority=ToolAuthority.READ),
])


class FakeHandler:
    async def run(self, task, ctx):
        return None


reg = AgentRegistry(tool_catalog=catalog)
reg.register(AgentCapability(
    agent_id="agent_a", version="1.0.0", description="d",
    domains=frozenset({"support"}), supported_tasks=frozenset({"support_analysis"}),
    allowed_tools=frozenset({"crm_reader.get_tickets"}), authority=AgentAuthority.READ,
    input_contract="in", output_contract="out", timeout_ms=30000,
    max_retries=2, estimated_cost_class="low", enabled=True,
), FakeHandler())
reg.register(AgentCapability(
    agent_id="agent_b", version="1.0.0", description="d",
    domains=frozenset({"sales"}), supported_tasks=frozenset({"sales_risk"}),
    allowed_tools=frozenset({"crm_reader.get_deals"}), authority=AgentAuthority.READ,
    input_contract="in", output_contract="out", timeout_ms=30000,
    max_retries=2, estimated_cost_class="low", enabled=True,
), FakeHandler())

snap = reg.snapshot()
print("REGISTRY_VERSION=" + snap.version)

rt_a = RequestedTask(intent_id="rt-a", domain="support", task_type="support_analysis",
    objective="a", preferred_authority=AgentAuthority.READ,
    required_tools=frozenset({"crm_reader.get_tickets"}), estimated_tool_calls=1)
rt_b = RequestedTask(intent_id="rt-b", domain="sales", task_type="sales_risk",
    objective="b", preferred_authority=AgentAuthority.READ,
    required_tools=frozenset({"crm_reader.get_deals"}), estimated_tool_calls=1)
signals = PlanningSignals(
    domains=frozenset({"support", "sales"}),
    requested_task_types=frozenset({"support_analysis", "sales_risk"}),
    requested_tasks=[rt_a, rt_b],
)
request = PlanningRequest(run_id="run-001", tenant_id="t-001", actor_type="user",
    actor_id="user-001", objective="Multi", signals=signals,
    budget=ExecutionBudget(), registry_version=snap.version)

request_hash = compute_request_hash(request)
print("REQUEST_HASH=" + request_hash)

decision = ComplexityDecision(route="multi_agent",
    domains=frozenset({"support", "sales"}), reasons=["cross_domain"],
    requires_human_review=False)
intents = resolve_expected_intents(request, decision)
assignment = resolve_agent_assignment(request, decision, intents, reg)
planned_tasks = build_expected_planned_tasks(request, intents, assignment)
plan_hash = compute_plan_hash(request_hash=request_hash, complexity=decision,
    tasks=planned_tasks, planner_version=PLANNER_VERSION)
print("PLAN_HASH=" + plan_hash)
"""


def _run_child(seed: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    # R6 P0-1 — use the agents/ directory (parent of tests/) as cwd so
    # the child script's ``sys.path.insert(0, "src")`` resolves to the
    # same ``agents/src`` tree the parent pytest process uses.
    agents_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd=agents_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"child with seed={seed} failed: {result.returncode}\n{result.stderr}"
        )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class TestCrossProcessHashStability:
    """R6 P0-1 — ``plan_hash`` and ``registry_version`` must be stable
    across ``PYTHONHASHSEED`` values.  Tests spawn real subprocesses."""

    def test_plan_hash_stable_across_python_hash_seeds(self):
        """Spawn 4 subprocesses with different PYTHONHASHSEED values
        and verify they all produce the same ``plan_hash``."""
        seeds = ["0", "1", "42", "12345"]
        outputs = [_run_child(s) for s in seeds]
        plan_hashes = [o["PLAN_HASH"] for o in outputs]
        assert len(set(plan_hashes)) == 1, (
            f"plan_hash drifted across PYTHONHASHSEED values: {plan_hashes}"
        )

    def test_registry_version_stable_across_python_hash_seeds(self):
        """Spawn 4 subprocesses and verify ``registry_version`` is
        identical.  Previously ``AgentRegistry.snapshot()`` used
        ``model_dump(mode="json")`` which converted ``frozenset``
        fields to plain lists with hash-randomized iteration order."""
        seeds = ["0", "1", "42", "12345"]
        outputs = [_run_child(s) for s in seeds]
        versions = [o["REGISTRY_VERSION"] for o in outputs]
        assert len(set(versions)) == 1, (
            f"registry_version drifted across PYTHONHASHSEED values: {versions}"
        )

    def test_required_tools_order_does_not_change_plan_hash(self):
        """Two plans with the same ``required_tools`` but different
        iteration order must produce the same ``plan_hash``.
        ``required_tools`` is a ``frozenset``; with ``mode="python"``
        the Canonicalizer sorts it, so iteration order doesn't leak."""
        cap = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"task_a"}),
            allowed_tools=frozenset({"crm_reader.get_tickets", "crm_reader.get_deals"}),
        )
        reg = _make_registry([cap])

        # Same intent, different frozenset construction order (both
        # produce the same set, but the test verifies the hash is
        # stable regardless of how the set was built).
        intent_1 = TaskIntent(
            intent_id="i-1",
            domain="support",
            task_type="task_a",
            objective="o",
            required_tools=frozenset(
                {"crm_reader.get_tickets", "crm_reader.get_deals"}
            ),
            estimated_tool_calls=2,
        )
        intent_2 = TaskIntent(
            intent_id="i-1",
            domain="support",
            task_type="task_a",
            objective="o",
            required_tools=frozenset(
                {"crm_reader.get_deals", "crm_reader.get_tickets"}
            ),
            estimated_tool_calls=2,
        )
        # frozenset equality is order-independent, so these are the same object
        assert intent_1.required_tools == intent_2.required_tools

        request = _make_request(reg)
        decision = ComplexityDecision(route="single_agent", domains=["support"])
        assignment_1 = resolve_agent_assignment(request, decision, [intent_1], reg)
        assignment_2 = resolve_agent_assignment(request, decision, [intent_2], reg)
        tasks_1 = build_expected_planned_tasks(request, [intent_1], assignment_1)
        tasks_2 = build_expected_planned_tasks(request, [intent_2], assignment_2)

        rh = compute_request_hash(request)
        h1 = compute_plan_hash(
            request_hash=rh,
            complexity=decision,
            tasks=tasks_1,
            planner_version=PLANNER_VERSION,
        )
        h2 = compute_plan_hash(
            request_hash=rh,
            complexity=decision,
            tasks=tasks_2,
            planner_version=PLANNER_VERSION,
        )
        assert h1 == h2

    def test_dependency_frozenset_order_does_not_change_plan_hash(self):
        """Two plans with the same dependency ``frozenset`` but
        different construction order must produce the same
        ``plan_hash``.  ``AgentTask.dependencies`` is a ``frozenset``;
        with ``mode="python"`` the Canonicalizer sorts it."""
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

        # Two intents with dependencies on the same set of predecessors,
        # built with different iteration order.  frozenset equality is
        # order-independent, so the resulting AgentTask.dependencies
        # will be the same frozenset, but we verify the hash is stable.
        intent_c_dep_1 = TaskIntent(
            intent_id="rt-c",
            domain="billing",
            task_type="task_c",
            objective="c",
            dependencies=["rt-a", "rt-b"],
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        intent_c_dep_2 = TaskIntent(
            intent_id="rt-c",
            domain="billing",
            task_type="task_c",
            objective="c",
            dependencies=["rt-b", "rt-a"],
            required_tools=frozenset({"crm_reader.get_tickets"}),
            estimated_tool_calls=1,
        )
        # The dependencies list is deduped but order is preserved;
        # however, when converted to AgentTask.dependencies (frozenset),
        # the resulting set is the same.  Verify the frozenset is equal.
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

        request = _make_request(
            reg,
            signals=_make_signals(
                domains=frozenset({"support", "sales", "billing"}),
                requested_task_types=frozenset({"task_a", "task_b", "task_c"}),
                requested_tasks=[rt_a, rt_b, rt_c],
            ),
        )
        decision = ComplexityDecision(
            route="multi_agent",
            domains=frozenset({"support", "sales", "billing"}),
        )

        intents_1 = [
            TaskIntent(
                intent_id="rt-a",
                domain="support",
                task_type="task_a",
                objective="a",
                required_tools=frozenset({"crm_reader.get_tickets"}),
                estimated_tool_calls=1,
            ),
            TaskIntent(
                intent_id="rt-b",
                domain="sales",
                task_type="task_b",
                objective="b",
                required_tools=frozenset({"crm_reader.get_deals"}),
                estimated_tool_calls=1,
            ),
            intent_c_dep_1,
        ]
        intents_2 = [
            TaskIntent(
                intent_id="rt-a",
                domain="support",
                task_type="task_a",
                objective="a",
                required_tools=frozenset({"crm_reader.get_tickets"}),
                estimated_tool_calls=1,
            ),
            TaskIntent(
                intent_id="rt-b",
                domain="sales",
                task_type="task_b",
                objective="b",
                required_tools=frozenset({"crm_reader.get_deals"}),
                estimated_tool_calls=1,
            ),
            intent_c_dep_2,
        ]

        # Intent dependencies are lists, so their order matters for the
        # Intent itself.  But when build_expected_planned_tasks converts
        # them to AgentTask.dependencies (frozenset), the resulting
        # frozenset is the same.  The plan_hash should be identical.
        assignment_1 = resolve_agent_assignment(request, decision, intents_1, reg)
        assignment_2 = resolve_agent_assignment(request, decision, intents_2, reg)
        tasks_1 = build_expected_planned_tasks(request, intents_1, assignment_1)
        tasks_2 = build_expected_planned_tasks(request, intents_2, assignment_2)

        # The frozenset dependencies should be equal
        deps_1 = tasks_1[2].task.dependencies
        deps_2 = tasks_2[2].task.dependencies
        assert deps_1 == deps_2

        rh = compute_request_hash(request)
        h1 = compute_plan_hash(
            request_hash=rh,
            complexity=decision,
            tasks=tasks_1,
            planner_version=PLANNER_VERSION,
        )
        h2 = compute_plan_hash(
            request_hash=rh,
            complexity=decision,
            tasks=tasks_2,
            planner_version=PLANNER_VERSION,
        )
        assert h1 == h2


# ============================================================================
# P0-2: Canonical Intent Ordering — Assignment Invariant
# ============================================================================


class TestCanonicalIntentOrdering:
    """R6 P0-2 — Agent assignment and plan_hash must be invariant under
    list-order permutations of ``requested_tasks``."""

    def test_agent_assignment_invariant_to_requested_task_order(self):
        """Two requests with the same ``requested_tasks`` but in
        different list order must produce the same agent assignment."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"sales_risk"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
        )
        reg = _make_registry([cap_a, cap_b])

        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
            required_tools=frozenset({"crm_reader.get_tickets"}),
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="sales_risk",
            required_tools=frozenset({"crm_reader.get_deals"}),
        )

        # Request A: [rt-a, rt-b]
        signals_a = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_risk"}),
            requested_tasks=[rt_a, rt_b],
        )
        request_a = _make_request(reg, signals=signals_a)

        # Request B: [rt-b, rt-a]  (same tasks, permuted order)
        signals_b = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_risk"}),
            requested_tasks=[rt_b, rt_a],
        )
        request_b = _make_request(reg, signals=signals_b)

        decision = ComplexityDecision(
            route="multi_agent",
            domains=frozenset({"support", "sales"}),
        )

        intents_a = resolve_expected_intents(request_a, decision)
        intents_b = resolve_expected_intents(request_b, decision)
        assignment_a = resolve_agent_assignment(request_a, decision, intents_a, reg)
        assignment_b = resolve_agent_assignment(request_b, decision, intents_b, reg)

        # Same intent_id → same agent_id, regardless of input order.
        assert assignment_a.keys() == assignment_b.keys()
        for iid in assignment_a:
            assert assignment_a[iid].agent_id == assignment_b[iid].agent_id, (
                f"intent {iid!r} got different agents: "
                f"{assignment_a[iid].agent_id} vs {assignment_b[iid].agent_id}"
            )

    def test_plan_hash_invariant_with_generalist_candidates(self):
        """When two agents both support both tasks (generalists), the
        assignment must still be deterministic and the plan_hash must
        be invariant under input permutation."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support", "sales"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
            allowed_tools=frozenset({"crm_reader.get_tickets", "crm_reader.get_deals"}),
            timeout_ms=30_000,
            cost_class="low",
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"support", "sales"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
            allowed_tools=frozenset({"crm_reader.get_tickets", "crm_reader.get_deals"}),
            timeout_ms=30_000,
            cost_class="low",
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

        # Request A: [rt-a, rt-b]
        signals_a = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request_a = _make_request(reg, signals=signals_a)

        # Request B: [rt-b, rt-a]
        signals_b = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_b, rt_a],
        )
        request_b = _make_request(reg, signals=signals_b)

        decision = ComplexityDecision(
            route="multi_agent",
            domains=frozenset({"support", "sales"}),
        )

        intents_a = resolve_expected_intents(request_a, decision)
        intents_b = resolve_expected_intents(request_b, decision)
        assignment_a = resolve_agent_assignment(request_a, decision, intents_a, reg)
        assignment_b = resolve_agent_assignment(request_b, decision, intents_b, reg)

        # Same assignment
        for iid in assignment_a:
            assert assignment_a[iid].agent_id == assignment_b[iid].agent_id

        tasks_a = build_expected_planned_tasks(request_a, intents_a, assignment_a)
        tasks_b = build_expected_planned_tasks(request_b, intents_b, assignment_b)

        rh_a = compute_request_hash(request_a)
        rh_b = compute_request_hash(request_b)
        # Request hash is already order-invariant (R5)
        assert rh_a == rh_b

        h_a = compute_plan_hash(
            request_hash=rh_a,
            complexity=decision,
            tasks=tasks_a,
            planner_version=PLANNER_VERSION,
        )
        h_b = compute_plan_hash(
            request_hash=rh_b,
            complexity=decision,
            tasks=tasks_b,
            planner_version=PLANNER_VERSION,
        )
        assert h_a == h_b, (
            f"plan_hash drifted for permuted requested_tasks: {h_a[:16]} vs {h_b[:16]}"
        )

    def test_assignment_key_preserves_intent_agent_mapping(self):
        """The assignment tie-breaker must preserve the intent→agent
        mapping.  Two semantically-identical requests must not only
        pick the same set of agents, but assign them to the same
        intents."""
        # Two generalist agents that both support both tasks.
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support", "sales"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
            allowed_tools=frozenset({"crm_reader.get_tickets", "crm_reader.get_deals"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"support", "sales"}),
            supported_tasks=frozenset({"task_a", "task_b"}),
            allowed_tools=frozenset({"crm_reader.get_tickets", "crm_reader.get_deals"}),
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

        # Order 1: [rt-a, rt-b]
        signals_1 = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_a, rt_b],
        )
        request_1 = _make_request(reg, signals=signals_1)

        # Order 2: [rt-b, rt-a]
        signals_2 = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"task_a", "task_b"}),
            requested_tasks=[rt_b, rt_a],
        )
        request_2 = _make_request(reg, signals=signals_2)

        decision = ComplexityDecision(
            route="multi_agent",
            domains=frozenset({"support", "sales"}),
        )

        intents_1 = resolve_expected_intents(request_1, decision)
        intents_2 = resolve_expected_intents(request_2, decision)
        assignment_1 = resolve_agent_assignment(request_1, decision, intents_1, reg)
        assignment_2 = resolve_agent_assignment(request_2, decision, intents_2, reg)

        # The critical assertion: rt-a always gets the same agent,
        # regardless of whether it was first or second in the list.
        assert assignment_1["rt-a"].agent_id == assignment_2["rt-a"].agent_id
        assert assignment_1["rt-b"].agent_id == assignment_2["rt-b"].agent_id

    def test_planner_output_identical_for_permuted_intents(self):
        """End-to-end: ``DeterministicPlanner.create_plan()`` must
        produce identical ``plan_hash`` for two requests that differ
        only in ``requested_tasks`` list order."""
        cap_a = _make_capability(
            agent_id="agent_a",
            domains=frozenset({"support"}),
            supported_tasks=frozenset({"support_analysis"}),
            allowed_tools=frozenset({"crm_reader.get_tickets"}),
        )
        cap_b = _make_capability(
            agent_id="agent_b",
            domains=frozenset({"sales"}),
            supported_tasks=frozenset({"sales_risk"}),
            allowed_tools=frozenset({"crm_reader.get_deals"}),
        )
        reg = _make_registry([cap_a, cap_b])

        rt_a = _make_requested_task(
            intent_id="rt-a",
            domain="support",
            task_type="support_analysis",
            required_tools=frozenset({"crm_reader.get_tickets"}),
        )
        rt_b = _make_requested_task(
            intent_id="rt-b",
            domain="sales",
            task_type="sales_risk",
            required_tools=frozenset({"crm_reader.get_deals"}),
        )

        signals_1 = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_risk"}),
            requested_tasks=[rt_a, rt_b],
        )
        signals_2 = _make_signals(
            domains=frozenset({"support", "sales"}),
            requested_task_types=frozenset({"support_analysis", "sales_risk"}),
            requested_tasks=[rt_b, rt_a],
        )

        request_1 = _make_request(reg, signals=signals_1)
        request_2 = _make_request(reg, signals=signals_2)

        planner = DeterministicPlanner()
        plan_1 = planner.create_plan(request_1, reg)
        plan_2 = planner.create_plan(request_2, reg)

        assert plan_1.plan_hash == plan_2.plan_hash, (
            f"plan_hash drifted for permuted requested_tasks: "
            f"{plan_1.plan_hash[:16]} vs {plan_2.plan_hash[:16]}"
        )
        assert plan_1.request_hash == plan_2.request_hash


# ============================================================================
# P0-3: PlanDraft Full Deep Snapshot
# ============================================================================


class TestPlanDraftFullSnapshot:
    """R6 P0-3 — ``PlanDraft`` must deep-copy ``complexity`` and
    ``tasks`` at the contract boundary, not just ``request``."""

    def test_plan_complexity_is_deep_snapshot(self):
        """``plan.complexity is original_complexity`` must be False."""
        cap = _make_capability(agent_id="agent_a")
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[_make_requested_task()],
        )
        request = _make_request(reg, signals=signals)
        complexity = ComplexityDecision(
            route="single_agent", domains=["support"], reasons=["r1"]
        )
        rh = compute_request_hash(request)
        plan = PlanDraft(
            request=request,
            request_hash=rh,
            complexity=complexity,
            tasks=[],
            planner_version=PLANNER_VERSION,
            summary="",
            warnings=[],
        )
        assert plan.complexity is not complexity

    def test_original_complexity_mutation_does_not_change_plan(self):
        """Mutating the caller's ``ComplexityDecision.domains`` must
        not change ``plan.complexity.domains``."""
        cap = _make_capability(agent_id="agent_a")
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[_make_requested_task()],
        )
        request = _make_request(reg, signals=signals)
        complexity = ComplexityDecision(
            route="single_agent", domains=["support"], reasons=["r1"]
        )
        rh = compute_request_hash(request)
        plan = PlanDraft(
            request=request,
            request_hash=rh,
            complexity=complexity,
            tasks=[],
            planner_version=PLANNER_VERSION,
            summary="",
            warnings=[],
        )
        original_domains = list(plan.complexity.domains)
        # Mutate the caller's complexity
        complexity.domains.append("sales")
        assert plan.complexity.domains == original_domains

    def test_plan_tasks_are_deep_snapshots(self):
        """``plan.tasks[0] is original_planned_task`` must be False."""
        cap = _make_capability(agent_id="agent_a")
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[_make_requested_task()],
        )
        request = _make_request(reg, signals=signals)
        decision = ComplexityDecision(route="single_agent", domains=["support"])
        intents = resolve_expected_intents(request, decision)
        assignment = resolve_agent_assignment(request, decision, intents, reg)
        planned_tasks = build_expected_planned_tasks(request, intents, assignment)

        rh = compute_request_hash(request)
        plan = PlanDraft(
            request=request,
            request_hash=rh,
            complexity=decision,
            tasks=planned_tasks,
            planner_version=PLANNER_VERSION,
            summary="",
            warnings=[],
        )
        assert plan.tasks[0] is not planned_tasks[0]
        assert plan.tasks[0].task is not planned_tasks[0].task

    def test_original_task_mutation_does_not_change_plan(self):
        """Mutating the caller's ``AgentTask.status`` must not change
        ``plan.tasks[0].task.status``."""
        cap = _make_capability(agent_id="agent_a")
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[_make_requested_task()],
        )
        request = _make_request(reg, signals=signals)
        decision = ComplexityDecision(route="single_agent", domains=["support"])
        intents = resolve_expected_intents(request, decision)
        assignment = resolve_agent_assignment(request, decision, intents, reg)
        planned_tasks = build_expected_planned_tasks(request, intents, assignment)

        rh = compute_request_hash(request)
        plan = PlanDraft(
            request=request,
            request_hash=rh,
            complexity=decision,
            tasks=planned_tasks,
            planner_version=PLANNER_VERSION,
            summary="",
            warnings=[],
        )
        # PlannedTask is frozen, but AgentTask is not — we can mutate it
        # via model validation.  Simulate by building a new task with
        # mutated status and checking the plan is unaffected.
        original_status = plan.tasks[0].task.status
        # Mutate the original caller's task (planned_tasks[0].task)
        # Since AgentTask has validate_assignment, we need to use
        # object.__setattr__ to bypass validation for this test.
        object.__setattr__(planned_tasks[0].task, "status", "running")
        assert plan.tasks[0].task.status == original_status

    def test_original_metadata_mutation_does_not_change_plan(self):
        """Mutating the caller's ``planning_metadata`` dict must not
        change ``plan.tasks[0].planning_metadata``."""
        cap = _make_capability(agent_id="agent_a")
        reg = _make_registry([cap])
        signals = _make_signals(
            domains=frozenset({"support"}),
            requested_task_types=frozenset({"support_analysis"}),
            requested_tasks=[_make_requested_task()],
        )
        request = _make_request(reg, signals=signals)
        decision = ComplexityDecision(route="single_agent", domains=["support"])
        intents = resolve_expected_intents(request, decision)
        assignment = resolve_agent_assignment(request, decision, intents, reg)
        planned_tasks = build_expected_planned_tasks(request, intents, assignment)

        rh = compute_request_hash(request)
        plan = PlanDraft(
            request=request,
            request_hash=rh,
            complexity=decision,
            tasks=planned_tasks,
            planner_version=PLANNER_VERSION,
            summary="",
            warnings=[],
        )
        original_meta = dict(plan.tasks[0].planning_metadata)
        # Mutate the caller's planning_metadata dict
        planned_tasks[0].planning_metadata["tampered"] = True
        assert plan.tasks[0].planning_metadata == original_meta


# ============================================================================
# P0-4: Immutable Tool Authority Mapping
# ============================================================================


class TestImmutableToolAuthorityMapping:
    """R6 P0-4 — ``TOOL_TO_AGENT_AUTHORITY`` must be immutable so a
    single ``dict`` assignment cannot downgrade the authority boundary
    for the entire process."""

    def test_tool_authority_mapping_is_immutable(self):
        """``TOOL_TO_AGENT_AUTHORITY`` must raise ``TypeError`` on
        mutation attempts (``__setitem__``, ``__delitem__``)."""
        # __setitem__
        with pytest.raises(TypeError):
            TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] = AgentAuthority.READ
        # __delitem__
        with pytest.raises(TypeError):
            del TOOL_TO_AGENT_AUTHORITY[ToolAuthority.READ]

    def test_mapping_mutation_cannot_bypass_authority_check(self):
        """Even if a caller tries to mutate the mapping, the authority
        check must still reject READ intent + PROPOSE tool.

        ``validate_intent_tool_authority`` resolves tools via the
        registry's ``tool_catalog`` — no agent registration is needed.
        The default catalog includes ``crm_writer.propose`` as a
        PROPOSE-level tool."""
        # Registry with the default tool catalog but no agents —
        # validate_intent_tool_authority only needs tool_catalog.resolve.
        reg = _make_registry([])

        intent = TaskIntent(
            intent_id="i-1",
            domain="support",
            task_type="support_analysis",
            objective="o",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_writer.propose"}),
            estimated_tool_calls=1,
        )
        # Attempt to mutate the mapping — must raise TypeError because
        # TOOL_TO_AGENT_AUTHORITY is now a MappingProxyType.
        with pytest.raises(TypeError):
            TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] = AgentAuthority.READ
        # Authority check must still raise PlanningInputError because
        # the mapping is immutable and cannot be tampered with.
        with pytest.raises(PlanningInputError):
            validate_intent_tool_authority(intent, reg)

    def test_read_intent_always_rejects_propose_tool(self):
        """After importing the module, a READ intent with a PROPOSE
        tool must always be rejected.  This is a regression test for
        the scenario where a previous test mutated the mapping and
        left it in a permissive state.

        ``validate_intent_tool_authority`` resolves tools via the
        registry's ``tool_catalog`` — no agent registration is needed."""
        # Registry with the default tool catalog but no agents.
        reg = _make_registry([])

        intent = TaskIntent(
            intent_id="i-1",
            domain="support",
            task_type="support_analysis",
            objective="o",
            preferred_authority=AgentAuthority.READ,
            required_tools=frozenset({"crm_writer.propose"}),
            estimated_tool_calls=1,
        )
        with pytest.raises(PlanningInputError):
            validate_intent_tool_authority(intent, reg)

        # Verify the mapping is still correct after the check
        assert TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] is AgentAuthority.PROPOSE
