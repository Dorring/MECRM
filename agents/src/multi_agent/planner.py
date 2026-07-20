"""Phase 3 Deterministic Planner.

Produces a :class:`PlanDraft` from a :class:`PlanningRequest` and an
:class:`AgentRegistry`.  The planner is **side-effect free** — it never
calls an agent handler, never invokes a tool, never writes to the
registry, and never opens a network connection.

Flow (per Phase 3 spec §7, updated for R2):

1. Verify Registry Snapshot version.
2. Run Complexity Gate.
3. ``deterministic_workflow`` → return empty plan.
4. Build TaskIntents via the shared :func:`resolve_expected_intents`
   (R2 P0-1 — Planner and Validator share the same source of truth).
5. Validate write/approval requirements (≥1 PROPOSE intent).
6. Validate intent structure (unique IDs, dependencies exist, no cycle).
7. Build per-intent candidate lists with **tool-aware filtering**
   (R2 P0-3).
8. Assign agents via **global deterministic multi-agent assignment**
   when route == multi_agent (R2 P0-4 — guarantees ≥2 distinct agents
   when feasible); single_agent uses the per-intent selector.
9. Generate stable AgentTask IDs + idempotency keys.
10. Resolve intent_id dependencies → task_id dependencies (no filtering).
11. Assemble PlanDraft (hashes auto-computed).
12. Run PlanValidator.
13. If invalid → raise the appropriate specific error type.

Agent selection (per Phase 3 reviews R1 + R2):

* EXECUTE agents are *filtered out* of the candidate set, not failed
  on sight.  If filtering leaves at least one READ/PROPOSE candidate,
  the minimum-privilege one is chosen.  If only EXECUTE candidates
  remain, the planner fails closed.
* ``preferred_authority`` comes from the :class:`RequestedTask` (or
  template), never hardcoded.
* ``requires_write=True`` or ``requires_approval=True`` require at
  least one PROPOSE-level intent, else :class:`PlanningInputError`.
* **Tool-aware (R2 P0-3)**: candidates that do not cover
  ``intent.required_tools`` are filtered out *before* sorting, and
  each required tool must exist in the catalog with authority
  <= the agent's authority and <= PROPOSE.
* **Global assignment (R2 P0-4)**: for ``multi_agent`` route, the
  planner searches for an assignment with at least two distinct
  agents before falling back to per-intent greedy selection.
"""

from __future__ import annotations

