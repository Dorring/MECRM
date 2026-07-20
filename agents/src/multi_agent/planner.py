"""Phase 3 Deterministic Planner.

Produces a :class:`PlanDraft` from a :class:`PlanningRequest` and an
:class:`AgentRegistry`.  The planner is **side-effect free** — it never
calls an agent handler, never invokes a tool, never writes to the
registry, and never opens a network connection.

Flow (per Phase 3 spec §7, updated for R3):

1. Verify Registry Snapshot version.
2. Run Complexity Gate.
3. ``deterministic_workflow`` → return empty plan.
4. Build TaskIntents via the shared :func:`resolve_expected_intents`
   (R2 P0-1 — Planner and Validator share the same source of truth).
5. Validate write/approval requirements (≥1 PROPOSE intent).
6. Validate intent structure (unique IDs, dependencies exist, no cycle).
7. Assign agents via the shared :func:`resolve_agent_assignment`
   (R3 P0-2 — Planner and Validator share the same assignment logic).
   This internally builds tool-aware candidate lists via
   :func:`resolve_candidate_agents` (R2 P0-3) and performs a bounded
   global search for multi-agent diversity (R2 P0-4 + R3 P1).
8. Build canonical PlannedTasks via the shared
   :func:`build_expected_planned_tasks` (R3 P0-3 — Planner and
   Validator share the same Canonical Plan reconstruction).
9. Assemble PlanDraft (hashes auto-computed).
10. Run PlanValidator.
11. If invalid → raise the appropriate specific error type.

R3 changes:

* The Planner no longer holds its own copy of the candidate filtering,
  global assignment, or task-building logic.  All three now live in
  :mod:`multi_agent.planning` as shared pure functions that the
  Validator also calls.  This closes the "substitute a more privileged
  agent" hole: even if a tampered plan is registry-supported, the
  Validator recomputes the canonical assignment and rejects any
  mismatch.
* ``max_retries`` is now fixed at ``0`` (Phase 3 default) rather than
  being read from the capability.  This makes the Canonical Plan
  fully deterministic.
* ``priority``, ``status``, ``input_data``, ``user_id``,
  ``correlation_id``, ``started_at``, ``completed_at`` are now
  canonical fixed values enforced at construction time.
"""

from __future__ import annotations

from typing import Any, Protocol

from multi_agent.contracts import AgentAuthority
from multi_agent.registry import AgentRegistry
from multi_agent.planning import (
    PLANNER_VERSION,
    PlanDraft,
    PlannedTask,
    PlanningRequest,
    TaskIntent,
    build_expected_planned_tasks,
    compute_request_hash,
    resolve_agent_assignment,
    resolve_expected_intents,
)
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    PlanCycleError,
    PlanIntegrityError,
    PlanValidationError,
    PlanningInputError,
    RegistryVersionMismatchError,
)
from multi_agent.planning_templates import (
    DEFAULT_CUSTOMER_RECOVERY_TEMPLATE,
    CustomerRecoveryTemplate,
)
from multi_agent.complexity_gate import RuleBasedComplexityGate

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
    # R3 codes
    "planner_version_mismatch",
    "agent_assignment_mismatch",
    "agent_version_mismatch",
    "dependency_mismatch",
    "required_evidence_mismatch",
    "task_lifecycle_violation",
    "task_field_mismatch",
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

        # Step 7 — Assign agents via the shared pure function (R3 P0-2).
        # This internally builds tool-aware candidate lists and performs
        # a bounded global search for multi-agent diversity.
        assignment = resolve_agent_assignment(request, decision, intents, registry)

        # Step 8 — Build canonical PlannedTasks (R3 P0-3).
        planned_tasks = build_expected_planned_tasks(request, intents, assignment)

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
