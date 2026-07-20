"""Phase 3 Deterministic Planner.

Produces a :class:`PlanDraft` from a :class:`PlanningRequest` and an
:class:`AgentRegistry`.  The planner is **side-effect free** — it never
calls an agent handler, never invokes a tool, never writes to the
registry, and never opens a network connection.

Flow (per Phase 3 spec §7):

1. Verify Registry Snapshot version.
2. Run Complexity Gate.
3. ``deterministic_workflow`` → return empty plan.
4. Build TaskIntents (from template or single intent).
5. Select minimum-privilege agent per intent.
6. Generate stable AgentTask IDs.
7. Assemble PlanDraft (without hash).
8. Run PlanValidator.
9. If valid → compute plan_hash and return.
10. If invalid → raise ``PlanValidationError``.

Agent selection (per Phase 3 review R1):

* EXECUTE agents are *filtered out* of the candidate set, not failed
  on sight.  If filtering leaves at least one READ/PROPOSE candidate,
  the minimum-privilege one is chosen.  If only EXECUTE candidates
  remain, the planner fails closed.
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
)
from multi_agent.planning_errors import (
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
        # Late import to avoid circular module load.
        from multi_agent.plan_validator import PlanValidator

        self._gate = gate or RuleBasedComplexityGate()
        self._validator = PlanValidator()
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

        # Step 5 — Select agents for all intents (first pass).
        # We need all task_ids before we can resolve dependencies, because
        # TaskIntent.dependencies reference intent_ids, but AgentTask.dependencies
        # must reference task_ids.
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

        # Step 6 — Build PlannedTasks with resolved dependencies.
        planned_tasks: list[PlannedTask] = []
        for intent in intents:
            cap = intent_to_cap[intent.intent_id]
            # Convert intent_id dependencies → task_id dependencies.
            resolved_deps: frozenset[str] = frozenset(
                intent_to_task_id[dep]
                for dep in intent.dependencies
                if dep in intent_to_task_id
            )
            pt = self._build_planned_task(intent, cap, request, resolved_deps)
            planned_tasks.append(pt)

        # Step 7 — Assemble draft (hash auto-computed by PlanDraft).
        request_hash = compute_request_hash(request)
        draft = PlanDraft(
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            objective=request.objective,
            complexity=decision,
            tasks=planned_tasks,
            planner_version=PLANNER_VERSION,
            registry_version=snapshot.version,
            request_hash=request_hash,
            summary=self._build_summary(request, decision, planned_tasks),
            warnings=[],
        )

        # Step 8 — Validate.
        report = self._validator.validate(request, draft, registry)
        if not report.valid:
            errors = [i for i in report.issues if i.severity == "error"]
            messages = "; ".join(f"{i.code}: {i.message}" for i in errors[:5])
            raise PlanValidationError(
                f"Plan validation failed with {len(errors)} error(s): {messages}"
            )

        # Step 9 — Hash is already computed & verified by PlanDraft.
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

        # Multi-agent route without a template — synthesise intents from
        # requested_task_types.  Each task type becomes its own intent.
        if decision.route == "multi_agent":
            if not request.signals.requested_task_types:
                raise PlanningInputError(
                    "multi_agent route requires requested_task_types when "
                    "no template matches"
                )
            domain = (
                sorted(request.signals.domains)[0]
                if request.signals.domains
                else "default"
            )
            intents: list[TaskIntent] = []
            for idx, task_type in enumerate(
                sorted(request.signals.requested_task_types)
            ):
                intents.append(
                    TaskIntent(
                        intent_id=f"task_{idx:02d}",
                        task_type=task_type,
                        domain=domain,
                        objective=request.objective,
                        dependencies=[],
                        required=True,
                        preferred_authority=AgentAuthority.READ,
                        required_tools=frozenset(),
                        estimated_tool_calls=0,
                    )
                )
            return intents

        # Should not reach here — gate already validated the route.
        raise PlanningInputError(f"unknown route {decision.route!r}")

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
        6. ``timeout_ms`` covers a notional task (any positive value —
           the planner uses the agent's own timeout_ms for the task)

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
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            objective=request.objective,
            complexity=decision,
            tasks=[],
            planner_version=PLANNER_VERSION,
            registry_version=request.registry_version,
            request_hash=request_hash,
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


__all__ = [
    "DeterministicPlanner",
    "Planner",
]
