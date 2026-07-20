"""Phase 3 Deterministic Planner.

Produces a :class:`PlanDraft` from a :class:`PlanningRequest` and an
:class:`AgentRegistry`.  The planner is **side-effect free** — it never
calls an agent handler, never invokes a tool, never writes to the
registry, and never opens a network connection.

Flow (per Phase 3 spec §7):

1. Verify Registry Snapshot version.
2. Run Complexity Gate.
3. ``deterministic_workflow`` → return empty plan.
4. Build TaskIntents (from template or from ``requested_tasks``).
5. Validate intents (unique IDs, dependencies exist, no cycle).
6. Select minimum-privilege agent per intent.
7. Generate stable AgentTask IDs.
8. Resolve intent_id dependencies → task_id dependencies.
9. Assemble PlanDraft (hashes auto-computed).
10. Run PlanValidator.
11. If invalid → raise the appropriate specific error type.

Agent selection (per Phase 3 review R1):

* EXECUTE agents are *filtered out* of the candidate set, not failed
  on sight.  If filtering leaves at least one READ/PROPOSE candidate,
  the minimum-privilege one is chosen.  If only EXECUTE candidates
  remain, the planner fails closed.
* ``preferred_authority`` comes from the :class:`RequestedTask` (or
  template), never hardcoded.
* ``requires_write=True`` or ``requires_approval=True`` require at
  least one PROPOSE-level intent, else :class:`PlanningInputError`.
"""

from __future__ import annotations

from typing import Any, Protocol

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentTask,
)
from multi_agent.registry import AgentRegistry
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    TaskIntent,
    compute_request_hash,
    task_intent_from_requested_task,
)
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    PlanCycleError,
    PlanIntegrityError,
    PlanValidationError,
    PlanningInputError,
    RegistryVersionMismatchError,
    UnsupportedCapabilityError,
)
from multi_agent.planning_templates import (
    DEFAULT_CUSTOMER_RECOVERY_TEMPLATE,
    CustomerRecoveryTemplate,
)
from multi_agent.complexity_gate import (
    CUSTOMER_RECOVERY_OBJECTIVE_KIND,
    RuleBasedComplexityGate,
)
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Authority ordering — READ < PROPOSE < EXECUTE
# ---------------------------------------------------------------------------

_AUTHORITY_RANK: dict[AgentAuthority, int] = {
    AgentAuthority.READ: 0,
    AgentAuthority.PROPOSE: 1,
    AgentAuthority.EXECUTE: 2,
}

_COST_CLASS_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# Validation issue codes that map to specific error types.
_BUDGET_CODES = {
    "task_budget_exceeded",
    "agent_call_budget_exceeded",
    "tool_call_budget_exceeded",
    "iteration_budget_exceeded",
    "deadline_exceeded",
}
_CYCLE_CODES = {"cycle"}
_HASH_CODES = {"plan_hash_mismatch", "request_hash_mismatch"}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Planner(Protocol):
    """Pluggable planner."""

    def create_plan(
        self,
        request: PlanningRequest,
        registry: AgentRegistry,
    ) -> PlanDraft: ...


# ---------------------------------------------------------------------------
# Deterministic implementation
# ---------------------------------------------------------------------------