from itertools import product
from typing import Any, Protocol

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentTask,
    ToolAuthority,
)
from multi_agent.registry import AgentRegistry
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    TaskIntent,
    compute_request_hash,
    resolve_expected_intents,
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
from multi_agent.complexity_gate import RuleBasedComplexityGate
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
_HASH_CODES = {
    "plan_hash_mismatch",
    "request_hash_mismatch",
    "plan_intent_mismatch",
    "unstable_task_id",
    "idempotency_key_mismatch",
    "planned_task_required_mismatch",
    "duplicate_intent_id",
}


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

        # Step 4 — Build intents via the shared pure function (R2 P0-1).
        intents = resolve_expected_intents(request, decision)

        # Step 5 — Validate write/approval requirements.
        self._validate_write_approval_requirements(request, intents)

        # Step 6 — Validate intent structure (unique IDs, deps exist, no cycle).
        self._validate_intents(intents)

        # Step 7 — Build per-intent candidate lists (tool-aware, R2 P0-3).
        intent_candidates: dict[str, list[AgentCapability]] = {}
        for intent in intents:
            candidates = self._candidate_agents(intent, registry)
            if not candidates:
                raise UnsupportedCapabilityError(
                    f"No READ/PROPOSE agent with required tools supports "
                    f"task_type={intent.task_type!r} domain={intent.domain!r} "
                    f"authority>={intent.preferred_authority.value} "
                    f"required_tools={sorted(intent.required_tools)!r}"
                )
            intent_candidates[intent.intent_id] = candidates

        # Step 8 — Assign agents.  multi_agent requires ≥2 distinct agents
        # when feasible (R2 P0-4).  single_agent uses greedy selection.
        if decision.route == "multi_agent" and len(intents) >= 2:
            assignment = self._assign_agents_global(intents, intent_candidates)
        else:
            assignment = {
                intent.intent_id: intent_candidates[intent.intent_id][0]
                for intent in intents
            }

        # Step 9 — Generate stable task IDs.
        intent_to_task_id: dict[str, str] = {}
        for intent in intents:
            cap = assignment[intent.intent_id]
            task_id = self._stable_task_id(
                run_id=request.run_id,
                intent_id=intent.intent_id,
                task_type=intent.task_type,
                agent_id=cap.agent_id,
            )
            intent_to_task_id[intent.intent_id] = task_id

        # Step 10 — Build PlannedTasks with resolved dependencies (no filtering).
        planned_tasks: list[PlannedTask] = []
        for intent in intents:
            cap = assignment[intent.intent_id]
            resolved_deps: frozenset[str] = frozenset(
                intent_to_task_id[dep] for dep in intent.dependencies
            )
            pt = self._build_planned_task(intent, cap, request, resolved_deps)
            planned_tasks.append(pt)

        # Step 11 — Assemble draft (hashes auto-computed by PlanDraft).
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

        # Step 12 — Validate.
        report = self._validator.validate(request, draft, registry)
        if not report.valid:
            # Step 13 — Raise the appropriate specific error type.
            self._raise_for_issues(report.issues)

        return draft

    # ------------------------------------------------------------------
    # Intent construction
    # ------------------------------------------------------------------

    # R2 P0-1: intent construction now lives in the shared pure function
    # ``multi_agent.planning.resolve_expected_intents`` so the Planner
    # and Validator cannot disagree.  The legacy ``_build_intents``
    # method has been removed.

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

    def _candidate_agents(
        self, intent: TaskIntent, registry: AgentRegistry
    ) -> list[AgentCapability]:
        """Return the stable, **tool-aware** candidate list for *intent*.

        Filters (all AND, R2 P0-3):

        1. ``enabled=True``
        2. ``supported_tasks`` contains ``intent.task_type``
        3. ``domains`` contains ``intent.domain``
        4. ``authority`` is READ or PROPOSE (EXECUTE filtered out)
        5. ``authority >= intent.preferred_authority``
        6. ``required_tools ⊆ cap.allowed_tools`` AND every required tool
           exists in the catalog with authority <= cap.authority and
           <= PROPOSE (Phase 3 ceiling).

        Sort key (ascending, deterministic):

        1. ``_AUTHORITY_RANK[authority]`` — READ before PROPOSE
        2. ``_COST_CLASS_RANK[estimated_cost_class]``
        3. ``timeout_ms`` — smaller first
        4. ``agent_id`` — lexicographic
        5. ``version`` — lexicographic
        """
        candidates: list[AgentCapability] = []
        # Pre-validate required tools against the catalog once per intent.
        tool_authority_ok: dict[str, bool] = {}
        for tool_name in intent.required_tools:
            if not registry.tool_catalog.is_registered(tool_name):
                # Unknown tool → no candidate can satisfy this intent.
                # The caller will see an empty candidate list and raise
                # UnsupportedCapabilityError.
                return []
            tool = registry.tool_catalog.resolve(tool_name)
            # Phase 3 ceiling: required tools must be READ or PROPOSE.
            if tool.authority is ToolAuthority.EXECUTE:
                return []
            tool_authority_ok[tool_name] = True

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
            # R2 P0-3: tool-aware filtering.
            if not intent.required_tools.issubset(cap.allowed_tools):
                continue
            # Per-tool authority hierarchy check.
            tool_ok = True
            for tool_name in intent.required_tools:
                tool = registry.tool_catalog.resolve(tool_name)
                if (
                    cap.authority is AgentAuthority.READ
                    and tool.authority is not ToolAuthority.READ
                ):
                    tool_ok = False
                    break
                if (
                    cap.authority is AgentAuthority.PROPOSE
                    and tool.authority is ToolAuthority.EXECUTE
                ):
                    tool_ok = False
                    break
            if not tool_ok:
                continue
            candidates.append(cap)

        candidates.sort(
            key=lambda c: (
                _AUTHORITY_RANK[c.authority],
                _COST_CLASS_RANK[c.estimated_cost_class],
                c.timeout_ms,
                c.agent_id,
                c.version,
            )
        )
        return candidates

    # ------------------------------------------------------------------
    # Global multi-agent assignment (R2 P0-4)
    # ------------------------------------------------------------------

    def _assign_agents_global(
        self,
        intents: list[TaskIntent],
        intent_candidates: dict[str, list[AgentCapability]],
    ) -> dict[str, AgentCapability]:
        """Pick a deterministic agent assignment that guarantees ≥2
        distinct agents when feasible.

        Algorithm:

        1. Build the cartesian product of per-intent candidate lists.
        2. Discard assignments where the same agent is chosen for
           multiple intents *and* the result would be a single-agent
           plan (multi_agent route requires ≥2 distinct agents).
        3. Among feasible assignments, pick the one with the stable
           minimum composite key:

           a. Number of distinct agents (more is better → we want ≥2,
              so among feasible assignments we prefer more diversity
              only as a tiebreaker; the primary key is total cost).
           b. Total authority rank (lower = least privilege).
           c. Total cost class rank.
           d. Total timeout_ms.
           e. Sorted agent_id concatenation (lexicographic).
           f. Sorted version concatenation.

        Phase 3 task/candidate counts are bounded by ``max_tasks`` so
        the cartesian product stays small.

        If no feasible diverse assignment exists, fall back to the
        per-intent greedy selection (the first candidate for each
        intent) and let the Validator surface the
        ``multi_agent_too_few_agents`` issue.
        """
        # Per-intent candidate lists (each already sorted).
        lists: list[list[AgentCapability]] = [
            intent_candidates[i.intent_id] for i in intents
        ]
        intent_ids = [i.intent_id for i in intents]

        best_assignment: dict[str, AgentCapability] | None = None
        best_key: tuple[Any, ...] | None = None

        for combo in product(*lists):
            assignment = dict(zip(intent_ids, combo))
            distinct_agents = {c.agent_id for c in combo}
            # We want ≥2 distinct agents when feasible.
            if len(distinct_agents) < 2:
                continue
            # Composite sort key: total authority, total cost, total
            # timeout, agent_id concat, version concat.
            total_auth = sum(_AUTHORITY_RANK[c.authority] for c in combo)
            total_cost = sum(_COST_CLASS_RANK[c.estimated_cost_class] for c in combo)
            total_timeout = sum(c.timeout_ms for c in combo)
            agent_ids_sorted = sorted(c.agent_id for c in combo)
            versions_sorted = sorted(c.version for c in combo)
            key = (
                total_auth,
                total_cost,
                total_timeout,
                agent_ids_sorted,
                versions_sorted,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_assignment = assignment

        if best_assignment is not None:
            return best_assignment

        # Fallback: per-intent greedy (first candidate per intent).
        # The Validator will raise multi_agent_too_few_agents if the
        # greedy assignment collapses to a single agent.
        return {intent_id: intent_candidates[intent_id][0] for intent_id in intent_ids}

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
