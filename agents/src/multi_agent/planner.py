"""Phase 3 Deterministic Planner.

Produces a :class:`PlanDraft` from a :class:`PlanningRequest` and an
:class:`AgentRegistry`.  The planner is **side-effect free** — it never
calls an agent handler, never invokes a tool, never writes to the
registry, and never opens a network connection.

Flow (per Phase 3 spec §7, updated for R4):

1. Verify Registry Snapshot version.
2. Run Complexity Gate.
3. ``deterministic_workflow`` → return empty plan.
4. Build TaskIntents via the shared :func:`resolve_expected_intents`
   (R2 P0-1 — Planner and Validator share the same source of truth).
5. Validate write/approval requirements (≥1 PROPOSE intent).
6. Validate intent structure (unique IDs, dependencies exist, no cycle)
   via the shared :func:`validate_intent_graph` (R4 P0-1 — Planner and
   Validator share the same intent-graph validation).
7. Validate Intent / Tool Authority alignment via the shared
   :func:`validate_intent_tool_authority` (R4 P0-3 — a READ intent
   cannot carry a PROPOSE/EXECUTE tool).
8. Assign agents via the shared :func:`resolve_agent_assignment`
   (R3 P0-2 + R4 P0-2 — budget-aware: structural pre-checks +
   per-combo DAG deadline filtering before deterministic sort).
9. Build canonical PlannedTasks via the shared
   :func:`build_expected_planned_tasks` (R3 P0-3 — Planner and
   Validator share the same Canonical Plan reconstruction).
10. Assemble PlanDraft (hashes auto-computed).
11. Run PlanValidator.
12. If invalid → raise the appropriate specific error type.

R4 changes:

* Intent-graph validation is now a shared pure function
  (:func:`validate_intent_graph`) so both Planner and Validator
  reject ``duplicate_intent_id`` / ``missing_intent_dependency`` /
  ``intent_cycle`` with stable issue codes instead of letting
  ``KeyError`` escape during Canonical Plan reconstruction.
* Intent ``preferred_authority`` must cover the highest authority
  required by any of its ``required_tools`` (READ tool ≥ READ,
  PROPOSE tool ≥ PROPOSE, EXECUTE tool rejected in Phase 3).  Silent
  auto-elevation is forbidden — :class:`PlanningInputError` fails
  closed via :func:`validate_intent_tool_authority`.
* ``resolve_agent_assignment`` is now budget-aware: structural budgets
  (``max_tasks`` / ``max_agent_calls`` / ``max_tool_calls`` /
  ``max_iterations``) are pre-checked before searching, and each
  candidate combination is filtered by DAG critical-path deadline
  before the deterministic sort picks the cheapest feasible combo.
* ``customer_recovery_template`` constructor parameter removed — the
  parameter was silently ignored (R4 P1-2).  Phase 3 supports only
  the default template; callers who need a custom template must wait
  for a future phase that wires Planner + Validator to a shared
  template context (id, version, content hash).
* Per-task Agent Version check removed (R4 P1-3) — ``PlannedTask``
  does not carry ``agent_version`` and both expected/actual
  capabilities come from the same Registry Snapshot, so the
  comparison was tautological.  Version drift is caught by the
  plan-level ``registry_version`` check.
* ``PlannedTask.planning_metadata`` is now copied verbatim from
  ``TaskIntent.metadata`` (R4 P1-1) and enters Plan Hash + Canonical
  Plan comparison so template/phase metadata tampering is detectable.
"""

from __future__ import annotations

from typing import Any, Protocol

from multi_agent.complexity_gate import RuleBasedComplexityGate
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
    validate_intent_graph,
    validate_intent_tool_authority,
    validate_write_approval_requirements,
)
from multi_agent.planning_errors import (
    BudgetExceededPlanningError,
    PlanCycleError,
    PlanIntegrityError,
    PlanningInputError,
    PlanValidationError,
    RegistryVersionMismatchError,
)
from multi_agent.registry import AgentRegistry