class DeterministicPlanner:
    """Rule-based, side-effect-free planner.

    The planner holds no mutable state — every ``create_plan`` call is
    independent and reproducible.
    """

    def __init__(
        self,
        *,
        gate: RuleBasedComplexityGate | None = None,
        customer_recovery_template: CustomerRecoveryTemplate | None = None,
    ) -> None:
        from multi_agent.plan_validator import PlanValidator

        self._gate = gate or RuleBasedComplexityGate()
        self._validator = PlanValidator(gate=self._gate)
        self._recovery_template = (
            customer_recovery_template or DEFAULT_CUSTOMER_RECOVERY_TEMPLATE
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_plan(
        self,
        request: PlanningRequest,
        registry: AgentRegistry,
    ) -> PlanDraft:
        # Step 1 — Registry version match.
        snapshot = registry.snapshot()
        if snapshot.version != request.registry_version:
            raise RegistryVersionMismatchError(
                f"Registry version mismatch: request="
                f"{request.registry_version!r} snapshot={snapshot.version!r}"
            )

        # Step 2 — Complexity gate.
        decision = self._gate.decide(request, registry)

        # Step 3 — Deterministic workflow → empty plan.
        if decision.route == "deterministic_workflow":
            return self._build_empty_plan(request, decision)

        # Step 4 — Build intents.
        intents = self._build_intents(request, decision)

        # Step 4b — Validate write/approval requirements.
        self._validate_write_approval_requirements(request, intents)

        # Step 5 — Validate intent structure (unique IDs, deps exist, no cycle).
        self._validate_intents(intents)

        # Step 6 — Select agents for all intents (first pass).
        intent_to_task_id: dict[str, str] = {}
        intent_to_cap: dict[str, AgentCapability] = {}
        for intent in intents:
            cap = self._select_agent(intent, registry)
            task_id = self._stable_task_id(
                run_id=request.run_id,
                intent_id=intent.intent_id,
                task_type=intent.task_type,
                agent_id=cap.agent_id,
            )
            intent_to_task_id[intent.intent_id] = task_id
            intent_to_cap[intent.intent_id] = cap

        # Step 7+8 — Build PlannedTasks with resolved dependencies.
        planned_tasks: list[PlannedTask] = []
        for intent in intents:
            cap = intent_to_cap[intent.intent_id]
            # Convert intent_id dependencies → task_id dependencies.
            # No filtering — missing deps already rejected in step 5.
            resolved_deps: frozenset[str] = frozenset(
                intent_to_task_id[dep] for dep in intent.dependencies
            )
            pt = self._build_planned_task(intent, cap, request, resolved_deps)
            planned_tasks.append(pt)

        # Step 9 — Assemble draft (hashes auto-computed by PlanDraft).
        request_hash = compute_request_hash(request)
        draft = PlanDraft(
            request=request,
            request_hash=request_hash,
            complexity=decision,
            tasks=planned_tasks,
            planner_version=PLANNER_VERSION,
            summary=self._build_summary(request, decision, planned_tasks),
            warnings=[],
        )

        # Step 10 — Validate.
        report = self._validator.validate(request, draft, registry)
        if not report.valid:
            # Step 11 — Raise the appropriate specific error type.
            self._raise_for_issues(report.issues)

        return draft

    # ------------------------------------------------------------------
    # Intent construction
    # ------------------------------------------------------------------

    def _build_intents(
        self, request: PlanningRequest, decision: Any
    ) -> list[TaskIntent]:
        """Translate the request into TaskIntents."""
        # Customer Recovery template.
        if request.signals.objective_kind == CUSTOMER_RECOVERY_OBJECTIVE_KIND:
            return self._recovery_template.build_intents()

        # Single-agent route — one intent covering the whole objective.
        if decision.route == "single_agent":
            if request.signals.requested_tasks:
                # If explicit tasks are given, use the first one.
                rt = request.signals.requested_tasks[0]
                return [task_intent_from_requested_task(rt)]
            domain = (
                sorted(request.signals.domains)[0]
                if request.signals.domains
                else "default"
            )
            task_type = (
                sorted(request.signals.requested_task_types)[0]
                if request.signals.requested_task_types
                else "default"
            )
            authority = (
                AgentAuthority.PROPOSE
                if request.signals.requires_approval or request.signals.requires_write
                else AgentAuthority.READ
            )
            return [
                TaskIntent(
                    intent_id="primary",
                    task_type=task_type,
                    domain=domain,
                    objective=request.objective,
                    dependencies=[],
                    required=True,
                    preferred_authority=authority,
                    required_tools=frozenset(),
                    estimated_tool_calls=0,
                )
            ]

        # Multi-agent route without a template — requires explicit
        # requested_tasks.  Guessing domains or building a cartesian
        # product is forbidden.
        if decision.route == "multi_agent":
            if not request.signals.requested_tasks:
                raise PlanningInputError(
                    "multi_agent route without a template requires explicit "
                    "signals.requested_tasks; cannot infer domain→task mapping"
                )
            return [
                task_intent_from_requested_task(rt)
                for rt in request.signals.requested_tasks
            ]

        # Should not reach here — gate already validated the route.
        raise PlanningInputError(f"unknown route {decision.route!r}")

    # ------------------------------------------------------------------
    # Write / approval requirement validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_write_approval_requirements(
        request: PlanningRequest, intents: list[TaskIntent]
    ) -> None:
        """If ``requires_write`` or ``requires_approval`` is set, at least
        one intent must have ``preferred_authority == PROPOSE``.

        Otherwise the request is structurally contradictory — the caller
        asked for write/approval but no task is allowed to propose.
        """
        if not (request.signals.requires_write or request.signals.requires_approval):
            return
        if not intents:
            return
        has_propose = any(
            i.preferred_authority is AgentAuthority.PROPOSE for i in intents
        )
        if not has_propose:
            raise PlanningInputError(
                "signals.requires_write or requires_approval is True but no "
                "task has preferred_authority=PROPOSE; cannot satisfy the "
                "request"
            )

    # ------------------------------------------------------------------
    # Intent structure validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_intents(intents: list[TaskIntent]) -> None:
        """Validate intent_id uniqueness, dependency existence, and cycles.

        Per Phase 3 review R1 P0-5: missing intent dependencies must
        Fail-Closed, not be silently dropped.
        """
        if not intents:
            return

        intent_ids: set[str] = set()
        for intent in intents:
            if intent.intent_id in intent_ids:
                raise PlanningInputError(f"duplicate intent_id {intent.intent_id!r}")
            intent_ids.add(intent.intent_id)

        # All dependencies must reference existing intent_ids.
        for intent in intents:
            missing = set(intent.dependencies) - intent_ids
            if missing:
                raise PlanningInputError(
                    f"Intent {intent.intent_id!r} has missing dependencies: "
                    f"{sorted(missing)}"
                )

        # Cycle detection on intent_id graph.
        graph: dict[str, set[str]] = {i.intent_id: set() for i in intents}
        in_degree: dict[str, int] = {i.intent_id: 0 for i in intents}
        for intent in intents:
            for dep in intent.dependencies:
                graph[dep].add(intent.intent_id)
                in_degree[intent.intent_id] += 1
        # Kahn's algorithm.
        queue: list[str] = sorted(
            i.intent_id for i in intents if in_degree[i.intent_id] == 0
        )
        visited: set[str] = set()
        while queue:
            node = queue.pop(0)
            visited.add(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        cycle_nodes = sorted(set(in_degree.keys()) - visited)
        if cycle_nodes:
            raise PlanCycleError(
                f"Intent dependency graph contains a cycle involving {cycle_nodes!r}"
            )

    # ------------------------------------------------------------------
    # Agent selection
    # ------------------------------------------------------------------

    def _build_planned_task(
        self,
        intent: TaskIntent,
        cap: AgentCapability,
        request: PlanningRequest,
        resolved_deps: frozenset[str],
    ) -> PlannedTask:
        """Build a PlannedTask from a resolved intent + capability."""
        task_id = self._stable_task_id(
            run_id=request.run_id,
            intent_id=intent.intent_id,
            task_type=intent.task_type,
            agent_id=cap.agent_id,
        )
        task = AgentTask(
            task_id=task_id,
            agent_id=cap.agent_id,
            task_type=intent.task_type,
            objective=intent.objective,
            tenant_id=request.tenant_id,
            dependencies=resolved_deps,
            required=intent.required,
            required_evidence=list(intent.required_evidence),
            timeout_ms=cap.timeout_ms,
            idempotency_key=f"{request.run_id}:{task_id}",
        )
        return PlannedTask(
            intent_id=intent.intent_id,
            domain=intent.domain,
            preferred_authority=intent.preferred_authority,
            required_tools=intent.required_tools,
            estimated_tool_calls=intent.estimated_tool_calls,
            required=intent.required,
            task=task,
        )

    def _select_agent(
        self, intent: TaskIntent, registry: AgentRegistry
    ) -> AgentCapability:
        """Select the minimum-privilege capable agent for *intent*.

        Filters:

        1. ``enabled=True``
        2. ``supported_tasks`` contains ``intent.task_type``
        3. ``domains`` contains ``intent.domain``
        4. ``authority`` is READ or PROPOSE (EXECUTE filtered out)
        5. ``authority >= intent.preferred_authority``
        6. ``timeout_ms`` covers a notional task (any positive value)

        Sort key (ascending, deterministic):

        1. ``_AUTHORITY_RANK[authority]`` — READ before PROPOSE
        2. ``_COST_CLASS_RANK[estimated_cost_class]``
        3. ``timeout_ms`` — smaller first
        4. ``agent_id`` — lexicographic
        5. ``version`` — lexicographic
        """
        candidates: list[AgentCapability] = []
        for cap in registry.list_all():
            if not cap.enabled:
                continue
            if intent.task_type not in cap.supported_tasks:
                continue
            if intent.domain not in cap.domains:
                continue
            if cap.authority is AgentAuthority.EXECUTE:
                # EXECUTE agents are filtered out, not failed on sight.
                continue
            if (
                _AUTHORITY_RANK[cap.authority]
                < _AUTHORITY_RANK[intent.preferred_authority]
            ):
                continue
            candidates.append(cap)

        if not candidates:
            raise UnsupportedCapabilityError(
                f"No READ/PROPOSE agent supports task_type={intent.task_type!r} "
                f"domain={intent.domain!r} authority>={intent.preferred_authority.value}"
            )

        candidates.sort(
            key=lambda c: (
                _AUTHORITY_RANK[c.authority],
                _COST_CLASS_RANK[c.estimated_cost_class],
                c.timeout_ms,
                c.agent_id,
                c.version,
            )
        )
        return candidates[0]

    # ------------------------------------------------------------------
    # Stable Task ID
    # ------------------------------------------------------------------

    @staticmethod
    def _stable_task_id(
        *,
        run_id: str,
        intent_id: str,
        task_type: str,
        agent_id: str,
    ) -> str:
        """Deterministic 24-char task ID — no random UUIDs."""
        return stable_hash(
            {
                "run_id": run_id,
                "intent_id": intent_id,
                "task_type": task_type,
                "agent_id": agent_id,
            }
        )[:24]

    # ------------------------------------------------------------------
    # Empty plan (deterministic_workflow)
    # ------------------------------------------------------------------

    def _build_empty_plan(self, request: PlanningRequest, decision: Any) -> PlanDraft:
        request_hash = compute_request_hash(request)
        return PlanDraft(
            request=request,
            request_hash=request_hash,
            complexity=decision,
            tasks=[],
            planner_version=PLANNER_VERSION,
            summary="deterministic_workflow: no tasks",
            warnings=[],
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary(
        request: PlanningRequest,
        decision: Any,
        tasks: list[PlannedTask],
    ) -> str:
        """Produce a fixed-template summary string — no chain-of-thought."""
        route = decision.route
        n = len(tasks)
        agents = sorted({pt.task.agent_id for pt in tasks})
        return (
            f"route={route} tasks={n} agents={','.join(agents)} "
            f"objective_kind={request.signals.objective_kind or 'none'}"
        )

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_for_issues(issues: list[Any]) -> None:
        """Raise the appropriate specific error type for the first error.

        Mapping (per Phase 3 review R1 P1-B):

        * budget codes → :class:`BudgetExceededPlanningError`
        * cycle codes → :class:`PlanCycleError`
        * hash codes → :class:`PlanIntegrityError`
        * anything else → :class:`PlanValidationError`
        """
        errors = [i for i in issues if i.severity == "error"]
        if not errors:
            return
        first = errors[0]
        messages = "; ".join(f"{i.code}: {i.message}" for i in errors[:5])

        if first.code in _BUDGET_CODES:
            raise BudgetExceededPlanningError(
                f"Plan validation failed (budget): {messages}"
            )
        if first.code in _CYCLE_CODES:
            raise PlanCycleError(f"Plan validation failed (cycle): {messages}")
        if first.code in _HASH_CODES:
            raise PlanIntegrityError(f"Plan validation failed (integrity): {messages}")
        raise PlanValidationError(
            f"Plan validation failed with {len(errors)} error(s): {messages}"
        )


__all__ = [
    "DeterministicPlanner",
    "Planner",
]