# Validation issue codes that map to specific error types.
_BUDGET_CODES = {
    "task_budget_exceeded",
    "agent_call_budget_exceeded",
    "tool_call_budget_exceeded",
    "iteration_budget_exceeded",
    "deadline_exceeded",
}
_CYCLE_CODES = {"cycle", "intent_cycle"}
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
    "dependency_mismatch",
    "required_evidence_mismatch",
    "task_lifecycle_violation",
    "task_field_mismatch",
    # R4 codes
    "missing_intent_dependency",
    "tool_authority_mismatch",
    # R5 codes
    "write_request_missing_propose_intent",
    "approval_request_missing_propose_intent",
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
    ) -> None:
        from multi_agent.plan_validator import PlanValidator

        self._gate = gate or RuleBasedComplexityGate()
        self._validator = PlanValidator(gate=self._gate)

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

        # Step 6 — Validate intent graph (R4 P0-1) via the shared pure
        # function so Planner and Validator agree on what makes an
        # intent graph invalid (duplicate id, missing dependency, cycle).
        self._validate_intents(intents)

        # Step 7 — Validate Intent / Tool Authority alignment (R4 P0-3).
        # A READ intent cannot carry a PROPOSE/EXECUTE tool; silent
        # auto-elevation is forbidden.
        self._validate_tool_authority(intents, registry)

        # Step 8 — Assign agents via the shared pure function (R3 P0-2
        # + R4 P0-2 — budget-aware: structural pre-checks + per-combo
        # DAG deadline filtering before deterministic sort).
        assignment = resolve_agent_assignment(request, decision, intents, registry)

        # Step 9 — Build canonical PlannedTasks (R3 P0-3).
        planned_tasks = build_expected_planned_tasks(request, intents, assignment)

        # Step 10 — Assemble draft (hashes auto-computed by PlanDraft).
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

        # Step 11 — Validate.
        report = self._validator.validate(request, draft, registry)
        if not report.valid:
            # Step 12 — Raise the appropriate specific error type.
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

        R5 P0-1 — this is a thin wrapper around the shared
        :func:`validate_write_approval_requirements` pure function.
        Planner and Validator now share the exact same validation logic
        so a tampered request bypassing ``create_plan`` (e.g. a
        hand-built :class:`PlanDraft`) is rejected by both sides with
        stable Issue Codes (``write_request_missing_propose_intent`` /
        ``approval_request_missing_propose_intent``).
        """
        issues = validate_write_approval_requirements(request, intents)
        if not issues:
            return
        raise PlanningInputError(
            "signals.requires_write or requires_approval is True but no "
            "task has preferred_authority=PROPOSE; cannot satisfy the "
            f"request; issues={issues!r}"
        )

    # ------------------------------------------------------------------
    # Intent structure validation (R4 P0-1 — shared with Validator)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_intents(intents: list[TaskIntent]) -> None:
        """Validate intent_id uniqueness, dependency existence, and cycles.

        R4 P0-1: this is a thin wrapper around the shared
        :func:`validate_intent_graph` pure function.  Planner and
        Validator now share the exact same validation logic so a
        tampered request that would cause Canonical Plan reconstruction
        to raise ``KeyError`` is rejected by both sides with stable
        issue codes (``duplicate_intent_id`` / ``missing_intent_dependency``
        / ``intent_cycle``).

        Per Phase 3 review R1 P0-5: missing intent dependencies must
        Fail-Closed, not be silently dropped.
        """
        issues = validate_intent_graph(intents)
        if not issues:
            return
        # Raise the most specific error type for the first detected issue.
        first = issues[0]
        if first == "intent_cycle":
            raise PlanCycleError(
                f"Intent dependency graph contains a cycle; issues={issues!r}"
            )
        raise PlanningInputError(f"Intent graph validation failed; issues={issues!r}")

    # ------------------------------------------------------------------
    # Intent / Tool Authority alignment (R4 P0-3)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_tool_authority(
        intents: list[TaskIntent], registry: AgentRegistry
    ) -> None:
        """Validate that each Intent's ``preferred_authority`` covers the
        highest authority required by any of its ``required_tools``.

        R4 P0-3: a READ intent cannot carry a PROPOSE/EXECUTE tool.
        Silent auto-elevation is forbidden — the caller explicitly
        declared the authority boundary, so we fail closed with
        :class:`PlanningInputError` instead of bumping the authority.

        EXECUTE tools are rejected outright by Phase 3 authority bounds
        elsewhere; here we only enforce READ vs PROPOSE consistency.
        """
        for intent in intents:
            validate_intent_tool_authority(intent, registry)

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
